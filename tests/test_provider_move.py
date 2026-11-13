"""Moving an agent to another provider (set_provider): the orchestrator before its first use,
an idle agent with and without a conversation, a busy one, and the CLI and API around it.

No real `claude` or `codex` runs: an agent that gets a turn is on a fake provider, or its
runner is not launched."""

from __future__ import annotations

import pytest
import test_provider_choice as choice
from starlette.testclient import TestClient
from typer.testing import CliRunner

from orchestrator import cli
from orchestrator.errors import NeedsConfirmation, OrchestratorError
from orchestrator.service import ORCHESTRATOR_ID, Orchestrator

ORIGIN = {"origin": "http://localhost"}
# The fixtures and helpers of test_provider_choice.
answer, infos, output, statuses = choice.answer, choice.infos, choice.output, choice.statuses
client, no_launch, two_fakes = choice.client, choice.no_launch, choice.two_fakes


def test_the_orchestrator_before_its_first_use_is_created_on_the_provider(
    orch: Orchestrator, no_launch: list[int], monkeypatch
) -> None:
    setup = orch.set_provider(ORCHESTRATOR_ID, "codex")

    assert setup["exists"] and setup["provider"] == "codex"
    assert orch.agent(ORCHESTRATOR_ID).provider == "codex" and no_launch == []
    statuses(monkeypatch, claude="not_installed")
    with pytest.raises(OrchestratorError, match="claude is not available"):
        orch.set_provider(ORCHESTRATOR_ID, "claude")


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

    with pytest.raises(NeedsConfirmation, match="starts a new conversation that does not know"):
        orch.set_provider(agent.id, "fake-codex")
    assert orch.agent(agent.id).provider == "fake"
    orch.set_provider(agent.id, "fake-codex", confirm=True)

    moved = orch.agent(agent.id)
    assert (moved.provider, moved.session_id, moved.effort) == ("fake-codex", None, None)
    assert infos(orch, agent.id)[-1] == (
        "moved from fake to fake-codex: the next message starts a new conversation on"
        f" fake-codex; the agent's own model and effort were cleared; the earlier fake session"
        f" was {session}"
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
