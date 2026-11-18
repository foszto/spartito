"""Moving an agent to another provider (set_provider): the orchestrator before its first use,
an idle agent with and without a conversation, a busy one, and the CLI and API around it.

No real `claude` or `codex` runs: an agent that gets a turn is on a fake provider, or its
runner is not launched."""

from __future__ import annotations

import json

import pytest
import test_provider_choice as choice
from fakeprovider import FakeProvider
from starlette.testclient import TestClient
from typer.testing import CliRunner

from orchestrator import cli, config
from orchestrator.errors import NeedsConfirmation, OrchestratorError
from orchestrator.service import ORCHESTRATOR_ID, Orchestrator
from orchestrator.timeline import AgentRuns

ORIGIN = {"origin": "http://localhost"}
# The fixtures and helpers of test_provider_choice.
answer, infos, output, statuses = choice.answer, choice.infos, choice.output, choice.statuses
client, no_launch, two_fakes = choice.client, choice.no_launch, choice.two_fakes


@pytest.fixture
def exec_argv(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    """What `attach` would run instead of the CLI (nothing is started)."""
    calls: list[list[str]] = []
    monkeypatch.setattr(cli.shutil, "which", lambda name, path=None: f"/bin/{name}")
    monkeypatch.setattr(cli.os, "execvpe", lambda file, argv, env: calls.append(argv))
    return calls


def test_the_orchestrator_before_its_first_use_is_created_on_the_provider(
    orch: Orchestrator, no_launch: list[int], monkeypatch
) -> None:
    setup = orch.set_provider(ORCHESTRATOR_ID, "codex")

    assert setup["exists"] and setup["provider"] == "codex"
    assert orch.agent(ORCHESTRATOR_ID).provider == "codex" and no_launch == []
    statuses(monkeypatch, claude="not_installed")
    with pytest.raises(OrchestratorError, match="claude is not available"):
        orch.set_provider(ORCHESTRATOR_ID, "claude")


def test_creating_the_orchestrator_on_a_fallback_by_a_setting_says_so(
    orch: Orchestrator, monkeypatch, client: TestClient
) -> None:
    """The model command and endpoint create the orchestrator on first use: their reply
    tells of the fallback, which no first run of this process would tell."""
    statuses(monkeypatch, claude="not_installed")
    note = (
        "claude (the first supported provider) is not available: not installed (not_installed"
        " here); this agent runs on codex"
    )

    result = CliRunner().invoke(cli.app, ["model", ORCHESTRATOR_ID, "--effort", "high"])

    assert result.exit_code == 0, result.output
    assert f"note: {note}" in output(result) and "moved from" not in output(result)
    assert orch.agent(ORCHESTRATOR_ID).provider == "codex"
    again = CliRunner().invoke(cli.app, ["model", ORCHESTRATOR_ID, "--effort", "low"])
    assert "note:" not in output(again)
    assert "note" not in orch.set_overrides(ORCHESTRATOR_ID, {"effort": "medium"})


def test_the_model_endpoint_tells_of_the_orchestrators_fallback(
    orch: Orchestrator, monkeypatch, client: TestClient
) -> None:
    statuses(monkeypatch, claude="not_installed")
    url = f"/api/agents/{ORCHESTRATOR_ID}/model"

    reply = client.post(url, json={"effort": "high"}, headers=ORIGIN).json()

    assert reply["provider"] == "codex" and reply["effort_override"] == "high"
    assert reply["note"].endswith("this agent runs on codex")
    assert "note" not in client.post(url, json={"effort": "low"}, headers=ORIGIN).json()


def test_an_agent_without_a_session_moves_at_once(
    orch: Orchestrator, project: str, two_fakes, no_launch: list[int]
) -> None:
    agent = orch.create_agent(project, model="fake-small", effort="high")

    setup = orch.set_provider(agent.id, "fake-codex")

    moved = orch.agent(agent.id)
    assert (moved.provider, moved.model, moved.effort) == ("fake-codex", None, None)
    assert setup["provider"] == "fake-codex" and setup["model_override"] is None
    assert orch.set_provider(agent.id, "fake-codex") == orch.setup(agent.id)  # no-op


def test_moving_an_agent_with_a_conversation_needs_confirmation_and_starts_anew(
    orch: Orchestrator, project: str, two_fakes
) -> None:
    agent, _ = orch.spawn(project, "hello", worktree=False, effort="high")
    assert answer(orch, agent.id) == "echo[new]: hello"
    session = orch.agent(agent.id).session_id
    assert session

    with pytest.raises(NeedsConfirmation) as asked:
        orch.set_provider(agent.id, "fake-codex")
    assert str(asked.value) == (
        f"{agent.id} has a conversation on fake: the next message starts a new conversation on"
        " fake-codex; the one on fake is kept and resumed when it moves back"
    )
    assert orch.agent(agent.id).provider == "fake"
    orch.set_provider(agent.id, "fake-codex", confirm=True)

    moved = orch.agent(agent.id)
    assert (moved.provider, moved.session_id, moved.effort) == ("fake-codex", None, None)
    assert orch.store.provider_sessions(agent.id) == {"fake": session}
    assert infos(orch, agent.id)[-1] == (
        "moved from fake to fake-codex: the next message starts a new conversation on"
        " fake-codex; the agent's own model and effort were cleared; the conversation on fake"
        f" (session {session}) is kept and resumed when it moves back"
    )
    # Nothing known of the new conversation yet: no size, nothing to compact, no attach.
    assert orch.view(moved, detail=True)["context"] is None
    assert orch.compact_refusal(moved) == "it has no conversation yet"
    attach = CliRunner().invoke(cli.app, ["attach", agent.id])
    assert attach.exit_code == 1 and "the agent has no session yet" in output(attach)
    orch.send(agent.id, "again")
    text = answer(orch, agent.id)
    assert text.startswith("echo[new]: ")  # no session to resume
    assert f"You are agent `{agent.id}`" in text and text.endswith("again")  # the role prompt


def turn(orch: Orchestrator, agent_id: str, message: str) -> tuple[str, int]:
    """Send `message` and wait for it: the answer and the run id."""
    run = orch.send(agent_id, message)
    return answer(orch, agent_id), run.id


def resumed_session(run_id: int) -> str | None:
    """The session the fake was asked to resume in run `run_id` (None: a new one)."""
    return json.loads((config.run_dir(run_id) / "ctx.json").read_text())["session_id"]


def test_moving_back_continues_the_earlier_conversation(
    orch: Orchestrator, project: str, two_fakes, monkeypatch, exec_argv: list[list[str]]
) -> None:
    """fake -> fake-codex -> fake -> fake-codex: each move back resumes the session kept for
    that provider, and what is shown of the conversation is that session's again."""
    agent, first_run = orch.spawn(
        project, "hello AUTOCOMPACT CONTEXT 500 TOKENS 100 CUMULATIVE", worktree=False
    )
    first = first_run.id
    assert answer(orch, agent.id).startswith("echo[new]: hello")
    _, second = turn(orch, agent.id, "more CONTEXT 400 TOKENS 100 CUMULATIVE")
    orch.set_provider(agent.id, "fake-codex", confirm=True)
    text, there = turn(orch, agent.id, "there CONTEXT 700 TOKENS 50 CUMULATIVE")
    assert text.startswith("echo[new]: ") and f"You are agent `{agent.id}`" in text
    orch.set_overrides(agent.id, {"effort": "high"})

    with pytest.raises(NeedsConfirmation) as asked:
        orch.set_provider(agent.id, "fake")
    assert str(asked.value) == (
        f"{agent.id} has a conversation on fake-codex: the next message continues the earlier"
        " conversation on fake (session fake-session); the one on fake-codex is kept and resumed"
        " when it moves back"
    )
    setup = orch.set_provider(agent.id, "fake", confirm=True)

    back = orch.agent(agent.id)
    assert (back.provider, back.session_id, back.effort) == ("fake", "fake-session", None)
    assert setup["session_id"] == "fake-session" and setup["effort_override"] is None
    assert orch.store.provider_sessions(agent.id) == {"fake-codex": "fake-codex-session"}
    assert infos(orch, agent.id)[-1] == (
        "moved from fake-codex to fake: the next message continues the earlier conversation on"
        " fake (session fake-session); the agent's own model and effort were cleared; the"
        " conversation on fake-codex (session fake-codex-session) is kept and resumed when it"
        " moves back"
    )
    # What is shown is the resumed session's: its size, its compaction, attach to it.
    assert orch.view(back, detail=True)["context"]["tokens"] == 400
    assert AgentRuns.read(orch.store, agent.id).compaction_left("fake-session") == (400, second)
    assert orch.compact_refusal(back) is None
    monkeypatch.setattr(FakeProvider, "attach_command", lambda self, a: ["true", a.session_id])
    attached = CliRunner().invoke(cli.app, ["attach", agent.id])
    assert attached.exit_code == 0, attached.output
    assert exec_argv == [["true", "fake-session"]]
    # The next turn resumes it; the fake's running total of the session goes on from 200.
    text, again = turn(orch, agent.id, "again TOKENS 10 CUMULATIVE")
    assert text == "echo[resumed]: again TOKENS 10 CUMULATIVE"
    assert resumed_session(again) == "fake-session"
    assert [orch.store.get_usage(run).input_tokens for run in (first, second, there, again)] == [
        100,
        100,
        50,
        10,
    ]

    orch.set_provider(agent.id, "fake-codex", confirm=True)
    text, later = turn(orch, agent.id, "later")
    assert text == "echo[resumed]: later"  # no role prompt again in a resumed session
    assert resumed_session(later) == "fake-codex-session"
    assert orch.store.provider_sessions(agent.id) == {"fake": "fake-session"}


def test_a_move_without_a_conversation_to_a_kept_one_needs_no_confirmation(
    orch: Orchestrator, project: str, no_launch: list[int]
) -> None:
    agent = orch.create_agent(project)
    orch.store.set_session(agent.id, "session-1")
    orch.set_provider(agent.id, "codex", confirm=True)

    setup = orch.set_provider(agent.id, "fake")  # nothing on codex yet: nothing to lose

    assert setup["session_id"] == "session-1"
    assert orch.store.provider_sessions(agent.id) == {}


def test_the_orchestrator_moves_back_to_its_earlier_conversation(
    orch: Orchestrator, no_launch: list[int]
) -> None:
    maestro = orch.orchestrator()
    assert maestro.provider == "claude"
    orch.store.set_session(ORCHESTRATOR_ID, "s-claude")

    with pytest.raises(NeedsConfirmation, match="starts a new conversation on codex"):
        orch.set_provider(ORCHESTRATOR_ID, "codex")
    orch.set_provider(ORCHESTRATOR_ID, "codex", confirm=True)
    orch.store.set_session(ORCHESTRATOR_ID, "s-codex")
    with pytest.raises(NeedsConfirmation, match=r"continues the earlier conversation on claude"):
        orch.set_provider(ORCHESTRATOR_ID, "claude")
    setup = orch.set_provider(ORCHESTRATOR_ID, "claude", confirm=True)

    assert (setup["provider"], setup["session_id"]) == ("claude", "s-claude")
    assert orch.store.provider_sessions(ORCHESTRATOR_ID) == {"codex": "s-codex"}
    assert no_launch == []


def test_a_busy_agent_is_not_moved(
    orch: Orchestrator, project: str, no_launch: list[int], monkeypatch
) -> None:
    agent, _ = orch.spawn(project, "queued, never launched")

    with pytest.raises(OrchestratorError, match="has a running or queued turn: stop it or wait"):
        orch.set_provider(agent.id, "codex")
    assert orch.agent(agent.id).provider == "fake"
    statuses(monkeypatch, codex="not_installed")
    orch.stop(agent.id)
    with pytest.raises(OrchestratorError, match="codex is not available"):
        orch.set_provider(agent.id, "codex")


def test_the_cli_asks_before_moving_a_conversation(
    orch: Orchestrator, project: str, no_launch: list[int]
) -> None:
    agent = orch.create_agent(project)
    orch.store.set_session(agent.id, "session-1")

    refused = CliRunner().invoke(cli.app, ["model", agent.id, "--provider", "codex"], input="n\n")
    assert refused.exit_code == 1 and orch.agent(agent.id).provider == "fake"
    assert "starts a new conversation" in output(refused)

    moved = CliRunner().invoke(cli.app, ["model", agent.id, "--provider", "codex", "--yes"])
    assert moved.exit_code == 0, moved.output
    assert orch.agent(agent.id).provider == "codex"
    assert "note: moved from fake to codex: the next message starts a new conversation" in output(
        moved
    )
    back = CliRunner().invoke(cli.app, ["model", agent.id, "--provider", "fake"])
    assert back.exit_code == 0, back.output  # no conversation on codex: nothing asked
    assert (
        "note: moved from codex to fake: the next message continues the earlier conversation on"
        " fake (session session-1)"
    ) in output(back)


def test_the_api_answers_a_needed_confirmation(
    orch: Orchestrator, project: str, no_launch: list[int], client: TestClient
) -> None:
    agent = orch.create_agent(project)
    orch.store.set_session(agent.id, "session-1")
    url = f"/api/agents/{agent.id}/model"

    asked = client.post(url, json={"provider": "codex"}, headers=ORIGIN).json()
    assert set(asked) == {"confirm"} and "starts a new conversation" in asked["confirm"]
    assert orch.agent(agent.id).provider == "fake"

    body = {"provider": "codex", "confirm": True, "effort": "high"}
    setup = client.post(url, json=body, headers=ORIGIN).json()
    assert setup["provider"] == "codex" and setup["effort_override"] == "high"
    assert client.post(url, json={"model": "gpt-x"}, headers=ORIGIN).json()["model_override"] == (
        "gpt-x"
    )
