from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from orchestrator import providers
from orchestrator.models import Agent
from orchestrator.providers import RunContext, claude, codex, get
from orchestrator.service import Orchestrator, OrchestratorError
from orchestrator.web.app import app


def answer(orch: Orchestrator, agent_id: str) -> str:
    done, (view,) = orch.wait([agent_id], timeout=30)
    assert done
    return view["last_run"]["result"]


def test_each_tier_gets_its_own_model_and_effort(orch: Orchestrator, project: str) -> None:
    orch.update_settings(
        {
            "lead": {"fake": {"model": "fake-large", "effort": "high"}},
            "worker": {"fake": {"model": "fake-small", "effort": "low"}},
        }
    )
    lead = orch.lead(project)
    orch.send(lead.id, "WHOAMI")
    worker, _ = orch.spawn(project, "WHOAMI", worktree=False)
    reviewer, _ = orch.spawn(project, "WHOAMI", role="reviewer")

    assert answer(orch, lead.id) == "fake-large/high"
    assert answer(orch, worker.id) == "fake-small/low"
    assert answer(orch, reviewer.id) == "fake-small/low"


def test_project_overrides_global_and_an_explicit_value_overrides_both(
    orch: Orchestrator, project: str
) -> None:
    orch.update_settings({"worker": {"fake": {"model": "fake-small", "effort": "low"}}})
    orch.update_settings({"worker": {"fake": {"model": "fake-large"}}}, project)
    inherited, _ = orch.spawn(project, "WHOAMI", worktree=False)
    explicit, _ = orch.spawn(project, "WHOAMI", worktree=False, model="mine", effort="high")

    assert answer(orch, inherited.id) == "fake-large/low"
    assert answer(orch, explicit.id) == "mine/high"


def test_a_changed_setting_applies_to_an_existing_agents_next_turn(
    orch: Orchestrator, project: str
) -> None:
    agent, _ = orch.spawn(project, "WHOAMI", worktree=False)
    before = answer(orch, agent.id)
    orch.update_settings({"worker": {"fake": {"model": "fake-large"}}})
    orch.send(agent.id, "WHOAMI")

    assert (before, answer(orch, agent.id)) == ("None/None", "fake-large/None")
    assert orch.effective(agent) == {"model": "fake-large", "effort": None}


def test_invalid_settings_are_rejected(orch: Orchestrator, project: str) -> None:
    with pytest.raises(OrchestratorError, match="no effort 'extreme'"):
        orch.update_settings({"worker": {"fake": {"effort": "extreme"}}})
    with pytest.raises(OrchestratorError, match="unknown tier"):
        orch.update_settings({"intern": {"fake": {"model": "x"}}})
    with pytest.raises(OrchestratorError, match="only be set globally"):
        orch.update_settings({"orchestrator": {"fake": {"model": "x"}}}, project)
    with pytest.raises(OrchestratorError, match="no effort 'extreme'"):
        orch.spawn(project, "task", effort="extreme")


def test_settings_over_the_api_and_cleared_values_are_dropped(
    orch: Orchestrator, project: str
) -> None:
    client = TestClient(app, base_url="http://localhost")
    origin = {"origin": "http://localhost"}
    data = {
        "worker": {"fake": {"model": "fake-small", "effort": ""}},
        "lead": {"fake": {"model": " "}},
    }

    saved = client.post("/api/settings", json={"data": data}, headers=origin)
    client.post(
        "/api/settings",
        json={"project": project, "data": {"lead": {"fake": {"effort": "high"}}}},
        headers=origin,
    )
    bad = client.post(
        "/api/settings", json={"data": {"worker": {"fake": {"effort": "extreme"}}}}, headers=origin
    )
    state = client.get("/api/state").json()

    assert saved.json() == {"worker": {"fake": {"model": "fake-small"}}}
    assert bad.status_code == 400
    assert state["settings"] == {
        "global": {"worker": {"fake": {"model": "fake-small"}}},
        "projects": {project: {"lead": {"fake": {"effort": "high"}}}},
    }
    fake = next(p for p in state["providers"] if p["name"] == "fake")
    assert fake["efforts"] == ["low", "high"] and fake["models_note"] is None
    assert fake["models"] == [
        {"value": "fake-small", "label": "fake-small"},
        {"value": "fake-large", "label": "fake-large"},
    ]


def test_state_lists_each_providers_models_as_values_and_labels(
    orch: Orchestrator, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = TestClient(app, base_url="http://localhost")
    catalog = '{"models":[{"slug":"gpt-x","visibility":"list"},{"slug":"h","visibility":"hide"}]}'
    monkeypatch.setattr(
        codex.subprocess,
        "run",
        lambda *a, **k: subprocess.CompletedProcess(a, 0, stdout=catalog, stderr=""),
    )
    monkeypatch.setitem(providers.PROVIDERS, "codex", codex.CodexProvider())

    listed = {p["name"]: p for p in client.get("/api/state").json()["providers"]}

    assert listed["codex"]["models"] == [{"value": "gpt-x", "label": "gpt-x"}]
    assert listed["claude"]["models_note"] is None
    assert {"value": "claude-mythos-5-1", "label": "Mythos 5.1"} in listed["claude"]["models"]
    assert listed["claude"]["models"][0] == {
        "value": "default",
        "label": "Default (recommended) · claude-mythos-5-2",
    }
    assert listed["fake"]["models"][0] == {"value": "fake-small", "label": "fake-small"}


def test_state_says_when_the_claude_list_is_the_fallback(
    orch: Orchestrator, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = TestClient(app, base_url="http://localhost")

    def timeout() -> str:
        raise subprocess.TimeoutExpired("claude", 20)

    monkeypatch.setattr(claude, "request_model_list", timeout)
    monkeypatch.setitem(providers.PROVIDERS, "claude", claude.ClaudeProvider())

    listed = {p["name"]: p for p in client.get("/api/state").json()["providers"]}

    assert listed["claude"]["models_note"] == "model list unavailable: showing aliases"
    assert listed["claude"]["models"][0] == {"value": "fable", "label": "fable"}


def test_providers_pass_model_and_effort_to_their_cli(tmp_path: Path) -> None:
    agent = Agent(id="a", project="p", name="a", role="worker", provider="x", cwd="/w")
    ctx = RunContext(
        agent=agent, prompt="p", system_prompt="", scratch=tmp_path, model="m1", effort="high"
    )

    claude = get("claude").invocation(ctx).argv
    codex = get("codex").invocation(ctx).argv

    assert (
        claude[claude.index("--model") + 1] == "m1"
        and claude[claude.index("--effort") + 1] == "high"
    )
    assert codex[codex.index("-m") + 1] == "m1" and 'model_reasoning_effort="high"' in codex


def test_view_shows_the_effective_model_and_effort_and_keeps_overrides_apart(
    orch: Orchestrator, project: str
) -> None:
    orch.update_settings({"worker": {"fake": {"model": "fake-small", "effort": "low"}}})
    orch.update_settings({"worker": {"fake": {"effort": "high"}}}, project)
    inherited = orch.create_agent(project, worktree=False)
    explicit = orch.create_agent(project, worktree=False, model="mine")
    lead = orch.lead(project)

    def picked(agent: Agent) -> tuple:
        view = orch.view(agent, detail=True)
        return view["model"], view["effort"], view["model_override"], view["effort_override"]

    assert picked(inherited) == ("fake-small", "high", None, None)
    assert picked(explicit) == ("mine", "high", "mine", None)
    assert picked(lead) == (None, None, None, None)
    assert (orch.view(inherited)["tokens"], orch.view(inherited)["cost_usd"]) == (None, 0)
