from __future__ import annotations

import json
from pathlib import Path

import pytest

from orchestrator.loadout import Loadout
from orchestrator.models import Agent, Event, Usage
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


FIXTURES = Path(__file__).parent / "fixtures"


def parsed(provider: str, fixture: str) -> tuple[Event, Event]:
    """The session and result events of a captured stream (trimmed real CLI output)."""
    lines = (FIXTURES / fixture).read_text().splitlines()
    session, result = (event for line in lines for event in get(provider).parse(line))
    return session, result


def tokens(usage: Usage) -> tuple:
    return (
        usage.input_tokens,
        usage.output_tokens,
        usage.cache_read_tokens,
        usage.cache_creation_tokens,
    )


def test_claude_reports_the_usage_of_this_invocation_also_when_resumed() -> None:
    session, fresh = parsed("claude", "claude_fresh.jsonl")
    _, resumed = parsed("claude", "claude_resumed.jsonl")

    assert session.model == "claude-haiku-5-5"
    assert tokens(fresh.usage) == (2, 4, 11873, 17855) and fresh.usage.cost_usd == 0.00369193
    # total_cost_usd of the resumed line is 0.00689838: the whole session, not this run.
    assert tokens(resumed.usage) == (2, 4, 14945, 15274) and resumed.usage.cost_usd == 0.00320645
    assert resumed.cost_usd == 0.00320645
    assert not fresh.usage.cumulative and not resumed.usage.cumulative
    assert resumed.usage.model == "claude-haiku-5-5"


def test_claude_without_a_per_invocation_breakdown_reports_session_totals() -> None:
    lines = (FIXTURES / "claude_resumed.jsonl").read_text().splitlines()
    result = json.loads(lines[-1])
    del result["model_usage_since_last_result"]
    _, fresh = parsed("claude", "claude_fresh.jsonl")

    (event,) = get("claude").parse(json.dumps(result))
    own = event.usage.since(fresh.usage)

    assert event.usage.cumulative and event.usage.cost_usd == 0.00689838
    assert tokens(own) == (2, 4, 14945, 15274) and own.cost_usd == 0.00320645


def test_claude_sums_every_model_of_the_invocation() -> None:
    result = {
        "type": "result",
        "subtype": "success",
        "total_cost_usd": 9.0,  # session total, ignored
        "model_usage_since_last_result": {
            "claude-opus-5-5": {"inputTokens": 10, "outputTokens": 5, "costUSD": 0.5},
            "claude-haiku-5-5": {"inputTokens": 3, "cacheReadInputTokens": 7, "costUSD": 0.25},
        },
    }

    (event,) = get("claude").parse(json.dumps(result))

    assert tokens(event.usage) == (13, 5, 7, 0) and event.usage.cost_usd == 0.75
    assert event.usage.model is None  # the runner takes the model from the init event


def test_codex_reports_thread_totals_that_become_per_turn_figures() -> None:
    session, fresh = parsed("codex", "codex_fresh.jsonl")
    _, resumed = parsed("codex", "codex_resumed.jsonl")

    own = resumed.usage.since(fresh.usage)

    assert session.session_id == "01a1c566-fba4-77d2-8d4a-fd3d93931e39"
    assert fresh.usage.cumulative and resumed.usage.cumulative
    # Codex counts cached tokens inside input_tokens (30221 = 15245 + 14976).
    assert tokens(fresh.usage) == (15101, 5, 0, 0)
    assert tokens(resumed.usage) == (15245, 10, 14976, 0)
    assert tokens(own) == (144, 5, 14976, 0)
    assert fresh.usage.cost_usd is None and own.cost_usd is None


LEAD_LOADOUT = Loadout(
    tools=("Bash", "Read", "Glob", "Grep"),
    strict_mcp=True,
    setting_sources=("project", "local"),
    codex_user_config=False,
)
WORKER_LOADOUT = Loadout(
    tools=("Bash", "Read", "Edit", "Write", "Glob", "Grep"),
    strict_mcp=True,
    setting_sources=("project", "local"),
    slash_commands=False,
    codex_user_config=False,
)


def loadout_context(tmp_path: Path, loadout: Loadout, lead: bool, **agent_fields) -> RunContext:
    ctx = context(tmp_path, **agent_fields)
    ctx.loadout = loadout
    if lead:
        ctx.mcp = McpServer("py", ["-m", "x"], {"ORCH_AGENT_ID": "a-1"})
    return ctx


@pytest.mark.parametrize("session_id", [None, "sid"])
def test_claude_passes_the_loadout_on_fresh_and_resumed_turns(
    tmp_path: Path, session_id: str | None
) -> None:
    claude = get("claude")

    lead = claude.invocation(loadout_context(tmp_path, LEAD_LOADOUT, True, session_id=session_id))
    worker = claude.invocation(
        loadout_context(tmp_path, WORKER_LOADOUT, False, session_id=session_id)
    )

    assert ("--resume" in lead.argv) == bool(session_id)
    assert lead.argv[lead.argv.index("--tools") + 1] == "Bash,Read,Glob,Grep"
    assert "--strict-mcp-config" in lead.argv and "--mcp-config" in lead.argv
    assert lead.argv[lead.argv.index("--allowedTools") + 1] == "mcp__orch"
    assert lead.argv[lead.argv.index("--setting-sources") + 1] == "project,local"
    assert "--disable-slash-commands" not in lead.argv
    assert worker.argv[worker.argv.index("--tools") + 1] == "Bash,Read,Edit,Write,Glob,Grep"
    assert "--strict-mcp-config" in worker.argv and "--mcp-config" not in worker.argv
    assert "--disable-slash-commands" in worker.argv


def test_claude_inherit_and_default_values_restrict_nothing(tmp_path: Path) -> None:
    inherit = Loadout(tools=None, strict_mcp=False, setting_sources=None)
    none = Loadout(tools=())

    argv = get("claude").invocation(loadout_context(tmp_path, inherit, True)).argv
    no_tools = get("claude").invocation(loadout_context(tmp_path, none, False)).argv

    for flag in ("--tools", "--strict-mcp-config", "--setting-sources", "--disable-slash-commands"):
        assert flag not in argv
    assert no_tools[no_tools.index("--tools") + 1] == ""


@pytest.mark.parametrize("session_id", [None, "tid"])
def test_codex_ignores_the_user_config_on_fresh_and_resumed_turns(
    tmp_path: Path, session_id: str | None
) -> None:
    codex = get("codex")

    lead = codex.invocation(loadout_context(tmp_path, LEAD_LOADOUT, True, session_id=session_id))
    worker = codex.invocation(
        loadout_context(tmp_path, WORKER_LOADOUT, False, session_id=session_id)
    )
    inherit = codex.invocation(loadout_context(tmp_path, Loadout(), False, session_id=session_id))

    assert ("resume" in lead.argv) == bool(session_id)
    assert "--ignore-user-config" in lead.argv and "--ignore-user-config" in worker.argv
    assert "--ignore-user-config" not in inherit.argv
    approve = 'mcp_servers.orch.default_tools_approval_mode="approve"'
    assert approve in lead.argv and approve not in " ".join(worker.argv)
    for argv in (lead.argv, worker.argv):
        assert "--tools" not in argv and "--strict-mcp-config" not in argv  # Claude-only keys


def test_codex_puts_the_loadout_options_after_the_resumed_thread(tmp_path: Path) -> None:
    argv = (
        get("codex")
        .invocation(loadout_context(tmp_path, LEAD_LOADOUT, True, session_id="tid"))
        .argv
    )

    thread = argv.index("resume") + 1
    assert argv[thread] == "tid"
    assert argv.index("--ignore-user-config") > thread
    assert argv.index('mcp_servers.orch.default_tools_approval_mode="approve"') > thread
