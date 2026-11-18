"""Codex in headless mode: `codex exec --json`.

Options go through `-c key=value` overrides rather than flags because
`codex exec resume` accepts only a subset of the flags of `codex exec`.
"""

from __future__ import annotations

import json
import os
import subprocess
from datetime import UTC, datetime
from pathlib import Path

from ..models import Agent, Event, Usage
from . import kit
from .base import Invocation, RunContext, brief, wired

DEFAULT_SANDBOX = "workspace-write"
ROLLOUT_TAIL_BYTES = 2 << 20  # where the last token_count of a rollout file is looked for
# `/compact` does not work in `codex exec resume` (the model only reads it); a turn with a
# lower model_auto_compact_token_limit compacts at its start. The model sees this prompt.
COMPACT_PROMPT = (
    "[orch] This turn only compacts the conversation; there is nothing to do."
    " Reply with the single word: compacted"
)
MODEL_LIST_TIMEOUT = 15


def request_model_list() -> str:
    """The output of `codex debug models`: the models the installed Codex offers for this
    account, as JSON. Raises OSError or subprocess.SubprocessError."""
    return kit.run_cli(["codex", "debug", "models"], MODEL_LIST_TIMEOUT)[1]


def _lookup_models() -> kit.Models:
    """The listed models; none when the CLI is missing or fails."""
    try:
        return parse_model_list(request_model_list()), None
    except OSError, subprocess.SubprocessError:
        return [], None


def _toml(value: object) -> str:
    """JSON scalars, strings and arrays are valid TOML; tables need inline syntax."""
    if isinstance(value, dict):
        return "{" + ", ".join(f"{key} = {_toml(item)}" for key, item in value.items()) + "}"
    return json.dumps(value)


def parse_model_list(output: str) -> list[str]:
    """The listed model slugs of `codex debug models`; [] for anything unexpected."""
    try:
        catalog = json.loads(output)
    except json.JSONDecodeError:
        return []
    entries = catalog.get("models", []) if isinstance(catalog, dict) else catalog
    if not isinstance(entries, list):
        return []
    return [
        m["slug"]
        for m in entries
        if isinstance(m, dict) and m.get("slug") and m.get("visibility") == "list"
    ]


class CodexProvider:
    name = "codex"
    efforts = ("low", "medium", "high", "xhigh")
    compact_needs_size = True  # compaction lowers model_auto_compact_token_limit below it
    label = "Codex"
    description = "OpenAI's coding agent CLI, run as codex exec --json"
    executable = "codex"
    version_args = ("--version",)
    login_args = None  # not checked: no check known to be offline and free of prompts
    reports_cost = False
    role_prompt = "first_turn"
    limits = (
        "reports no cost: budgets do not count its turns",
        "gets its role prompt and attribution only in the first turn",
        "compacts without instructions, once its context size is known",
        "of the loadout only `codex_user_config` applies: `tools` and `memory` do not restrict it",
    )
    # Facts the tables keyed by provider name hold today (roles._CREDITS, setup.CACHE_EFFECT,
    # runner.MCP_PASSED_ENV and autostart.ENV_NAMES, availability's and capabilities' checks
    # for the built-in ones), as data of the provider; nothing reads them yet.
    reports_context = True
    carries_mcp_servers = False
    credit = kit.Credit("[Codex](https://github.com/openai/codex)", kit.TRAILER_IF_ADDED)
    cache_effect = {"model": "restarts", "effort": "keeps"}
    config_env = ("CODEX_HOME",)

    # An empty list (the CLI missing or failing) is asked for again.
    catalog = kit.ModelCatalog(_lookup_models, retry_when=lambda models, note: not models)

    def models(self) -> list[str]:
        """The models the installed Codex offers for this account. Asked once per process;
        an empty answer is asked for again once it is kit.MODEL_LIST_RETRY_SECONDS old."""
        return self.catalog.models(self)

    def invocation(self, ctx: RunContext) -> Invocation:
        agent = ctx.agent
        argv = ["codex", "exec"]
        if agent.session_id:
            argv += ["resume", agent.session_id]
        argv += ["--json", "--skip-git-repo-check"]
        if not ctx.loadout.codex_user_config:
            argv.append("--ignore-user-config")  # accepted by `exec resume` too
        argv += ["-c", f"sandbox_mode={_toml(agent.permission or DEFAULT_SANDBOX)}"]
        if ctx.writable_dirs:
            argv += ["-c", f"sandbox_workspace_write.writable_roots={_toml(ctx.writable_dirs)}"]
        if ctx.model:
            argv += ["-m", ctx.model]
        if ctx.effort:
            argv += ["-c", f"model_reasoning_effort={_toml(ctx.effort)}"]
        if ctx.mcp:
            own = kit.own_server_entry(ctx.mcp)
            for key in ("command", "args", "env"):
                argv += ["-c", f"mcp_servers.orch.{key}={_toml(own[key])}"]
            # Waiting on sub-agents is a long tool call.
            argv += ["-c", "mcp_servers.orch.tool_timeout_sec=3600"]
            # Without the user's approvals_reviewer, exec rejects MCP calls that need approval.
            argv += ["-c", 'mcp_servers.orch.default_tools_approval_mode="approve"']
        if ctx.compact_below:
            argv += ["-c", f"model_auto_compact_token_limit={int(ctx.compact_below)}"]
        if ctx.output_schema:
            argv += ["--output-schema", str(kit.schema_file(ctx))]
        extra_argv, extra_env = wired(ctx.wirings)  # no capability has a Codex wiring yet
        argv += extra_argv
        argv.append("-")
        # Codex has no system-prompt flag for exec; the role opens the first turn instead.
        return Invocation(argv=argv, stdin=kit.with_role_prefix(ctx), env=extra_env)

    def parse(self, line: str) -> list[Event]:
        return Stream().feed(line)

    def attach_command(self, agent: Agent) -> list[str]:
        return ["codex", "resume", agent.session_id or ""]

    @staticmethod
    def compact_prompt(instructions: str | None) -> str:
        if instructions:
            raise ValueError("Codex compacts on its own terms: instructions are not supported")
        return COMPACT_PROMPT

    @staticmethod
    def context_after(session_id: str, since: str) -> Usage | None:
        """The conversation's size from the thread's rollout file (the `--json` stream has
        no window, and its usage is the thread total): the last `token_count` event's
        `last_token_usage.input_tokens` (cached tokens included) and
        `model_context_window` (the model's window less Codex's reserve, e.g. 95% of
        272000); a `compacted` entry after it leaves the size unknown (the `token_count`
        a compaction writes has `input_tokens` 0 and is no measure). Compactions written
        at or after `since` are reported too. The file format is not documented (seen with
        codex-cli 0.155.1): anything unexpected gives None."""
        path = rollout_file(session_id)
        if path is None:
            return None
        try:
            with path.open("rb") as file:
                file.seek(0, os.SEEK_END)
                file.seek(max(0, file.tell() - ROLLOUT_TAIL_BYTES))
                lines = file.read().splitlines()
        except OSError:
            return None
        start = _when(since)
        usage, compactions, sized = Usage(), 0, False
        for line in reversed(lines):
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            if not isinstance(entry, dict):
                continue
            payload = entry.get("payload")
            payload = payload if isinstance(payload, dict) else {}
            when = _when(entry.get("timestamp"))
            recent = start is not None and when is not None and when >= start
            if entry.get("type") == "compacted":
                if recent:
                    compactions += 1
                sized = True  # the size before this compaction is no longer the size
            elif not sized and payload.get("type") == "token_count":
                info = payload.get("info")
                info = info if isinstance(info, dict) else {}
                last = info.get("last_token_usage")
                tokens = last.get("input_tokens") if isinstance(last, dict) else None
                window = info.get("model_context_window")
                if isinstance(tokens, int) and tokens > 0:  # 0: written by a compaction
                    usage.context_tokens = tokens
                    usage.context_window = window if isinstance(window, int) else None
                    sized = True
            if sized and not recent:
                break
        if compactions:
            usage.compact_trigger = "auto"
        if usage.context_tokens is None and not compactions:
            return None
        return usage


class Stream(kit.JsonLines):
    """The events of `codex exec --json`."""

    def on_json(self, data: dict) -> list[Event]:
        kind = data.get("type")
        if kind == "thread.started":
            return [Event("session", session_id=data.get("thread_id"))]
        if kind == "item.completed":
            return self._item(data.get("item", {}))
        if kind == "turn.completed":
            # The text is the last agent_message; the runner keeps it.
            return [Event("result", usage=self._usage(data.get("usage")))]
        if kind == "turn.failed":
            return [
                Event("result", brief(data.get("error", {}).get("message"), 2000), is_error=True)
            ]
        if kind == "error":
            return [Event("error", brief(data.get("message"), 2000))]
        return []

    @staticmethod
    def _usage(usage: object) -> Usage | None:
        """Token counts of `turn.completed`. Codex reports no cost.

        On `codex exec resume` the figures are totals for the whole thread, not for this
        turn (verified with codex-cli 0.155.1), so they are marked cumulative. Codex counts
        cached tokens inside `input_tokens`; they are moved out to match Claude's meaning.
        """
        if not isinstance(usage, dict) or not usage:
            return None
        cached = int(usage.get("cached_input_tokens") or 0)
        return Usage(
            input_tokens=int(usage.get("input_tokens") or 0) - cached,
            output_tokens=int(usage.get("output_tokens") or 0),
            cache_read_tokens=cached,
            cache_creation_tokens=int(usage.get("cache_write_input_tokens") or 0),
            cumulative=True,
        )

    @staticmethod
    def _item(item: dict) -> list[Event]:
        kind = item.get("type")
        if kind == "agent_message":
            return [Event("text", item.get("text", ""))]
        if kind == "command_execution":
            return [
                Event("tool", f"shell {brief(item.get('command'))} -> exit {item.get('exit_code')}")
            ]
        if kind == "file_change":
            changes = ", ".join(
                f"{change.get('kind')} {change.get('path')}" for change in item.get("changes", [])
            )
            return [Event("tool", f"edit {changes}")]
        if kind == "mcp_tool_call":
            return [
                Event(
                    "tool",
                    f"{item.get('server')}.{item.get('tool')} {brief(item.get('arguments'))}",
                )
            ]
        if kind == "error":
            return [Event("info", brief(item.get("message"), 1000))]
        return []


def _when(stamp: object) -> datetime | None:
    if not isinstance(stamp, str):
        return None
    try:
        when = datetime.fromisoformat(stamp)
    except ValueError:
        return None
    return when if when.tzinfo else when.replace(tzinfo=UTC)


def codex_home() -> Path:
    return Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")


def rollout_file(thread_id: str) -> Path | None:
    """`$CODEX_HOME/sessions/YYYY/MM/DD/rollout-<time>-<thread id>.jsonl`, if it exists."""
    if not thread_id or "/" in thread_id or "*" in thread_id:
        return None
    try:
        found = sorted((codex_home() / "sessions").glob(f"*/*/*/rollout-*-{thread_id}.jsonl"))
    except OSError:
        return None
    return found[-1] if found else None
