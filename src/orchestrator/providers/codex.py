"""Codex in headless mode: `codex exec --json`.

Options go through `-c key=value` overrides rather than flags because
`codex exec resume` accepts only a subset of the flags of `codex exec`.
"""

from __future__ import annotations

import functools
import json
import subprocess

from ..models import Agent, Event
from .base import Invocation, RunContext, brief

DEFAULT_SANDBOX = "workspace-write"


def _toml(value: object) -> str:
    """JSON scalars, strings and arrays are valid TOML; tables need inline syntax."""
    if isinstance(value, dict):
        return "{" + ", ".join(f"{key} = {_toml(item)}" for key, item in value.items()) + "}"
    return json.dumps(value)


class CodexProvider:
    name = "codex"
    efforts = ("low", "medium", "high", "xhigh")

    @functools.cache  # noqa: B019 - one provider instance per process
    def models(self) -> list[str]:
        """The models the installed Codex offers for this account."""
        try:
            proc = subprocess.run(
                ["codex", "debug", "models"], capture_output=True, text=True, timeout=15
            )
            catalog = json.loads(proc.stdout)
        except OSError, subprocess.SubprocessError, json.JSONDecodeError:
            return []
        entries = catalog.get("models", []) if isinstance(catalog, dict) else catalog
        return [m["slug"] for m in entries if m.get("slug") and m.get("visibility") == "list"]

    def invocation(self, ctx: RunContext) -> Invocation:
        agent = ctx.agent
        argv = ["codex", "exec"]
        if agent.session_id:
            argv += ["resume", agent.session_id]
        argv += ["--json", "--skip-git-repo-check"]
        argv += ["-c", f"sandbox_mode={_toml(agent.permission or DEFAULT_SANDBOX)}"]
        if ctx.writable_dirs:
            argv += ["-c", f"sandbox_workspace_write.writable_roots={_toml(ctx.writable_dirs)}"]
        if ctx.model:
            argv += ["-m", ctx.model]
        if ctx.effort:
            argv += ["-c", f"model_reasoning_effort={_toml(ctx.effort)}"]
        if ctx.mcp:
            argv += ["-c", f"mcp_servers.orch.command={_toml(ctx.mcp.command)}"]
            argv += ["-c", f"mcp_servers.orch.args={_toml(ctx.mcp.args)}"]
            argv += ["-c", f"mcp_servers.orch.env={_toml(ctx.mcp.env)}"]
            # Waiting on sub-agents is a long tool call.
            argv += ["-c", "mcp_servers.orch.tool_timeout_sec=3600"]
        if ctx.output_schema:
            schema_file = ctx.scratch / "output-schema.json"
            schema_file.write_text(json.dumps(ctx.output_schema), encoding="utf-8")
            argv += ["--output-schema", str(schema_file)]
        argv.append("-")
        # Codex has no system-prompt flag for exec; the role opens the first turn instead.
        prompt = ctx.prompt
        if ctx.system_prompt and not agent.session_id:
            prompt = f"{ctx.system_prompt}\n\n---\n\n{ctx.prompt}"
        return Invocation(argv=argv, stdin=prompt)

    def parse(self, line: str) -> list[Event]:
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            return [Event("info", line.strip())] if line.strip() else []
        kind = data.get("type")
        if kind == "thread.started":
            return [Event("session", session_id=data.get("thread_id"))]
        if kind == "item.completed":
            return self._item(data.get("item", {}))
        if kind == "turn.completed":
            return [Event("result")]  # the text is the last agent_message; the runner keeps it
        if kind == "turn.failed":
            return [
                Event("result", brief(data.get("error", {}).get("message"), 2000), is_error=True)
            ]
        if kind == "error":
            return [Event("error", brief(data.get("message"), 2000))]
        return []

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

    def attach_command(self, agent: Agent) -> list[str]:
        return ["codex", "resume", agent.session_id or ""]
