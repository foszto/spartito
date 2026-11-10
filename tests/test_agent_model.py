"""Changing an existing agent's own model and effort (service, API, CLI)."""

from __future__ import annotations

import time

import pytest
from starlette.testclient import TestClient
from typer.testing import CliRunner

from orchestrator import cli, config
from orchestrator.service import ORCHESTRATOR_ID, Orchestrator, OrchestratorError
from orchestrator.web.app import app

ORIGIN = {"origin": "http://localhost"}


def answer(orch: Orchestrator, agent_id: str) -> str:
    done, (view,) = orch.wait([agent_id], timeout=30)
    assert done
    return view["last_run"]["result"]


def own(orch: Orchestrator, agent_id: str) -> tuple[str | None, str | None]:
    agent = orch.agent(agent_id)
    return agent.model, agent.effort


@pytest.fixture
def client(orch: Orchestrator) -> TestClient:
    return TestClient(app, base_url="http://localhost")


@pytest.fixture
def fake_orchestrator_role(orch: Orchestrator) -> None:
    """The packaged orchestrator role runs Claude; point it at the fake provider."""
    config.user_roles_dir().mkdir(parents=True)
    role = "---\nlead: true\nworktree: false\nprovider: fake\n---\nYou orchestrate."
    (config.user_roles_dir() / "orchestrator.md").write_text(role)


def test_model_and_effort_are_set_and_cleared_independently(
    orch: Orchestrator, project: str
) -> None:
    orch.update_settings({"worker": {"fake": {"model": "fake-small", "effort": "low"}}})
    agent = orch.create_agent(project, worktree=False)

    orch.set_overrides(agent.id, {"model": "fake-large"})
    assert own(orch, agent.id) == ("fake-large", None)
    orch.set_overrides(agent.id, {"effort": "high"})
    assert own(orch, agent.id) == ("fake-large", "high")
    setup = orch.set_overrides(agent.id, {"model": None})
    assert own(orch, agent.id) == (None, "high")
    assert setup["effective"] == {"model": "fake-small", "effort": "high"}
    assert setup["inherited"] == {"model": "fake-small", "effort": "low"}
    assert (setup["model_override"], setup["effort_override"]) == (None, "high")
    orch.set_overrides(agent.id, {"model": "my-model", "effort": None})
    assert own(orch, agent.id) == ("my-model", None)

    view = orch.view(orch.agent(agent.id), detail=True)
    assert (view["model"], view["effort"]) == ("my-model", "low")
    assert (view["model_override"], view["effort_override"]) == ("my-model", None)


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"effort": "extreme"}, "fake has no effort 'extreme'"),
        ({"effort": ""}, "fake has no effort ''"),
        ({"model": ""}, "must not be empty"),
        ({"model": "fake large"}, "one word"),
        ({"model": " fake"}, "one word"),
        ({"model": "fake\tlarge"}, "one word"),
        ({"model": "fake\x1b[0m"}, "control characters"),
        ({"model": "fake​"}, "control characters"),
        ({"model": "--dangerously-skip-permissions"}, "must not start with '-'"),
        ({"model": "-x"}, "must not start with '-'"),
        ({"model": "m" * 201}, "too long"),
        ({"model": 5}, "must be a string"),
        ({"temperature": "1"}, "cannot set temperature"),
        ({}, "nothing to change"),
    ],
)
def test_invalid_values_are_refused_and_change_nothing(
    orch: Orchestrator, project: str, changes: dict, message: str
) -> None:
    agent = orch.create_agent(project, worktree=False, model="fake-small", effort="low")

    with pytest.raises(OrchestratorError, match=message):
        orch.set_overrides(agent.id, changes)
    assert own(orch, agent.id) == ("fake-small", "low")


def test_unusual_but_safe_model_names_are_accepted(orch: Orchestrator, project: str) -> None:
    agent = orch.create_agent(project, worktree=False)
    for name in ("claude-sonnet-4-5[1m]", "us.anthropic.claude:0", "org/model-x", "m" * 200):
        orch.set_overrides(agent.id, {"model": name})
        assert orch.agent(agent.id).model == name


def test_an_unknown_agent_is_an_error(orch: Orchestrator) -> None:
    with pytest.raises(OrchestratorError, match="unknown agent 'ghost'"):
        orch.set_overrides("ghost", {"model": "x"})
    with pytest.raises(OrchestratorError, match="unknown agent 'ghost'"):
        orch.setup("ghost")


def test_the_next_turn_uses_the_change_and_a_running_turn_keeps_its_own(
    orch: Orchestrator, project: str
) -> None:
    agent, _ = orch.spawn(project, "SLEEP 2 WHOAMI", worktree=False, model="fake-small")
    deadline = time.monotonic() + 20
    while not orch.agent(agent.id).session_id:  # the CLI has started: its model is set
        assert time.monotonic() < deadline, "the turn did not start"
        time.sleep(0.05)

    setup = orch.set_overrides(agent.id, {"model": "fake-large", "effort": "high"})
    assert setup["running"] is True
    assert answer(orch, agent.id) == "fake-small/None"

    orch.send(agent.id, "WHOAMI")
    assert answer(orch, agent.id) == "fake-large/high"
    orch.set_overrides(agent.id, {"model": None, "effort": None})
    orch.send(agent.id, "WHOAMI")
    assert answer(orch, agent.id) == "None/None"
    assert orch.setup(agent.id)["running"] is False


def test_the_orchestrator_is_shown_before_it_exists_and_created_by_a_change(
    orch: Orchestrator, fake_orchestrator_role: None
) -> None:
    orch.update_settings({"orchestrator": {"fake": {"model": "fake-large"}}})

    before = orch.setup(ORCHESTRATOR_ID)
    assert before["exists"] is False and orch.store.get_agent(ORCHESTRATOR_ID) is None
    assert before["inherited"] == {"model": "fake-large", "effort": None}

    after = orch.set_overrides(ORCHESTRATOR_ID, {"effort": "high"})
    assert after["exists"] is True and after["provider"] == "fake"
    assert after["effective"] == {"model": "fake-large", "effort": "high"}
    orch.send(ORCHESTRATOR_ID, "WHOAMI")
    assert answer(orch, ORCHESTRATOR_ID) == "fake-large/high"


def test_an_invalid_change_does_not_create_the_orchestrator(
    orch: Orchestrator, fake_orchestrator_role: None
) -> None:
    with pytest.raises(OrchestratorError):
        orch.set_overrides(ORCHESTRATOR_ID, {"effort": "extreme"})
    assert orch.store.get_agent(ORCHESTRATOR_ID) is None


def test_cache_keys_name_what_restarts_the_prompt_cache(orch: Orchestrator, project: str) -> None:
    fake = orch.create_agent(project, worktree=False)
    codex = orch.create_agent(project, worktree=False, provider="codex")

    assert orch.setup(fake.id)["cache_keys"] == ["model", "effort"]
    assert orch.setup(codex.id)["cache_keys"] == ["model"]


# -- API ------------------------------------------------------------------------


def test_the_api_sets_and_clears_the_model_and_effort(
    client: TestClient, orch: Orchestrator, project: str
) -> None:
    orch.update_settings({"worker": {"fake": {"model": "fake-small"}}})
    agent = orch.create_agent(project, worktree=False)
    url = f"/api/agents/{agent.id}/model"

    set_both = client.post(url, json={"model": "fake-large", "effort": "high"}, headers=ORIGIN)
    assert set_both.status_code == 200, set_both.text
    assert set_both.json()["effective"] == {"model": "fake-large", "effort": "high"}
    assert set_both.json()["inherited"] == {"model": "fake-small", "effort": None}
    cleared = client.post(url, json={"model": None}, headers=ORIGIN).json()
    assert (cleared["model_override"], cleared["effort_override"]) == (None, "high")
    assert cleared["effective"] == {"model": "fake-small", "effort": "high"}

    detail = client.get(f"/api/agents/{agent.id}/events").json()["agent"]
    assert detail["effective"] == {"model": "fake-small", "effort": "high"}
    assert (detail["model_override"], detail["effort_override"]) == (None, "high")
    assert detail["inherited"] == {"model": "fake-small", "effort": None}


def test_the_api_refuses_invalid_values_and_foreign_requests(
    client: TestClient, orch: Orchestrator, project: str
) -> None:
    agent = orch.create_agent(project, worktree=False)
    url = f"/api/agents/{agent.id}/model"

    bad_effort = client.post(url, json={"effort": "extreme"}, headers=ORIGIN)
    bad_model = client.post(url, json={"model": "-rf"}, headers=ORIGIN)
    unknown = client.post("/api/agents/ghost/model", json={"model": "x"}, headers=ORIGIN)
    assert (bad_effort.status_code, bad_model.status_code, unknown.status_code) == (400, 400, 400)
    assert "no effort 'extreme'" in bad_effort.json()["error"]
    assert "must not start with '-'" in bad_model.json()["error"]

    assert client.post(url, json={"model": "x"}).status_code == 403
    assert (
        client.post(url, json={"model": "x"}, headers={"origin": "http://evil.example"}).status_code
        == 403
    )
    assert client.post(url, data={"model": "x"}, headers=ORIGIN).status_code == 415
    assert own(orch, agent.id) == (None, None)


def test_the_api_creates_the_orchestrator_and_state_shows_it_before(
    client: TestClient, orch: Orchestrator, fake_orchestrator_role: None
) -> None:
    before = client.get("/api/state").json()
    assert before["orchestrator"] is None
    assert before["orchestrator_setup"]["provider"] == "fake"
    assert before["orchestrator_setup"]["exists"] is False

    changed = client.post(
        f"/api/agents/{ORCHESTRATOR_ID}/model", json={"model": "fake-large"}, headers=ORIGIN
    )
    assert changed.status_code == 200, changed.text
    after = client.get("/api/state").json()
    assert after["orchestrator"]["model"] == "fake-large"
    assert after["orchestrator_setup"] is None


# -- CLI ------------------------------------------------------------------------


def test_cli_model_shows_sets_and_clears(orch: Orchestrator, project: str) -> None:
    orch.update_settings({"worker": {"fake": {"model": "fake-small", "effort": "low"}}})
    agent = orch.create_agent(project, worktree=False)
    runner = CliRunner()

    shown = runner.invoke(cli.app, ["model", agent.id])
    assert shown.exit_code == 0, shown.output
    assert "model   fake-small           inherited" in shown.output
    assert "effort  low                  inherited" in shown.output

    changed = runner.invoke(cli.app, ["model", agent.id, "--model", "big", "--effort", "high"])
    assert changed.exit_code == 0, changed.output
    assert "model   big                  own (inherited: fake-small)" in changed.output
    assert "effort  high                 own (inherited: low)" in changed.output
    assert own(orch, agent.id) == ("big", "high")

    cleared = runner.invoke(cli.app, ["model", agent.id, "--model", ""])
    assert cleared.exit_code == 0 and own(orch, agent.id) == (None, "high")

    refused = runner.invoke(cli.app, ["model", agent.id, "--effort", "extreme"])
    assert refused.exit_code == 1 and "no effort 'extreme'" in refused.output
    assert own(orch, agent.id) == (None, "high")


def test_cli_model_notes_the_running_turn_and_the_cache(orch: Orchestrator, project: str) -> None:
    agent, _ = orch.spawn(project, "hello", worktree=False)
    answer(orch, agent.id)
    orch.send(agent.id, "SLEEP 5")
    deadline = time.monotonic() + 20
    while not orch.setup(agent.id)["running"]:
        assert time.monotonic() < deadline
        time.sleep(0.05)

    result = CliRunner().invoke(cli.app, ["model", agent.id, "--effort", "high"])

    assert result.exit_code == 0, result.output
    assert "applies from the next one" in result.output
    assert "fresh prompt cache" in result.output
    orch.stop(agent.id)
