"""Claude Code in headless mode: `claude -p --output-format stream-json`."""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
import tempfile
import uuid
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from .. import memory_guard
from ..loadout import Loadout
from ..models import Agent, Event, Usage
from . import kit
from .base import (
    NOTIFICATION_RESULT,
    Invocation,
    RunContext,
    Support,
    Wiring,
    brief,
    wired,
)

# Set by a parent Claude Code session; a child must not believe it is that session.
PARENT_SESSION_ENV = [
    "CLAUDECODE",
    "CLAUDE_CODE_ENTRYPOINT",
    "CLAUDE_CODE_SESSION_ID",
    "CLAUDE_CODE_CHILD_SESSION",
    "CLAUDE_CODE_SESSION_ATTENDED",
    "CLAUDE_CODE_MESSAGING_SOCKET",
    "CLAUDE_CODE_MESSAGING_TOKEN",
    "CLAUDE_PID",
    "CLAUDE_EFFORT",
]

DEFAULT_PERMISSION = "auto"

# Removes `run_in_background` from the Bash tool and keeps commands in the foreground
# (verified with Claude Code 2.1.322). Set for leads: a background command keeps the CLI
# alive after its answer, and a lead must stay reachable between turns.
NO_BACKGROUND_ENV = {"CLAUDE_CODE_DISABLE_BACKGROUND_TASKS": "1"}

# The capability `chrome` (capabilities.py): Claude in Chrome's browser tools. On: the CLI's
# `--chrome`. Off: CLAUDE_CODE_ENABLE_CFC=0, so the user's own `claudeInChromeDefaultEnabled`
# does not reach agents; a CLI too old to know it ignores the variable (and `--no-chrome`
# would make it fail). Its browser tools are MCP tools (`mcp__claude-in-chrome__…`).
CHROME = Support(
    on=Wiring(argv=("--chrome",), mcp_tools=True),
    off=Wiring(env={"CLAUDE_CODE_ENABLE_CFC": "0"}),
)

# The built-in tool that loads MCP tools' schemas on demand. Claude Code defers MCP tools
# only when it is among the built-in tools: a `--tools` list without it sends every MCP
# tool's full schema with every request (verified with Claude Code 2.1.322). The CLI's
# default set has it; ENABLE_TOOL_SEARCH, when inherited, still applies as the CLI reads it.
TOOL_SEARCH = "ToolSearch"

OWN_SERVER = kit.OWN_SERVER


def config_entry(entry: Mapping[str, Any]) -> dict[str, Any]:
    """An MCP server's entry as a turn's configuration file gets it: as given, `${NAME}`
    references unexpanded (Claude Code expands them from its environment: in a stdio
    entry's command, args and env values, a remote entry's url and header values; seen
    with Claude Code 2.1.322), and without an empty `args`, `env` or `headers`."""
    return {
        key: dict(value) if isinstance(value, Mapping) else value
        for key, value in entry.items()
        if not (key in ("args", "env", "headers") and not value)
    }


def server_statuses(data: dict) -> dict[str, str] | None:
    """The MCP servers' statuses of a `system`/`init` line: {"<name>": "connected", …}
    from its `mcp_servers` [{"name", "status"}, …]; None when it has no such list."""
    listed = data.get("mcp_servers")
    if not isinstance(listed, list):
        return None
    return {
        entry["name"]: entry["status"]
        for entry in listed
        if isinstance(entry, dict)
        and isinstance(entry.get("name"), str)
        and isinstance(entry.get("status"), str)
    }


# The picker's fallback when the installed CLI cannot say what it offers.
FALLBACK_MODELS = ("fable", "opus", "sonnet", "haiku")
MODEL_LIST_TIMEOUT = 20
# A fallback or generic list is asked for again after this long; a full one is kept.
MODEL_LIST_RETRY_SECONDS = kit.MODEL_LIST_RETRY_SECONDS
UNAVAILABLE_NOTE = "model list unavailable: showing aliases"
GENERIC_NOTE = "model list may be incomplete"
_INIT_ID = "orch-models"


def request_model_list() -> str:
    """The CLI's answer to an SDK `initialize` control request (stdout, stream-json).

    The answer lists the models of the account like the `/model` picker. No user message is
    sent, so no model request is made; closing stdin ends the CLI after its answer. No user
    settings (hooks, plugins) or MCP servers are loaded and no session is saved.
    """
    request = {
        "type": "control_request",
        "request_id": _INIT_ID,
        "request": {"subtype": "initialize"},
    }
    argv = ["claude", "-p", "--input-format", "stream-json", "--output-format", "stream-json"]
    argv += ["--verbose", "--strict-mcp-config", "--setting-sources", "project,local"]
    argv += ["--no-session-persistence"]
    env = {k: v for k, v in os.environ.items() if k not in PARENT_SESSION_ENV}
    with tempfile.TemporaryDirectory(prefix="orch-models-") as cwd:
        code, out, err = kit.run_cli(
            argv, MODEL_LIST_TIMEOUT, stdin=json.dumps(request) + "\n", env=env, cwd=cwd
        )
    if code:
        raise subprocess.SubprocessError(f"exit {code}: {brief(err, 200)}")
    return out


def parse_model_list(output: str) -> list[tuple[str, str]]:
    """(value, label) pairs from the `initialize` answer; the label carries the version."""
    return [_model_choice(entry) for entry in model_entries(output)]


def model_entries(output: str) -> list[dict]:
    """The model entries of the `initialize` answer (each with a string `value`)."""
    for line in output.splitlines():
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(data, dict) or data.get("type") != "control_response":
            continue
        response = data.get("response")
        if not isinstance(response, dict):
            continue
        if response.get("request_id") != _INIT_ID:
            continue
        if response.get("subtype") != "success":
            raise ValueError(f"initialize failed: {brief(response.get('error', response), 200)}")
        answer = response.get("response")
        entries = answer.get("models") if isinstance(answer, dict) else None
        if not isinstance(entries, list):
            raise ValueError("initialize answer has no model list")
        models = [m for m in entries if isinstance(m, dict) and _text(m.get("value"))]
        if not models:
            raise ValueError("initialize answer lists no models")
        return models
    raise ValueError("no initialize answer")


def looks_generic(entries: list[dict]) -> bool:
    """The short list the CLI gives before it knows the account: no full model ids, or
    names without a version (seen: "Opus" for `opus`, "Fable" for `claude-fable-5-1[1m]`)."""
    others = [entry for entry in entries if entry["value"] != "default"]
    full_ids = any(entry["value"].startswith("claude-") for entry in others)
    unversioned = any(not _has_digit(_display_name(entry)) for entry in others)
    return not full_ids or unversioned


def _text(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""


def _has_digit(text: str) -> bool:
    return any(c.isdigit() for c in text)


def _display_name(entry: dict) -> str:
    return _text(entry.get("displayName")) or entry["value"]


def _model_choice(entry: dict) -> tuple[str, str]:
    value, label, resolved = entry["value"], _display_name(entry), _text(entry.get("resolvedModel"))
    if value == "default":
        # Not the picker's "inherit": an own `default` runs the CLI's default, whatever that
        # is at the time; the model named here is the one it stood for at lookup.
        return value, f"Claude Code default (now {resolved})" if resolved else "Claude Code default"
    if not _has_digit(label) and resolved and resolved != label:
        label = f"{label} · {resolved}"  # a name without a version: add the model it stands for
    return value, label


def _lookup_models() -> kit.Models:
    """The picker's list and its note: the aliases when the CLI cannot say."""
    try:
        entries = model_entries(request_model_list())
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        print(f"claude model list unavailable ({exc}); showing aliases", file=sys.stderr)
        return list(FALLBACK_MODELS), UNAVAILABLE_NOTE
    models = [_model_choice(entry) for entry in entries]
    return models, GENERIC_NOTE if looks_generic(entries) else None


def compaction_text(usage: Usage) -> str:
    """`context compacted (manual): 48450 -> 901 tokens` for a compaction's usage."""

    def size(value: int | None) -> str:
        return "?" if value is None else str(value)

    sizes = f"{size(usage.compact_pre_tokens)} -> {size(usage.compact_post_tokens)} tokens"
    return f"context compacted ({usage.compact_trigger}): {sizes}"


def memory_guarded(loadout: Loadout) -> bool:
    """Whether `memory` adds Write/Edit behind the guard: only where the tools grant no
    writing of their own (confining a worker's Write would break it, and the CLI's default
    set writes anywhere already)."""
    tools = loadout.tools
    return loadout.memory and tools is not None and not set(tools) & set(memory_guard.PATH_FIELDS)


def memory_settings(canary: Path) -> dict:
    """`--settings` that confine the file-writing tools to the session's memory directory,
    with a SessionStart hook that creates `canary` as proof that this session runs them.

    Hooks may be off in a session (`disableAllHooks` at any level, a policy's
    `allowManagedHooksOnly`), and that is respected: the runner sends the prompt only once
    `canary` exists, otherwise it relaunches the turn without `memory` (runner.py).
    Verified with Claude Code 2.1.322: with `--input-format stream-json` SessionStart hooks
    run before the CLI reads any input, on fresh and resumed turns; hooks from `--settings`
    load with `--setting-sources project,local`; the CLI runs commands through /bin/sh, and
    `|| exit 2` refuses the write when the guard's interpreter cannot start.
    """
    guard = shlex.join([sys.executable, "-I", memory_guard.__file__]) + " || exit 2"
    return {
        "hooks": {
            "SessionStart": [
                {"hooks": [{"type": "command", "command": shlex.join(["touch", str(canary)])}]}
            ],
            "PreToolUse": [
                {"matcher": memory_guard.MATCHER, "hooks": [{"type": "command", "command": guard}]}
            ],
        }
    }


def tool_search(
    loadout: Loadout, own_server: bool, wirings: Sequence[Wiring] = ()
) -> tuple[bool, bool]:
    """(add ToolSearch to the turn's `--tools`, keep the `orch` server's tools loaded upfront
    with `alwaysLoad`), by the loadout's `tool_search` (loadout.py). `own_server`: the turn
    has the `orch` server. Nothing is added to the CLI's default set (`tools = default`
    has ToolSearch) or to a list that names it already; `none` is a list like any other."""
    tools = loadout.tools
    if tools is None or TOOL_SEARCH in tools:
        return False, False
    brought = any(wiring.mcp_tools for wiring in wirings)
    if loadout.tool_search == "auto":
        return brought, brought and own_server
    if loadout.tool_search == "always":
        return brought or own_server, False
    return False, False


def stream_json_prompt(prompt: str) -> str:
    """The prompt as the single user message of `--input-format stream-json`."""
    message = {"role": "user", "content": [{"type": "text", "text": prompt}]}
    line = {"type": "user", "session_id": "", "message": message, "parent_tool_use_id": None}
    return json.dumps(line) + "\n"


def loadout_flags(loadout: Loadout, canary: Path | None = None, search: bool = False) -> list[str]:
    """CLI flags for a loadout. Needed on every turn: `--resume` does not remember them.

    `memory` takes effect only with a `canary` (see memory_settings) and a caller that
    waits for it before the prompt; `spartito chat` passes none, so it gets no Write/Edit.
    `search`: add ToolSearch to a restricted tool list (tool_search).
    """
    flags = []
    tools = loadout.tools
    if canary is not None and memory_guarded(loadout):
        tools = (*tools, "Write", "Edit")
        flags += ["--settings", json.dumps(memory_settings(canary))]
    if search and tools is not None and TOOL_SEARCH not in tools:
        tools = (*tools, TOOL_SEARCH)
    if tools is not None:
        flags += ["--tools", ",".join(tools)]
    if loadout.strict_mcp:
        flags.append("--strict-mcp-config")
    if loadout.setting_sources is not None:
        flags += ["--setting-sources", ",".join(loadout.setting_sources)]
    if not loadout.slash_commands:
        flags.append("--disable-slash-commands")
    return flags


class ClaudeProvider:
    name = "claude"
    efforts = ("low", "medium", "high", "xhigh", "max")
    label = "Claude Code"
    description = "Anthropic's coding agent CLI, run as claude -p --output-format stream-json"
    executable = "claude"
    version_args = ("--version",)
    login_args = None  # not checked: no check known to be offline and free of prompts
    reports_cost = True
    role_prompt = "every_turn"
    limits: tuple[str, ...] = ()
    # Facts the tables keyed by provider name hold today (roles._CREDITS, setup.CACHE_EFFECT,
    # runner.MCP_PASSED_ENV and autostart.ENV_NAMES, availability's and capabilities' checks
    # for the built-in ones), as data of the provider; nothing reads them yet.
    reports_context = True
    carries_mcp_servers = True
    credit = kit.Credit("[Claude Code](https://claude.com/claude-code)", kit.TRAILER_AS_ASKED)
    cache_effect = {"model": "restarts", "effort": "may restart"}
    config_env = ("CLAUDE_CONFIG_DIR",)

    def __init__(self) -> None:
        # A fallback or generic list (one with a note) is asked for again.
        self.catalog = kit.ModelCatalog(
            _lookup_models, retry_when=lambda models, note: note is not None
        )

    @property
    def models_note(self) -> str | None:
        """Set when the list is the fallback or looks generic."""
        return self.catalog.note

    def models(self) -> list[tuple[str, str]] | list[str]:
        """What Claude Code's own `/model` picker offers, as (value for --model, label).

        Asked from the installed CLI once per process; the aliases when that fails. A
        fallback or generic answer is asked for again once it is MODEL_LIST_RETRY_SECONDS old.
        """
        return self.catalog.models()

    def invocation(self, ctx: RunContext) -> Invocation:
        agent = ctx.agent
        argv = ["claude", "-p", "--output-format", "stream-json", "--verbose"]
        if agent.session_id:
            argv += ["--resume", agent.session_id]
        else:
            argv += ["--session-id", str(uuid.uuid4()), "--name", agent.id]
        argv += ["--permission-mode", agent.permission or DEFAULT_PERMISSION]
        # Named at random: nothing else in the session can know it in advance.
        canary = (
            ctx.scratch / f"hooks-ran-{uuid.uuid4().hex}" if memory_guarded(ctx.loadout) else None
        )
        search, keep_own = tool_search(ctx.loadout, ctx.mcp is not None, ctx.wirings)
        argv += loadout_flags(ctx.loadout, canary, search)
        if ctx.model:
            argv += ["--model", ctx.model]
        if ctx.effort:
            argv += ["--effort", ctx.effort]
        extra_argv, extra_env = wired(ctx.wirings)
        argv += extra_argv
        if ctx.system_prompt:
            argv += ["--append-system-prompt", ctx.system_prompt]
        servers = kit.turn_servers(ctx)
        if keep_own:  # a lead keeps using its own tools as without tool search
            servers[OWN_SERVER]["alwaysLoad"] = True
        brought = [name for name in servers if name != OWN_SERVER]
        private_files = {}
        if brought:
            if ctx.mcp_config is None:
                raise ValueError("a turn with MCP servers needs a path for its configuration")
            for name in brought:
                servers[name] = config_entry(servers[name])
            content = json.dumps({"mcpServers": servers})
            private_files[ctx.mcp_config] = content
            argv += ["--mcp-config", str(ctx.mcp_config)]
        elif servers:
            argv += ["--mcp-config", json.dumps({"mcpServers": servers})]
        if ctx.mcp:
            argv += ["--allowedTools", "mcp__orch"]
        if ctx.output_schema:
            argv += ["--json-schema", json.dumps(ctx.output_schema)]
        stdin = ctx.prompt
        if canary:  # read only once the runner has seen the canary
            argv += ["--input-format", "stream-json"]
            stdin = stream_json_prompt(ctx.prompt)
        return Invocation(
            argv=argv,
            stdin=stdin,
            canary=canary,
            env={**extra_env, **(NO_BACKGROUND_ENV if ctx.lead else {})},
            unset_env=PARENT_SESSION_ENV,
            private_files=private_files,
        )

    def parse(self, line: str) -> list[Event]:
        return Stream().feed(line)

    def attach_command(self, agent: Agent) -> list[str]:
        return ["claude", "--resume", agent.session_id or ""]

    @staticmethod
    def compact_prompt(instructions: str | None) -> str:
        """The CLI's own `/compact [instructions]`, on one line. It works only without
        `--disable-slash-commands` (with it the CLI answers "/compact isn't available in
        this environment." without compacting) and keeps the session id; its result is
        empty, with `num_turns: 0` (verified with Claude Code 2.1.322)."""
        words = " ".join((instructions or "").split())
        return f"/compact {words}" if words else "/compact"


class Stream(kit.JsonLines):
    """The events of Claude Code's `stream-json` output."""

    def on_json(self, data: dict) -> list[Event]:
        kind = data.get("type")
        if kind == "system" and data.get("subtype") == "init":
            session = Event("session", session_id=data.get("session_id"), model=data.get("model"))
            session.mcp = server_statuses(data)
            return [session]
        if kind == "system" and data.get("subtype") == "compact_boundary":
            return self._compaction(data)
        if kind == "assistant":
            return [*self._context(data), *self._content(data)]
        if kind == "user":
            return self._tool_results(data)
        if kind == "result":
            failed = bool(data.get("is_error")) or data.get("subtype") != "success"
            text = data.get("result") or ""
            if failed and not text:
                text = "; ".join(data.get("errors") or []) or data.get("subtype", "error")
            usage = self._usage(data)
            origin = data.get("origin")
            # A turn the CLI took for a background task notification (after its answer, or
            # on resume for a task the previous process left behind) has an origin.
            by_notification = isinstance(origin, dict) and origin.get("kind") == "task-notification"
            return [
                Event(
                    NOTIFICATION_RESULT if by_notification else "result",
                    text,
                    session_id=data.get("session_id"),
                    cost_usd=usage.cost_usd if usage else data.get("total_cost_usd"),
                    is_error=failed,
                    structured=data.get("structured_output"),
                    usage=usage,
                    windows=self._windows(data),
                )
            ]
        return []

    @staticmethod
    def _context(data: dict) -> list[Event]:
        """The conversation's size as of one model request: what the request sent (input,
        cache read, cache write) by the main chain. A subagent's messages (with a
        `parent_tool_use_id`) and the CLI's synthetic ones (all-zero usage) say nothing
        about it. `result.usage` is no measure: it sums every request of the turn.
        Verified with Claude Code 2.1.322 against its own `/context`."""
        message = data.get("message")
        if data.get("parent_tool_use_id") or not isinstance(message, dict):
            return []
        usage, model = message.get("usage"), message.get("model")
        if not isinstance(usage, dict) or model == "<synthetic>":
            return []
        keys = ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")
        tokens = sum(int(usage.get(key) or 0) for key in keys)
        if not tokens:
            return []
        return [Event("context", usage=Usage(context_tokens=tokens, model=model))]

    @staticmethod
    def _compaction(data: dict) -> list[Event]:
        """`compact_boundary`: the conversation was replaced by a summary, by `/compact`
        (trigger `manual`) or by the CLI on its own (`auto`, possibly in mid-turn)."""
        meta = data.get("compact_metadata")
        meta = meta if isinstance(meta, dict) else {}
        trigger = meta.get("trigger") if isinstance(meta.get("trigger"), str) else None
        pre, post = meta.get("pre_tokens"), meta.get("post_tokens")
        pre = pre if isinstance(pre, int) else None
        post = post if isinstance(post, int) else None
        usage = Usage(
            compact_trigger=trigger or "unknown", compact_pre_tokens=pre, compact_post_tokens=post
        )
        return [Event("compact", compaction_text(usage), usage=usage)]

    @staticmethod
    def _windows(data: dict) -> dict[str, int]:
        """The context window per model (`contextWindow`); the same for every Claude model
        so far (1M), and not the auto-compaction point, which lies 33k below it."""
        windows = {}
        for key in ("modelUsage", "model_usage_since_last_result"):
            per_model = data.get(key)
            for model, entry in per_model.items() if isinstance(per_model, dict) else ():
                window = entry.get("contextWindow") if isinstance(entry, dict) else None
                if isinstance(window, int) and window > 0:
                    windows[model] = window
        return windows

    @staticmethod
    def _usage(data: dict) -> Usage | None:
        """Tokens and cost of this invocation, summed over every model it used.

        On a resumed session `total_cost_usd` and `modelUsage` are totals for the whole
        session, while `model_usage_since_last_result` covers only this invocation (verified
        with Claude Code 2.1.322). `usage` is per invocation too but counts only the main
        model, not subagents or auxiliary models, so it would not match the cost.
        """
        per_model = data.get("model_usage_since_last_result")
        cumulative = not isinstance(per_model, dict)
        if cumulative:  # older CLIs: only the session totals
            per_model = data.get("modelUsage")
        if not isinstance(per_model, dict):
            cost = data.get("total_cost_usd")
            return None if cost is None else Usage(cost_usd=cost, cumulative=True)
        entries = [entry for entry in per_model.values() if isinstance(entry, dict)]

        def total(key: str) -> int:
            return sum(int(entry.get(key) or 0) for entry in entries)

        costs = [entry["costUSD"] for entry in entries if entry.get("costUSD") is not None]
        if not costs and cumulative:
            costs = [data["total_cost_usd"]] if data.get("total_cost_usd") is not None else []
        return Usage(
            input_tokens=total("inputTokens"),
            output_tokens=total("outputTokens"),
            cache_read_tokens=total("cacheReadInputTokens"),
            cache_creation_tokens=total("cacheCreationInputTokens"),
            cost_usd=sum(costs) if costs else None,
            model=next(iter(per_model)) if len(per_model) == 1 else None,
            cumulative=cumulative,
        )

    @staticmethod
    def _content(data: dict) -> list[Event]:
        events = []
        prefix = "[subagent] " if data.get("parent_tool_use_id") else ""
        for block in data.get("message", {}).get("content", []):
            if block.get("type") == "text" and block.get("text", "").strip():
                events.append(Event("text", prefix + block["text"]))
            elif block.get("type") == "tool_use":
                events.append(
                    Event("tool", f"{prefix}{block.get('name')} {brief(block.get('input'))}")
                )
        return events

    @staticmethod
    def _tool_results(data: dict) -> list[Event]:
        events = []
        content = data.get("message", {}).get("content", [])
        for block in content if isinstance(content, list) else []:
            if block.get("type") == "tool_result" and block.get("is_error"):
                events.append(Event("tool_result", "error: " + brief(block.get("content"))))
        return events
