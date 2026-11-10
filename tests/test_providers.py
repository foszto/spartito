from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from dataclasses import replace
from pathlib import Path

import pytest

from orchestrator.loadout import Loadout
from orchestrator.models import Agent, Event, Usage
from orchestrator.providers import McpServer, RunContext, claude, get
from orchestrator.providers.claude import loadout_flags

# The real lookup; conftest replaces the module attribute with a recorded answer.
REAL_REQUEST_MODEL_LIST = claude.request_model_list
RECORDED = Path(__file__).parent / "fixtures" / "claude_initialize.jsonl"


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


@pytest.mark.parametrize("session_id", [None, "sid"])
def test_claude_memory_adds_write_and_edit_behind_the_guard_on_every_turn(
    tmp_path: Path, session_id: str | None
) -> None:
    claude = get("claude")
    keeper = replace(LEAD_LOADOUT, memory=True)

    guarded = claude.invocation(loadout_context(tmp_path, keeper, True, session_id=session_id))
    plain = claude.invocation(loadout_context(tmp_path, LEAD_LOADOUT, True, session_id=session_id))
    argv = guarded.argv

    assert ("--resume" in argv) == bool(session_id)
    assert argv[argv.index("--tools") + 1] == "Bash,Read,Glob,Grep,Write,Edit"
    settings = json.loads(argv[argv.index("--settings") + 1])
    assert "disableAllHooks" not in settings  # a user's or project's kill switch is respected
    (entry,) = settings["hooks"]["PreToolUse"]
    assert entry["matcher"] == "Write|Edit|MultiEdit|NotebookEdit"
    command = entry["hooks"][0]["command"]
    assert command.startswith(sys.executable) and command.endswith("memory_guard.py || exit 2")
    # The canary: created by a SessionStart hook in the same settings, before any input.
    assert guarded.canary is not None and guarded.canary.parent == tmp_path
    (start,) = settings["hooks"]["SessionStart"]
    assert start["hooks"][0]["command"] == f"touch {guarded.canary}"
    assert argv[argv.index("--input-format") + 1] == "stream-json"
    message = json.loads(guarded.stdin)
    assert message["type"] == "user" and guarded.stdin.endswith("\n")
    assert message["message"]["content"] == [{"type": "text", "text": "do it"}]
    # Without memory: as before, nothing waits for a canary.
    assert plain.canary is None and plain.stdin == "do it"
    assert "--settings" not in plain.argv and "--input-format" not in plain.argv
    assert plain.argv[plain.argv.index("--tools") + 1] == "Bash,Read,Glob,Grep"


def test_claude_memory_needs_a_canary(tmp_path: Path) -> None:
    keeper = replace(LEAD_LOADOUT, memory=True)

    assert loadout_flags(keeper) == loadout_flags(LEAD_LOADOUT)  # orch chat: no Write/Edit
    assert "--settings" in loadout_flags(keeper, tmp_path / "canary")


def test_claude_memory_leaves_tools_that_already_write_alone(tmp_path: Path) -> None:
    claude = get("claude")

    worker = claude.invocation(
        loadout_context(tmp_path, replace(WORKER_LOADOUT, memory=True), False)
    ).argv
    default = claude.invocation(loadout_context(tmp_path, Loadout(memory=True), False)).argv

    assert "--settings" not in worker and "--settings" not in default
    assert worker[worker.index("--tools") + 1] == "Bash,Read,Edit,Write,Glob,Grep"
    assert "--tools" not in default


@pytest.mark.parametrize("session_id", [None, "tid"])
def test_codex_ignores_memory(tmp_path: Path, session_id: str | None) -> None:
    codex = get("codex")
    keeper = replace(LEAD_LOADOUT, memory=True)

    with_memory = codex.invocation(loadout_context(tmp_path, keeper, True, session_id=session_id))
    without = codex.invocation(loadout_context(tmp_path, LEAD_LOADOUT, True, session_id=session_id))

    assert with_memory.argv == without.argv


def test_claude_models_are_what_its_cli_offers_with_versions_in_the_labels() -> None:
    recorded = (Path(__file__).parent / "fixtures" / "claude_initialize.jsonl").read_text()

    assert claude.parse_model_list(recorded) == [
        ("default", "Claude Code default (now claude-mythos-5-2)"),
        ("opus", "Opus 5.5"),
        ("haiku", "Haiku 5.5"),
        ("claude-fable-5-1", "Fable 5.1"),
        ("claude-haiku-4-5-20251001", "Haiku 4.5"),
        ("claude-mythos-5-2", "Mythos 5.2"),
        ("claude-mythos-5-1", "Mythos 5.1"),
    ]


@pytest.mark.parametrize(
    "output",
    [
        "",
        "not json\n",
        '{"type":"control_response","response":{"subtype":"error","request_id":"orch-models",'
        '"error":"nope"}}\n',
        '{"type":"control_response","response":{"subtype":"success","request_id":"orch-models",'
        '"response":{"models":[]}}}\n',
    ],
)
def test_claude_model_list_refuses_an_unexpected_answer(output: str) -> None:
    with pytest.raises(ValueError):
        claude.parse_model_list(output)


def test_claude_models_are_looked_up_once_per_process(monkeypatch: pytest.MonkeyPatch) -> None:
    recorded = (Path(__file__).parent / "fixtures" / "claude_initialize.jsonl").read_text()
    calls = []
    monkeypatch.setattr(claude, "request_model_list", lambda: calls.append(1) or recorded)
    provider = claude.ClaudeProvider()

    lists = [provider.models() for _ in range(5)]

    assert len(calls) == 1 and lists[0] == lists[-1] and ("opus", "Opus 5.5") in lists[0]
    assert provider.models_note is None


def test_claude_models_fall_back_to_the_aliases_and_say_so(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    calls = []

    def missing() -> str:
        calls.append(1)
        raise FileNotFoundError("claude")

    monkeypatch.setattr(claude, "request_model_list", missing)
    provider = claude.ClaudeProvider()

    assert provider.models() == provider.models() == ["fable", "opus", "sonnet", "haiku"]
    assert len(calls) == 1
    assert provider.models_note == "model list unavailable: showing aliases"
    assert "claude model list unavailable" in capsys.readouterr().err


def answer_with(models: list[dict]) -> str:
    response = {"subtype": "success", "request_id": "orch-models", "response": {"models": models}}
    return json.dumps({"type": "control_response", "response": response}) + "\n"


# What the CLI answered before it knew the account (empty config dir), trimmed.
GENERIC = answer_with(
    [
        {
            "value": "default",
            "displayName": "Default (recommended)",
            "resolvedModel": "claude-opus-5-5",
        },
        {"value": "opus", "displayName": "Opus", "resolvedModel": "claude-opus-5-5"},
        {
            "value": "claude-fable-5-1[1m]",
            "displayName": "Fable",
            "resolvedModel": "claude-fable-5-1",
        },
        {"value": "haiku", "displayName": "Haiku", "resolvedModel": "claude-haiku-5-5"},
    ]
)


def test_a_generic_or_failed_claude_list_is_asked_for_again_after_a_while(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    answers = [GENERIC, None, RECORDED.read_text()]

    def lookup() -> str:
        answer = answers.pop(0)
        if answer is None:
            raise subprocess.TimeoutExpired("claude", 20)
        return answer

    monkeypatch.setattr(claude, "request_model_list", lookup)
    provider = claude.ClaudeProvider()

    generic = provider.models()
    assert provider.models() is generic and provider.models_note == "model list may be incomplete"
    assert ("opus", "Opus · claude-opus-5-5") in generic
    provider._models_at -= claude.MODEL_LIST_RETRY_SECONDS
    assert provider.models() == list(claude.FALLBACK_MODELS)
    assert provider.models_note == "model list unavailable: showing aliases"
    provider._models_at -= claude.MODEL_LIST_RETRY_SECONDS
    full = provider.models()
    assert ("claude-mythos-5-1", "Mythos 5.1") in full and provider.models_note is None
    provider._models_at -= claude.MODEL_LIST_RETRY_SECONDS
    assert provider.models() is full and not answers  # a full list is kept


@pytest.fixture
def fake_claude(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Puts a `claude` shell script with the given body first on PATH; returns where it
    writes: `pid` (its own process id) and whatever the body writes to "$OUT"."""

    def install(body: str) -> Path:
        out = tmp_path / "out"
        out.mkdir()
        script = tmp_path / "bin" / "claude"
        script.parent.mkdir()
        script.write_text(f"#!/bin/sh\nOUT={out}\necho $$ > $OUT/pid\n{body}\n")
        script.chmod(0o755)
        monkeypatch.setenv("PATH", f"{script.parent}{os.pathsep}{os.environ['PATH']}")
        return out

    monkeypatch.setattr(claude, "MODEL_LIST_TIMEOUT", 5)  # an answering script takes ~0.1 s
    return install


def gone(pid_file: Path) -> bool:
    """The process is gone (reaped; an orphan is reaped by init within moments)."""
    pid = int(pid_file.read_text())
    deadline = time.monotonic() + 1
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        time.sleep(0.02)
    return False


def test_claude_model_lookup_asks_the_cli_without_the_parent_session(
    fake_claude, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CLAUDECODE", "1")
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "parent")
    out = fake_claude(
        'read request\nprintf "%s" "$request" > $OUT/request\n'
        f'env > $OUT/env\necho "$*" > $OUT/argv\npwd > $OUT/cwd\ncat {RECORDED}'
    )

    output = REAL_REQUEST_MODEL_LIST()

    assert claude.parse_model_list(output)[-1] == ("claude-mythos-5-1", "Mythos 5.1")
    assert json.loads((out / "request").read_text())["request"] == {"subtype": "initialize"}
    env = (out / "env").read_text().splitlines()
    assert not [line for line in env if line.startswith(("CLAUDECODE=", "CLAUDE_CODE_SESSION_ID="))]
    argv = (out / "argv").read_text()
    assert "--input-format stream-json" in argv and "--no-session-persistence" in argv
    assert "orch-models-" in (out / "cwd").read_text()  # a temporary directory
    assert gone(out / "pid")


@pytest.mark.parametrize(
    ("body", "error"),
    [("exit 1", subprocess.SubprocessError), ("echo 'not json at all'", ValueError)],
)
def test_claude_model_lookup_fails_on_an_error_exit_or_garbage(
    fake_claude, body: str, error: type[Exception]
) -> None:
    out = fake_claude(body)

    with pytest.raises(error):
        claude.parse_model_list(REAL_REQUEST_MODEL_LIST())
    assert gone(out / "pid")


def start_once_up(monkeypatch: pytest.MonkeyPatch, ready: Path) -> list[float]:
    """Makes the lookup's `Popen` return only once the fake has written `ready`, so the
    timeout starts once the fake is up: a new script takes ~0.25 s to start on macOS, and
    a CLI killed before it did anything proves nothing. Returns [when it was up]."""
    up: list[float] = []

    class Started(subprocess.Popen):
        def __init__(self, *args, **kwargs) -> None:
            super().__init__(*args, **kwargs)
            deadline = time.monotonic() + 10
            while not (ready.exists() and ready.read_text().endswith("\n")):
                assert time.monotonic() < deadline, "the fake CLI did not start"
                time.sleep(0.005)
            up.append(time.monotonic())

    monkeypatch.setattr(claude.subprocess, "Popen", Started)
    return up


def test_claude_model_lookup_kills_a_cli_that_does_not_answer(
    fake_claude, monkeypatch: pytest.MonkeyPatch
) -> None:
    out = fake_claude("sleep 30 &\necho $! > $OUT/child\nexec sleep 30")
    monkeypatch.setattr(claude, "MODEL_LIST_TIMEOUT", 0.3)
    up = start_once_up(monkeypatch, out / "child")

    with pytest.raises(subprocess.TimeoutExpired):
        REAL_REQUEST_MODEL_LIST()

    assert time.monotonic() - up[0] < 1
    assert gone(out / "pid") and gone(out / "child")  # its children too


def test_claude_model_lookup_survives_a_cli_that_exits_as_it_is_killed(
    fake_claude, monkeypatch: pytest.MonkeyPatch
) -> None:
    out = fake_claude("read go < $OUT/go")  # exits only when the test lets it
    os.mkfifo(out / "go")
    monkeypatch.setattr(claude, "MODEL_LIST_TIMEOUT", 0.3)
    start_once_up(monkeypatch, out / "pid")
    killpg = os.killpg

    def late_killpg(pgid: int, sig: int) -> None:  # the CLI exits just at the timeout
        os.close(os.open(out / "go", os.O_WRONLY))  # EOF: the fake exits
        os.waitid(os.P_PID, pgid, os.WEXITED | os.WNOWAIT)  # exited, not reaped
        killpg(pgid, sig)  # macOS: PermissionError for a group of only unreaped processes

    monkeypatch.setattr(claude.os, "killpg", late_killpg)

    with pytest.raises(subprocess.TimeoutExpired):
        REAL_REQUEST_MODEL_LIST()

    assert gone(out / "pid")  # reaped
