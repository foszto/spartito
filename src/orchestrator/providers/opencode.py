"""opencode in headless mode: `opencode run --standalone --format json` (opencode 2.x).

Each turn is one `opencode run` with a private server of its own (`--standalone`): the turn's
environment and configuration reach only that server, and the user's shared opencode service
is neither used nor started. The turn's settings are configuration, not flags:

- a private file named by OPENCODE_CONFIG: the role prompt as an `instructions` file, the
  `policies`, the MCP servers, `update: "disable"` and, when the user's global configuration
  is moved away, the `shell`;
- OPENCODE_CONFIG_CONTENT: the permission rules. It loads after a project's opencode.json and
  so wins over it, where the OPENCODE_CONFIG file loads before it; every deny is backed by a
  policy, which a project cannot lift.

Facts about the CLI (opencode 2.3.2): docs/providers-design.md, §2 and §4.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import shlex
import subprocess
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from . import kit
from .base import REFERENCE, Agent, Event, Invocation, RunContext, Usage, brief, wired

DEFAULT_PERMISSION = "default"  # no --auto: every `ask` is rejected and the turn goes on
PERMISSIONS = (DEFAULT_PERMISSION, "auto")
MODEL_LIST_TIMEOUT = 15
SESSION_PREFIX = "ses_"
OWN_SERVER = kit.OWN_SERVER

# The loadout's `tools` (Claude Code's names) as opencode's permission actions.
TOOL_ACTIONS = {
    "Bash": "shell",
    "Read": "read",
    "Edit": "edit",
    "Write": "edit",
    "MultiEdit": "edit",
    "NotebookEdit": "edit",
    "Glob": "glob",
    "Grep": "grep",
    "WebFetch": "webfetch",
    "WebSearch": "websearch",
    "Task": "subagent",
    "Agent": "subagent",
    "Skill": "skill",
}
BUILTIN_ACTIONS = tuple(dict.fromkeys(TOOL_ACTIONS.values()))
# Denied in every turn: nobody answers a question, and Spartito owns worktrees and sessions
# (the `opencode` namespace tools; their action is `opencode_<function>`, seen in 2.3.2).
ALWAYS_DENIED = (
    "question",
    "opencode_session_move",
    "opencode_session_rename",
    "opencode_worktree_create",
    "opencode_worktree_list",
    "opencode_worktree_remove",
)
# Exit codes of a CLI ended by a signal: 128 + SIGTERM, as opencode 2.3.2 exits on SIGTERM
# (with no closing line), and SIGKILL; a negative code is Python's for a signal.
SIGNAL_EXITS = (130, 137, 143)

# Where the user's global configuration is moved away to (XDG_CONFIG_HOME, a directory of the
# turn) and the shell that gives the agent's commands the user's own value back.
CONFIG_HOME = "opencode-config-home"
SHELL_WRAPPER = "opencode-shell"
INSTRUCTIONS = "opencode-instructions.md"


def new_session_id() -> str:
    """An id opencode creates a session with (`ses` and a suffix; `ses_` and 26 hex digits
    were seen to work in 2.3.2): chosen up front, as Claude's `--session-id`."""
    return SESSION_PREFIX + secrets.token_hex(13)


def model_argument(model: str | None, effort: str | None, notes: list[str]) -> str | None:
    """`--model`'s value: `provider/model#effort` (opencode's variant). A model that names a
    variant of its own keeps it; an effort without a model is ignored, with a note."""
    if not model:
        if effort:
            notes.append(f"effort {effort} is ignored: opencode needs a model for an effort")
        return None
    return f"{model}#{effort}" if effort and "#" not in model else model


def references(text: Any, env: Mapping[str, str]) -> Any:
    """A string of an MCP server's entry with its `${NAME}` references in opencode's terms:
    `{env:NAME}`; `${NAME:-default}` is `{env:NAME}` when `env` has NAME, else the default
    as written. Anything but a string as it is."""
    if not isinstance(text, str):
        return text

    def translate(match: re.Match[str]) -> str:
        name, default = match.group(1), match.group(2)
        if default is not None and not env.get(name):
            return default[2:]
        return "{env:" + name + "}"

    return REFERENCE.sub(translate, text)


def config_server(
    entry: Mapping[str, Any], env: Mapping[str, str], config_home: str | None
) -> dict[str, Any]:
    """An MCP server's entry (mcp.Server.entry's shape) as opencode's `mcp.servers` takes it.
    `config_home`: the XDG_CONFIG_HOME a local server gets (the user's own, when the turn
    moved it away)."""
    if entry.get("url"):
        server: dict[str, Any] = {"type": "remote", "url": references(entry["url"], env)}
        headers = entry.get("headers") or {}
        if headers:
            server["headers"] = {key: references(value, env) for key, value in headers.items()}
        return server
    command = [entry["command"], *(entry.get("args") or ())]
    server = {"type": "local", "command": [references(part, env) for part in command]}
    environment = {key: references(value, env) for key, value in (entry.get("env") or {}).items()}
    if config_home is not None:
        environment["XDG_CONFIG_HOME"] = config_home
    if environment:
        server["environment"] = environment
    return server


def rule(action: str, resource: str, effect: str) -> dict[str, str]:
    """One permission rule or policy statement."""
    return {"action": action, "resource": resource, "effect": effect}


def turn_rules(
    ctx: RunContext, servers: list[str], notes: list[str]
) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    """The turn's permission rules and policies, from its loadout (`servers`: the names of
    its MCP servers). Every deny rule has a `tool.use` deny policy behind it."""
    loadout = ctx.loadout
    permissions: list[dict[str, str]] = []
    policies: list[dict[str, str]] = []

    def deny(action: str, resource: str = "*") -> None:
        permissions.append(rule(action, resource, "deny"))
        policies.append(rule("tool.use", f"{action}:{resource}", "deny"))

    denied: list[str] = []
    if loadout.tools is not None:
        covered = {TOOL_ACTIONS[tool] for tool in loadout.tools if tool in TOOL_ACTIONS}
        denied = [action for action in BUILTIN_ACTIONS if action not in covered]
        ignored = [tool for tool in loadout.tools if tool not in TOOL_ACTIONS]
        if ignored:
            notes.append(f"tools without a counterpart in opencode, ignored: {', '.join(ignored)}")
    if not loadout.slash_commands and "skill" not in denied:
        denied.append("skill")
    for action in (*denied, *ALWAYS_DENIED):
        deny(action)
    if not loadout.slash_commands:
        policies.append(rule("skill.use", "*", "deny"))
    # Outside the worktree: only the directories the turn must write (the shared .git).
    deny("external_directory")
    for directory in ctx.writable_dirs:
        boundary = os.path.realpath(directory).rstrip("/") + "/*"
        permissions.append(rule("external_directory", boundary, "allow"))
        policies.append(rule("tool.use", f"external_directory:{boundary}", "allow"))
    if ctx.mcp:
        permissions.append(rule(f"{OWN_SERVER}_*", "*", "allow"))
    if loadout.strict_mcp:
        policies.append(rule("mcp.use", "*", "deny"))
        policies += [rule("mcp.use", name, "allow") for name in servers]
    if not user_config(ctx):
        policies.append(rule("plugin.use", "*", "deny"))
    return permissions, policies


def user_config(ctx: RunContext) -> bool:
    """Whether the turn keeps the user's global opencode configuration: its setting sources
    name `user` (None: every source)."""
    sources = ctx.loadout.setting_sources
    return sources is None or "user" in sources


def shell_wrapper(original: str | None) -> str:
    """The `shell` of a turn whose XDG_CONFIG_HOME is moved away: the agent's commands get
    the user's own value back (unset, when it was), so `git`, `gh` and the rest read the
    user's configuration; then bash runs them."""
    restore = (
        "unset XDG_CONFIG_HOME"
        if original is None
        else f"export XDG_CONFIG_HOME={shlex.quote(original)}"
    )
    return f'#!/bin/sh\n{restore}\nexec bash "$@"\n'


def _lookup_models() -> kit.Models:
    """The listed models; none when the CLI is missing or fails."""
    try:
        return parse_model_list(request_model_list()), None
    except OSError, subprocess.SubprocessError:
        return [], None


def request_model_list() -> str:
    """The output of `opencode models`, without fetching the catalogue: the models of the
    providers with credentials, one `provider/model` a line. Raises OSError or
    subprocess.SubprocessError."""
    env = {**os.environ, "OPENCODE_DISABLE_MODELS_FETCH": "1"}
    argv = ["opencode", "models", "--standalone"]
    code, out, err = kit.run_cli(argv, MODEL_LIST_TIMEOUT, env=env)
    if code:
        raise subprocess.SubprocessError(f"exit {code}: {brief(err, 200)}")
    return out


def parse_model_list(output: str) -> list[str]:
    """The `provider/model` lines of `opencode models`, in order; anything else is skipped."""
    found = [line.strip() for line in output.splitlines()]
    return list(dict.fromkeys(line for line in found if re.fullmatch(r"[^\s/]+/\S+", line)))


def logged_in(output: str) -> bool | None:
    """Whether `opencode auth list --format json` lists a connected provider (names only,
    no secrets; a key in a configuration file is not listed). None: not its JSON list."""
    try:
        listed = json.loads(output)
    except ValueError:
        return None
    return bool(listed) if isinstance(listed, list) else None


class OpencodeProvider:
    name = "opencode"
    efforts = ("low", "medium", "high", "max")  # offered variants; a model may lack one
    label = "OpenCode"
    description = "The open source coding agent CLI, run as opencode run --format json"
    executable = "opencode"
    version_args = ("--version",)
    min_version = (2, 2, 3)  # 1.x is another line; policies need 2.2.3
    # Names only, never secrets; it misses a provider keyed in a configuration file.
    login_args = ("auth", "list", "--standalone", "--format", "json")
    reports_cost = True
    role_prompt = "every_turn"
    limits = (
        "asks for an answer schema in the prompt without enforcing it",
        "cannot compact: opencode compacts on its own, and that is not reported",
        "reports the context size without a window",
        "takes no MCP servers added in Spartito yet",
        "of the loadout `memory`, `tool_search` and `codex_user_config` have no effect",
        "has no sandbox: only opencode's own tools are held back",
        "runs the agent's commands in bash when `setting_sources` leaves out `user`",
        "may report no cost for subscription sign-ins, and misses opencode's title and"
        " compaction calls",
        "starts a lost session anew instead of failing",
    )
    reports_context = True
    # MCP servers need the owner's mark that a server may run without approval (as decided
    # for Codex); the mark does not exist yet, so none is carried.
    carries_mcp_servers = False
    credit = kit.Credit("[OpenCode](https://opencode.ai)", kit.TRAILER_IF_ADDED)
    cache_effect = {"model": "restarts", "effort": "may restart"}
    config_env = ("XDG_CONFIG_HOME", "XDG_DATA_HOME", "OPENCODE_DB")

    catalog = kit.ModelCatalog(_lookup_models, retry_when=lambda models, note: not models)

    # Whether the last invocation asked for a structured answer: its stream reads one then.
    # One turn per runner process.
    _structured = False

    def models(self) -> list[str]:
        """What `opencode models` lists. Asked once per process; an empty answer is asked
        for again once it is kit.MODEL_LIST_RETRY_SECONDS old."""
        return self.catalog.models(self)

    @staticmethod
    def logged_in(output: str) -> bool | None:
        return logged_in(output)

    def invocation(self, ctx: RunContext) -> Invocation:
        agent = ctx.agent
        permission = agent.permission or DEFAULT_PERMISSION
        if permission not in PERMISSIONS:
            raise ValueError(
                f"opencode takes the permission mode {' or '.join(PERMISSIONS)}, not {permission!r}"
            )
        if ctx.mcp_config is None:
            raise ValueError("an opencode turn needs a path for its configuration")
        notes: list[str] = []
        argv = ["opencode", "run", "--standalone", "--format", "json"]
        if agent.session_id:
            argv += ["--session", agent.session_id]
        else:  # the title also spares the hidden call that would name the session
            argv += ["--session", new_session_id(), "--archived", "--title", agent.id]
        if permission == "auto":
            argv.append("--auto")
        model = model_argument(ctx.model, ctx.effort, notes)
        if model:
            argv += ["--model", model]
        extra_argv, extra_env = wired(ctx.wirings)  # no capability has an opencode wiring yet
        argv += extra_argv
        env = {**extra_env, "PWD": agent.cwd}  # opencode takes its directory from $PWD
        turn_env = {**os.environ, **extra_env}
        config: dict[str, Any] = {"update": "disable"}
        if ctx.system_prompt:
            instructions = ctx.scratch / INSTRUCTIONS
            instructions.write_text(ctx.system_prompt, encoding="utf-8")
            config["instructions"] = [str(instructions)]
        server_home = None
        if not user_config(ctx):
            original = os.environ.get("XDG_CONFIG_HOME")
            home = ctx.scratch / CONFIG_HOME
            home.mkdir(exist_ok=True)
            env["XDG_CONFIG_HOME"] = str(home)
            config["shell"] = str(write_wrapper(ctx.scratch / SHELL_WRAPPER, original))
            # An environment can only set a value: the default stands in for an unset one.
            server_home = original if original is not None else str(Path.home() / ".config")
        if ctx.loadout.setting_sources is not None and "project" not in ctx.loadout.setting_sources:
            env["OPENCODE_DISABLE_PROJECT_CONFIG"] = "1"
        servers = {
            name: config_server(entry, turn_env, server_home)
            for name, entry in kit.turn_servers(ctx).items()
        }
        permissions, policies = turn_rules(ctx, list(servers), notes)
        config["policies"] = policies
        if servers:
            config["mcp"] = {"servers": servers}
        env["OPENCODE_CONFIG"] = str(ctx.mcp_config)
        env["OPENCODE_CONFIG_CONTENT"] = json.dumps({"permissions": permissions})
        self._structured = bool(ctx.output_schema)
        return Invocation(
            argv=argv,
            stdin=kit.schema_in_prompt(ctx.prompt, ctx.output_schema),
            env=env,
            private_files={ctx.mcp_config: json.dumps(config, indent=2)},
            notes=notes,
        )

    def stream(self) -> Stream:
        return Stream(structured=self._structured)

    def parse(self, line: str) -> list[Event]:
        return Stream().feed(line)

    def attach_command(self, agent: Agent) -> list[str]:
        return ["opencode", "--session", agent.session_id or ""]


def write_wrapper(path: Path, original: str | None) -> Path:
    """The shell wrapper of a turn (shell_wrapper), executable by its owner only."""
    path.unlink(missing_ok=True)
    path.write_text(shell_wrapper(original), encoding="utf-8")
    path.chmod(0o700)
    return path


def code_servers(code: Any) -> list[str]:
    """`server.tool` for each `tools.<server>.<tool>` a Code Mode `execute` call names."""
    if not isinstance(code, str):
        return []
    found = re.findall(r"\btools\.([A-Za-z0-9_-]+)\.([A-Za-z0-9_]+)", code)
    return list(dict.fromkeys(f"{server}.{tool}" for server, tool in found))


class Stream(kit.JsonLines):
    """The events of `opencode run --format json`: one object a line, each with `type` and
    `sessionID`. A text part arrives once, whole, when it ends; a tool only once it has
    completed; the usage of every model step in its `step_finish`. The result comes when the
    stream has ended (finish), with the turn's usage summed over its steps."""

    def __init__(self, structured: bool = False) -> None:
        self.structured = structured
        self.session: str | None = None
        self.texts: dict[str, str] = {}  # part id -> text, of the current step
        self.last_text = ""
        self.usage: Usage | None = None
        self.error: str | None = None

    def on_json(self, data: dict) -> list[Event]:
        events: list[Event] = []
        session = data.get("sessionID")
        if isinstance(session, str) and session and session != self.session:
            self.session = session
            events.append(Event("session", session_id=session))
        kind = data.get("type")
        part = data.get("part") if isinstance(data.get("part"), dict) else {}
        if kind == "text":
            text = part.get("text")
            if isinstance(text, str):
                self.texts[str(part.get("id"))] = text
        elif kind == "tool_use":
            events += self._tool(part)
        elif kind == "step_finish":
            events += self._texts()
            events += self._step(part)
        elif kind == "retry":
            error = data.get("error")
            what = error.get("message") if isinstance(error, dict) else error
            events.append(Event("info", f"retry {data.get('attempt')}: {brief(what, 500)}"))
        elif kind == "background_wait":
            jobs = data.get("jobs") if isinstance(data.get("jobs"), list) else []
            titles = [str(job.get("title")) for job in jobs if isinstance(job, dict)]
            events.append(Event("info", f"waiting for background jobs: {brief(', '.join(titles))}"))
        elif kind == "error":
            self.error = self._error(data.get("error"))
        return events

    def _texts(self) -> list[Event]:
        """The step's text parts, in the order they began."""
        texts = [text for text in self.texts.values() if text.strip()]
        self.texts = {}
        if texts:
            self.last_text = texts[-1]
        return [Event("text", text) for text in texts]

    def _step(self, part: dict) -> list[Event]:
        tokens = part.get("tokens") if isinstance(part.get("tokens"), dict) else {}
        cache = tokens.get("cache") if isinstance(tokens.get("cache"), dict) else {}

        def count(value: Any) -> int:
            return value if isinstance(value, int) and not isinstance(value, bool) else 0

        # `input` leaves out cache reads and writes (seen in 2.3.2), as Usage does.
        step = Usage(
            input_tokens=count(tokens.get("input")),
            output_tokens=count(tokens.get("output")) + count(tokens.get("reasoning")),
            cache_read_tokens=count(cache.get("read")),
            cache_creation_tokens=count(cache.get("write")),
            cost_usd=part.get("cost") if isinstance(part.get("cost"), int | float) else None,
        )
        self.usage = step if self.usage is None else self.usage + step
        size = step.input_tokens + step.cache_read_tokens + step.cache_creation_tokens
        return [Event("context", usage=Usage(context_tokens=size))] if size else []

    @staticmethod
    def _tool(part: dict) -> list[Event]:
        tool = part.get("tool")
        state = part.get("state") if isinstance(part.get("state"), dict) else {}
        given = state.get("input")
        if tool == "execute":  # Code Mode: MCP calls inside a script
            code = given.get("code") if isinstance(given, dict) else given
            named = code_servers(code)
            label = f"execute ({', '.join(named)})" if named else "execute"
            events = [Event("tool", f"{label} {brief(code)}")]
            output = state.get("output")
            # An error inside the script comes as `completed`, its text the output.
            if isinstance(output, str) and output.strip():
                try:
                    json.loads(output)
                except ValueError:
                    events.append(Event("tool_result", "error: " + brief(output)))
        else:
            events = [Event("tool", f"{tool} {brief(given)}")]
        if state.get("status") == "error":
            events.append(Event("tool_result", "error: " + brief(state.get("error"))))
        return events

    @staticmethod
    def _error(error: Any) -> str:
        """The text of an `error` line's nested `error` ({type, message, …})."""
        if not isinstance(error, dict):
            return brief(error, 2000) if error else "error"
        message, kind = error.get("message"), error.get("type")
        text = brief(message, 2000) if message else "error"
        return f"{text} ({kind})" if kind else text

    def finish(self, exit_code: int | None) -> list[Event]:
        events = self._texts()
        usage, cost = self.usage, self.usage.cost_usd if self.usage else None
        code = exit_code or 0
        if code < 0 or code in SIGNAL_EXITS:
            text = f"stopped: opencode ended by a signal (exit {exit_code})"
            return [*events, Event("result", text, cost_usd=cost, is_error=True, usage=usage)]
        if self.error or code:
            # No text: the runner tells the error by stderr or the exit code instead.
            text = self.error or ""
            return [*events, Event("result", text, cost_usd=cost, is_error=True, usage=usage)]
        structured = kit.extract_json(self.last_text) if self.structured else None
        result = Event("result", self.last_text, cost_usd=cost, structured=structured, usage=usage)
        return [*events, result]
