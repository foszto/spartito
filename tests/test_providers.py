from __future__ import annotations

import json
from pathlib import Path

from orchestrator.models import Agent
from orchestrator.providers import McpServer, RunContext, get


def context(tmp_path: Path, **agent_fields) -> RunContext:
    agent = Agent(
        id="a-1", project="p", name="a", role="worker", provider="x", cwd="/w", **agent_fields
    )
    return RunContext(agent=agent, prompt="do it", system_prompt="ROLE", scratch=tmp_path)


def test_claude_starts_a_session_then_resumes_it(tmp_path: Path) -> None:
    claude = get("claude")

    first = claude.invocation(context(tmp_path)).argv
    later = claude.invocation(context(tmp_path, session_id="sid")).argv

    assert "--session-id" in first and "--resume" not in first
    assert later[later.index("--resume") + 1] == "sid" and "--session-id" not in later
    assert first[first.index("--append-system-prompt") + 1] == "ROLE"


def test_claude_lead_gets_the_orchestrator_mcp_server(tmp_path: Path) -> None:
    ctx = context(tmp_path)
    ctx.mcp = McpServer("py", ["-m", "x"], {"ORCH_AGENT_ID": "a-1"})

    argv = get("claude").invocation(ctx).argv

    config = json.loads(argv[argv.index("--mcp-config") + 1])
    assert config["mcpServers"]["orch"]["env"] == {"ORCH_AGENT_ID": "a-1"}


def test_claude_parses_the_stream() -> None:
    claude = get("claude")
    assistant = {"type": "assistant", "message": {"content": [
        {"type": "text", "text": "hello"},
        {"type": "tool_use", "name": "Bash", "input": {"command": "ls"}},
    ]}}  # fmt: skip
    result = {"type": "result", "subtype": "success", "is_error": False, "result": "done",
              "total_cost_usd": 0.5, "session_id": "sid", "structured_output": {"ok": True}}  # fmt: skip

    (session,) = claude.parse(
        json.dumps({"type": "system", "subtype": "init", "session_id": "sid"})
    )
    text, tool = claude.parse(json.dumps(assistant))
    (final,) = claude.parse(json.dumps(result))
    (failed,) = claude.parse(
        json.dumps({"type": "result", "subtype": "error_max_turns", "is_error": True})
    )

    assert (session.kind, session.session_id) == ("session", "sid")
    assert (text.kind, text.text) == ("text", "hello")
    assert tool.kind == "tool" and tool.text.startswith("Bash ")
    assert (final.text, final.cost_usd, final.structured, final.is_error) == (
        "done",
        0.5,
        {"ok": True},
        False,
    )
    assert failed.is_error and failed.text == "error_max_turns"


def test_codex_resume_puts_the_role_only_in_the_first_turn(tmp_path: Path) -> None:
    codex = get("codex")

    first = codex.invocation(context(tmp_path))
    later = codex.invocation(context(tmp_path, session_id="tid"))

    assert first.argv[:2] == ["codex", "exec"] and "resume" not in first.argv
    assert first.stdin.startswith("ROLE") and first.stdin.endswith("do it")
    assert later.argv[:4] == ["codex", "exec", "resume", "tid"] and later.stdin == "do it"


def test_codex_can_write_the_shared_git_directory_of_a_worktree(tmp_path: Path) -> None:
    ctx = context(tmp_path)
    ctx.writable_dirs = ["/repo/.git"]

    argv = get("codex").invocation(ctx).argv

    assert 'sandbox_workspace_write.writable_roots=["/repo/.git"]' in argv
    assert 'sandbox_mode="workspace-write"' in argv


def test_codex_parses_the_stream() -> None:
    codex = get("codex")

    (session,) = codex.parse('{"type":"thread.started","thread_id":"tid"}')
    (text,) = codex.parse('{"type":"item.completed","item":{"type":"agent_message","text":"hi"}}')
    (shell,) = codex.parse(
        '{"type":"item.completed","item":{"type":"command_execution","command":"ls","exit_code":0}}'
    )
    (done,) = codex.parse('{"type":"turn.completed","usage":{}}')
    (failed,) = codex.parse('{"type":"turn.failed","error":{"message":"model_not_found"}}')

    assert session.session_id == "tid"
    assert (text.kind, text.text) == ("text", "hi")
    assert shell.text == "shell ls -> exit 0"
    assert done.kind == "result" and not done.is_error
    assert failed.is_error and failed.text == "model_not_found"
