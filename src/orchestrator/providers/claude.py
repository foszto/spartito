"""Claude Code in headless mode: `claude -p --output-format stream-json`."""

from __future__ import annotations

import json
import uuid

from ..models import Agent, Event
from .base import Invocation, RunContext, brief

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
        return Invocation(argv=argv, stdin=ctx.prompt, unset_env=PARENT_SESSION_ENV)

    def parse(self, line: str) -> list[Event]:
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            return [Event("info", line.strip())] if line.strip() else []
        kind = data.get("type")
        if kind == "system" and data.get("subtype") == "init":
            return [Event("session", session_id=data.get("session_id"))]
        if kind == "assistant":
            return self._content(data)
        if kind == "user":
            return self._tool_results(data)
        if kind == "result":
            failed = bool(data.get("is_error")) or data.get("subtype") != "success"
            text = data.get("result") or ""
            if failed and not text:
                text = "; ".join(data.get("errors") or []) or data.get("subtype", "error")
            return [
                Event(
                    "result",
                    text,
                    session_id=data.get("session_id"),
                    cost_usd=data.get("total_cost_usd"),
                    is_error=failed,
                    structured=data.get("structured_output"),
                )
            ]
        return []

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
