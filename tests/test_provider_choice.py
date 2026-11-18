"""Choosing a provider from the available ones (core/availability.choose), the fallback note,
a project's default provider, a lead's and the orchestrator's provider, and moving an agent
to another provider (set_provider), through the facade, the CLI, the MCP tools and the API.

No real `claude` or `codex` runs: availability is faked (conftest, or a test's own statuses),
and an agent that gets a turn is on a fake provider, or its runner is not launched."""

from __future__ import annotations

import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import click
import pytest
from fakeprovider import FirstTurnFake
from starlette.testclient import TestClient
from typer.testing import CliRunner

from orchestrator import cli, config, mcp_server, providers
from orchestrator.core import availability
from orchestrator.errors import OrchestratorError
from orchestrator.providers import locate
from orchestrator.service import ORCHESTRATOR_ID, Orchestrator
from orchestrator.web.app import app

ORIGIN = {"origin": "http://localhost"}
FAKES = "fake=fakeprovider:FakeProvider,fake-codex=fakeprovider:FirstTurnFake"


def statuses(monkeypatch: pytest.MonkeyPatch, **by_name: str) -> None:
    """The availability of each provider: as named, else available (unknown without an
    executable, as the fakes). Nothing is run."""

    def probe(provider: Any) -> locate.Probe:
        default = "available" if getattr(provider, "executable", None) else "unknown"
        status = by_name.get(provider.name.replace("-", "_"), default)
        return locate.Probe(status, None, None, f"{status} here")

    monkeypatch.setattr(availability, "probe", probe)
    availability.reset()


@pytest.fixture
def no_launch(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """No runner starts (the agents may be on claude or codex): the queued run ids."""
    launched: list[int] = []
    monkeypatch.setattr(Orchestrator, "_launch", lambda self, run: launched.append(run.id))
    return launched


@pytest.fixture
def two_fakes(orch: Orchestrator, monkeypatch: pytest.MonkeyPatch) -> None:
    """A second fake provider, Codex-like, in this process and in the runners."""
    monkeypatch.setenv("ORCH_PROVIDERS", FAKES)
    monkeypatch.setitem(providers.PROVIDERS, "fake-codex", FirstTurnFake())


@pytest.fixture
def client(orch: Orchestrator) -> TestClient:
    return TestClient(app, base_url="http://localhost")


def until(predicate: Callable[[], Any], timeout: float = 30) -> Any:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if found := predicate():
            return found
        time.sleep(0.05)
    raise AssertionError("timed out")


def infos(orch: Orchestrator, agent_id: str) -> list[str]:
    return [event.text for event in orch.events(agent_id) if event.kind == "info"]


def answer(orch: Orchestrator, agent_id: str) -> str:
    done, (view,) = orch.wait([agent_id], timeout=30)
    assert done
    return view["last_run"]["result"]


def output(result: Any) -> str:
    return " ".join(click.unstyle(result.output).split())


# -- choose ------------------------------------------------------------------------------------


def test_an_explicit_provider_must_be_usable(orch: Orchestrator, monkeypatch) -> None:
    statuses(monkeypatch, codex="not_installed")

    assert availability.choose("claude") == availability.Choice("claude")
    with pytest.raises(OrchestratorError) as raised:
        availability.choose("codex", [("claude", "the project's default")])
    assert str(raised.value) == (
        "codex is not available: not installed (not_installed here). Available: claude, fake."
        f" `{config.CLI_NAME} providers` says more"
    )
    with pytest.raises(OrchestratorError, match="unknown provider 'nope'"):
        availability.choose("nope")


def test_an_unusable_default_falls_back_to_the_first_usable_with_a_note(
    orch: Orchestrator, monkeypatch
) -> None:
    statuses(monkeypatch, claude="not_installed")

    chosen = availability.choose(None, [(None, "the role's"), ("claude", "the project's default")])

    assert chosen == availability.Choice(
        "codex",
        "claude (the project's default) is not available: not installed (not_installed here);"
        " this agent runs on codex",
    )
    assert availability.choose(None, [("codex", "the project's default")]).note is None
    assert availability.choose(None) == availability.Choice(
        "codex",
        "claude (the first supported provider) is not available: not installed (not_installed"
        " here); this agent runs on codex",
    )


def test_unknown_counts_as_usable_and_nothing_usable_is_an_error(
    orch: Orchestrator, monkeypatch
) -> None:
    statuses(monkeypatch, claude="not_installed", codex="not_installed")
    assert availability.choose(None, [("fake", "the project's default")]).provider == "fake"

    statuses(monkeypatch, claude="not_installed", codex="not_installed", fake="not_installed")
    with pytest.raises(availability.NoneUsable) as raised:
        availability.choose(None, [("codex", "the project's default")])
    message = str(raised.value)
    assert message.startswith("no provider is available here: claude: not installed")
    assert "codex: not installed" in message and message.endswith("See docs/providers.md")


# -- new agents --------------------------------------------------------------------------------


def test_a_fallback_is_noted_on_the_first_run_and_in_every_reply(
    orch: Orchestrator, repo: Path, monkeypatch, no_launch: list[int], client: TestClient
) -> None:
    project = orch.add_project(str(repo), "demo", "codex").name
    statuses(monkeypatch, codex="not_installed", claude="not_installed")
    expected = (
        "codex (the project's default) is not available: not installed (not_installed here);"
        " this agent runs on fake"
    )

    agent, run = orch.spawn(project, "first")
    orch.send(agent.id, "second")

    assert agent.provider == "fake" and agent.provider_note == expected
    events = [(e.run_id, e.text) for e in orch.events(agent.id) if e.kind == "info"]
    assert events == [(run.id, expected)]  # the first run only

    reply = mcp_server.spawn_agent("task", project=project)
    assert reply["note"] == expected and reply["provider"] == "fake"
    result = CliRunner().invoke(cli.app, ["spawn", "task", "-p", project])
    assert result.exit_code == 0, result.output
    assert f"note: {expected}" in output(result)
    body = {"project": project, "task": "task"}
    assert client.post("/api/agents", json=body, headers=ORIGIN).json()["note"] == expected

    statuses(monkeypatch)
    reply = mcp_server.spawn_agent("task", project=project)
    assert "note" not in reply and reply["provider"] == "codex"
    assert client.post("/api/agents", json=body, headers=ORIGIN).json()["note"] is None


def test_an_explicit_provider_that_is_not_usable_is_refused(
    orch: Orchestrator, project: str, monkeypatch, no_launch: list[int]
) -> None:
    statuses(monkeypatch, codex="not_installed")

    with pytest.raises(OrchestratorError, match="codex is not available: not installed"):
        orch.spawn(project, "task", provider="codex")
    assert orch.agents(project) == [] and no_launch == []


def test_the_roles_provider_comes_before_the_projects(
    orch: Orchestrator, project: str, two_fakes, no_launch: list[int]
) -> None:
    config.user_roles_dir().mkdir(parents=True)
    (config.user_roles_dir() / "coder.md").write_text("---\nprovider: fake-codex\n---\nCode.")

    agent = orch.create_agent(project, role="coder")

    assert (agent.provider, agent.provider_note) == ("fake-codex", None)


# -- projects ----------------------------------------------------------------------------------


def test_a_project_without_a_provider_gets_the_first_usable(
    orch: Orchestrator, repo: Path, tmp_path: Path, monkeypatch, client: TestClient
) -> None:
    statuses(monkeypatch, claude="not_installed")
    assert orch.add_project(str(repo), "one").default_provider == "codex"

    result = CliRunner().invoke(cli.app, ["project", "add", str(repo), "--name", "two"])
    assert result.exit_code == 0, result.output
    assert "provider: codex" in output(result)
    reply = client.post("/api/projects", json={"path": str(repo), "name": "three"}, headers=ORIGIN)
    assert reply.json()["default_provider"] == "codex"
    assert mcp_server.add_project(str(repo), "four")["default_provider"] == "codex"
    with pytest.raises(OrchestratorError, match="claude is not available"):
        orch.add_project(str(repo), "five", "claude")


def test_with_nothing_usable_the_first_supported_is_registered_with_a_note(
    orch: Orchestrator, repo: Path, monkeypatch
) -> None:
    statuses(monkeypatch, claude="not_installed", codex="not_installed", fake="not_installed")

    project = orch.add_project(str(repo), "demo")

    assert project.default_provider == "claude"
    note = orch.project_note(project)
    assert note.startswith("The default provider claude is not available: not installed")
    assert "no other provider is available here either (see docs/providers.md)" in note


def test_a_projects_default_provider_can_be_changed(
    orch: Orchestrator, project: str, monkeypatch, client: TestClient, no_launch
) -> None:
    agent = orch.create_agent(project)

    changed = orch.set_default_provider(project, "codex")

    assert changed.default_provider == "codex" == orch.project(project).default_provider
    assert orch.agent(agent.id).provider == "fake"  # existing agents keep theirs
    assert orch.default_provider_note(changed) == (
        f"1 existing agent(s) keep their provider ({agent.id}); new agents run on codex"
    )
    assert orch.create_agent(project).provider == "codex"

    result = CliRunner().invoke(cli.app, ["project", "provider", project, "fake"])
    assert result.exit_code == 0, result.output
    assert f"{project}: new agents run on fake" in output(result)

    reply = client.post(
        f"/api/projects/{project}/provider", json={"provider": "claude"}, headers=ORIGIN
    )
    assert reply.json()["project"]["default_provider"] == "claude"
    assert reply.json()["note"].startswith("2 existing agent(s) keep their provider")

    assert mcp_server.set_project_provider(project, "codex")["default_provider"] == "codex"
    statuses(monkeypatch, claude="not_installed")
    with pytest.raises(OrchestratorError, match="claude is not available"):
        orch.set_default_provider(project, "claude")
    bad = client.post(
        f"/api/projects/{project}/provider", json={"provider": "claude"}, headers=ORIGIN
    )
    assert bad.status_code == 400 and "claude is not available" in bad.json()["error"]


def test_only_the_orchestrator_sets_a_projects_provider(
    orch: Orchestrator, project: str, monkeypatch
) -> None:
    monkeypatch.setenv("ORCH_AGENT_ID", orch.lead(project).id)

    with pytest.raises(OrchestratorError, match="only the top-level orchestrator"):
        mcp_server.set_project_provider(project, "codex")
    assert orch.project(project).default_provider == "fake"
    assert "set_project_provider" in mcp_server.ORCHESTRATOR_TOOLS


# -- the lead and the orchestrator -------------------------------------------------------------


def test_a_new_lead_gets_the_provider_asked_for_and_an_existing_one_keeps_its_own(
    orch: Orchestrator, project: str, two_fakes, no_launch: list[int], client: TestClient
) -> None:
    lead = orch.lead(project, provider="fake-codex")
    assert (lead.provider, lead.provider_note) == ("fake-codex", None)

    again = orch.lead(project, provider="fake")
    assert again.id == lead.id and again.provider == "fake-codex"
    note = (
        "the lead already runs on fake-codex, not fake: an existing lead keeps its provider"
        " until it is moved"
    )
    assert again.provider_note == note
    assert orch.lead(project, provider="fake-codex").provider_note is None

    assert mcp_server.ask_project_lead(project, "hi", provider="fake")["note"] == note
    result = CliRunner().invoke(cli.app, ["ask", project, "hi", "--provider", "fake"])
    assert result.exit_code == 0, result.output
    assert f"note: {note}" in output(result)
    reply = client.post(
        f"/api/projects/{project}/lead", json={"message": "hi", "provider": "fake"}, headers=ORIGIN
    )
    assert reply.json()["note"] == note and reply.json()["agent_id"] == lead.id


def test_a_board_dispatch_tells_of_the_new_leads_fallback(
    orch: Orchestrator, repo, monkeypatch, no_launch: list[int], client: TestClient
) -> None:
    names = [orch.add_project(str(repo), name, "claude").name for name in ("one", "two", "three")]
    ids = [orch.add_board_item(name, "Fix CI", state="ready").id for name in names]
    statuses(monkeypatch, claude="not_installed")
    note = (
        "claude (the project's default) is not available: not installed (not_installed here);"
        " this agent runs on codex"
    )

    result = CliRunner().invoke(cli.app, ["board", "dispatch", "--project", "one"])
    assert result.exit_code == 0, result.output
    assert f"note: {note}" in output(result)
    reply = client.post("/api/board/dispatch", json={"project": "two"}, headers=ORIGIN).json()
    assert reply["note"] == note
    assert mcp_server.dispatch_board("three", (ids[2],))["note"] == note
    assert [orch.lead(name).provider for name in names] == ["codex"] * 3
    item = orch.add_board_item("one", "Docs", state="ready")
    assert "note" not in mcp_server.dispatch_board("one", (item.id,))


def test_the_orchestrator_draft_takes_the_first_usable_provider(
    orch: Orchestrator, monkeypatch, client: TestClient
) -> None:
    statuses(monkeypatch, claude="not_installed")

    assert orch.setup(ORCHESTRATOR_ID)["provider"] == "codex"
    assert orch.orchestrator().provider == "codex"


def test_an_unusable_orchestrator_role_provider_falls_back_with_a_note(
    orch: Orchestrator, monkeypatch, no_launch: list[int], client: TestClient
) -> None:
    config.user_roles_dir().mkdir(parents=True)
    role = "---\nlead: true\nworktree: false\nprovider: codex\n---\nYou orchestrate."
    (config.user_roles_dir() / "orchestrator.md").write_text(role)
    statuses(monkeypatch, codex="not_installed")

    reply = client.post("/api/orchestrator", json={"message": "hi"}, headers=ORIGIN).json()

    assert reply["note"].startswith("codex (the orchestrator role's provider) is not available")
    assert reply["note"].endswith("this agent runs on claude")
    assert orch.agent(ORCHESTRATOR_ID).provider == "claude"
    assert infos(orch, ORCHESTRATOR_ID) == [reply["note"]]


def test_an_orchestrator_role_provider_not_registered_here_still_shows(
    orch: Orchestrator, client: TestClient
) -> None:
    """A role override may name a plugin this process does not load: the page still shows
    the orchestrator on it; the first message is where it fails."""
    config.user_roles_dir().mkdir(parents=True)
    role = "---\nlead: true\nworktree: false\nprovider: myplugin\n---\nYou orchestrate."
    (config.user_roles_dir() / "orchestrator.md").write_text(role)

    state = client.get("/api/state")

    assert state.status_code == 200
    assert state.json()["orchestrator_setup"]["provider"] == "myplugin"
    assert orch.setup(ORCHESTRATOR_ID)["provider"] == "myplugin"
    with pytest.raises(OrchestratorError, match="unknown provider 'nope'"):
        orch.set_provider(ORCHESTRATOR_ID, "nope")


def test_the_state_shows_the_orchestrator_draft_without_waiting(
    orch: Orchestrator, monkeypatch, client: TestClient
) -> None:
    def slow(provider: Any) -> locate.Probe:
        time.sleep(2)
        return locate.Probe("available", None, None, "slow")

    monkeypatch.setattr(availability, "probe", slow)
    availability.reset()

    started = time.monotonic()
    state = client.get("/api/state").json()

    assert time.monotonic() - started < 1
    assert state["orchestrator_setup"]["provider"] == "claude"  # not checked yet: usable
