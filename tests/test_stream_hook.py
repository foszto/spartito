"""The per-turn stream hook (`Provider.stream()`), `Invocation.notes` and the variables
passed on to the orchestrator's MCP server. The plugin providers are in streamprovider.py."""

from __future__ import annotations

import json

import pytest
from streamprovider import NOTE, HeldBackProvider, HomeProvider, NotingProvider

from orchestrator import capabilities, providers, runner
from orchestrator.models import Agent, Event, RunStatus
from orchestrator.providers.base import ParsedLines, turn_stream
from orchestrator.providers.claude import ClaudeProvider
from orchestrator.providers.codex import CodexProvider


@pytest.fixture
def plugins(orch, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(
        "ORCH_PROVIDERS",
        "fake=fakeprovider:FakeProvider,held-back=streamprovider:HeldBackProvider,"
        "noting=streamprovider:NotingProvider",
    )
    monkeypatch.setitem(providers.PROVIDERS, "held-back", HeldBackProvider())
    monkeypatch.setitem(providers.PROVIDERS, "noting", NotingProvider())


# -- what finish reports counts ----------------------------------------------------------------


def test_a_result_held_back_until_the_end_of_the_stream_answers_the_turn(
    orch, project: str, plugins: None
) -> None:
    agent, run = orch.spawn(project, "one two", worktree=False, provider="held-back")
    assert orch.wait([agent.id], timeout=60)[0]

    finished = orch.store.get_run(run.id)
    assert (finished.status, finished.result) == (RunStatus.DONE, "one two")
    assert orch.store.get_agent(agent.id).session_id == "held-back-session"
    texts = [(e.kind, e.text) for e in orch.store.events(agent.id) if e.run_id == run.id]
    assert texts[-3:] == [("text", "one"), ("text", "two"), ("result", "one two")]


def test_an_error_reported_at_the_end_fails_the_turn(orch, project: str, plugins: None) -> None:
    agent, run = orch.spawn(project, "one two EXIT3", worktree=False, provider="held-back")
    assert orch.wait([agent.id], timeout=60)[0]

    finished = orch.store.get_run(run.id)
    assert finished.status is RunStatus.FAILED
    assert finished.error == "exit code 3 after 2 lines"


def test_each_turn_has_a_stream_of_its_own(orch, project: str, plugins: None) -> None:
    agent, _ = orch.spawn(project, "one two", worktree=False, provider="held-back")
    assert orch.wait([agent.id], timeout=60)[0]
    second = orch.send(agent.id, "three four")
    assert orch.wait([agent.id], timeout=60)[0]

    finished = orch.store.get_run(second.id)
    assert (finished.status, finished.result) == (RunStatus.DONE, "three four")


# -- a provider without stream() --------------------------------------------------------------


class ParseOnly:
    def parse(self, line: str) -> list[Event]:
        return [Event("text", line.upper())]


def test_without_stream_each_line_goes_through_parse_and_the_end_reports_nothing() -> None:
    stream = turn_stream(ParseOnly())

    assert isinstance(stream, ParsedLines)
    assert stream.feed("hello") == [Event("text", "HELLO")]
    assert stream.finish(0) == stream.finish(1) == stream.finish(None) == []


@pytest.mark.parametrize(
    ("provider", "lines"),
    [
        (
            ClaudeProvider(),
            [
                {"type": "system", "subtype": "init", "session_id": "s-1", "model": "m-1"},
                {"type": "assistant", "message": {"content": [{"type": "text", "text": "hi"}]}},
                {"type": "result", "subtype": "success", "result": "done", "session_id": "s-1"},
            ],
        ),
        (
            CodexProvider(),
            [
                {"type": "thread.started", "thread_id": "t-1"},
                {"type": "item.completed", "item": {"type": "agent_message", "text": "hi"}},
                {"type": "turn.completed", "usage": {"input_tokens": 10, "output_tokens": 2}},
            ],
        ),
    ],
)
def test_the_built_in_streams_give_the_events_of_parse(provider, lines: list[dict]) -> None:
    stream = turn_stream(provider)
    texts = [json.dumps(line) for line in lines] + ["not json", ""]

    assert type(stream) is type(provider.stream()) and stream is not provider.stream()
    assert [stream.feed(text) for text in texts] == [provider.parse(text) for text in texts]
    assert stream.finish(0) == stream.finish(1) == []


# -- Invocation.notes ---------------------------------------------------------------------------


def test_the_notes_of_the_invocation_are_logged_for_the_turn(
    orch, project: str, plugins: None
) -> None:
    agent, run = orch.spawn(project, "one", worktree=False, provider="noting")
    assert orch.wait([agent.id], timeout=60)[0]

    infos = [e.text for e in orch.store.events(agent.id) if e.run_id == run.id and e.kind == "info"]
    assert infos.count(NOTE) == 1
    assert orch.store.get_run(run.id).status is RunStatus.DONE


# -- the variables passed on to the MCP server ------------------------------------------------


def test_the_built_in_providers_pass_on_the_variables_of_before() -> None:
    passed = runner.passed_env([ClaudeProvider(), CodexProvider()])

    assert sorted(passed) == sorted(
        (
            "CODEX_HOME",
            "CLAUDE_CONFIG_DIR",
            "ORCH_PROVIDERS",
            capabilities.PLUGINS_ENV,
            "PYTHONPATH",
        )
    )


def test_a_provider_config_home_is_passed_on_once(monkeypatch: pytest.MonkeyPatch) -> None:
    registered = [ClaudeProvider(), CodexProvider(), HomeProvider(), ParseOnly()]
    passed = runner.passed_env(registered)

    assert "HELD_BACK_HOME" in passed
    assert passed.count("CODEX_HOME") == 1

    monkeypatch.setattr(runner, "MCP_PASSED_ENV", passed)
    monkeypatch.setenv("HELD_BACK_HOME", "/tmp/held-back-home")
    agent = Agent(
        id="fix-login-3f2a",
        project="my-app",
        name="fix-login",
        role="lead",
        provider="home",
        cwd="/tmp",
    )
    assert runner.mcp_server(agent).env["HELD_BACK_HOME"] == "/tmp/held-back-home"
