"""Claude Code in headless mode: `claude -p --output-format stream-json`."""

from __future__ import annotations

import json
import shlex
import sys
import uuid
from pathlib import Path

from .. import memory_guard
from ..loadout import Loadout
from ..models import Agent, Event, Usage
from .base import NOTIFICATION_RESULT, Invocation, RunContext, brief

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


def stream_json_prompt(prompt: str) -> str:
    """The prompt as the single user message of `--input-format stream-json`."""
    message = {"role": "user", "content": [{"type": "text", "text": prompt}]}
    line = {"type": "user", "session_id": "", "message": message, "parent_tool_use_id": None}
    return json.dumps(line) + "\n"


def loadout_flags(loadout: Loadout, canary: Path | None = None) -> list[str]:
    """CLI flags for a loadout. Needed on every turn: `--resume` does not remember them.

    `memory` takes effect only with a `canary` (see memory_settings) and a caller that
    waits for it before the prompt; `orch chat` passes none, so it gets no Write/Edit.
    """
    flags = []
    tools = loadout.tools
    if canary is not None and memory_guarded(loadout):
        tools = (*tools, "Write", "Edit")
        flags += ["--settings", json.dumps(memory_settings(canary))]
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

    def models(self) -> list[str]:
        return ["fable", "opus", "sonnet", "haiku"]

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
        argv += loadout_flags(ctx.loadout, canary)
        if ctx.model:
            argv += ["--model", ctx.model]
        if ctx.effort:
            argv += ["--effort", ctx.effort]
        if ctx.system_prompt:
            argv += ["--append-system-prompt", ctx.system_prompt]
        if ctx.mcp:
            servers = {
                "orch": {"command": ctx.mcp.command, "args": ctx.mcp.args, "env": ctx.mcp.env}
            }
            argv += ["--mcp-config", json.dumps({"mcpServers": servers})]
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
            env=dict(NO_BACKGROUND_ENV) if ctx.lead else {},
            unset_env=PARENT_SESSION_ENV,
        )

    def parse(self, line: str) -> list[Event]:
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            return [Event("info", line.strip())] if line.strip() else []
        kind = data.get("type")
        if kind == "system" and data.get("subtype") == "init":
            return [Event("session", session_id=data.get("session_id"), model=data.get("model"))]
        if kind == "assistant":
            return self._content(data)
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
                )
            ]
        return []

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

    def attach_command(self, agent: Agent) -> list[str]:
        return ["claude", "--resume", agent.session_id or ""]
