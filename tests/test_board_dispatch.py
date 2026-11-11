"""Dispatching named board items across projects (core/boards.dispatch_items): facade, API,
CLI and the orchestrator's MCP tool. No runner starts: the messages stay queued."""

from __future__ import annotations

import json
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

import anyio
import pytest
from starlette.testclient import TestClient
from test_notifications import lead
from typer.testing import CliRunner

from orchestrator import cli, mcp_server
from orchestrator.service import ORCHESTRATOR_ID, Orchestrator, OrchestratorError
from orchestrator.web.app import app

ORIGIN = {"origin": "http://localhost"}


@pytest.fixture(autouse=True)
def no_runner(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(Orchestrator, "_launch", lambda self, run: None)


@pytest.fixture
def other(orch: Orchestrator, tmp_path: Path) -> str:
    second = tmp_path / "second"
    second.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(second)], check=True)
    return orch.add_project(str(second), "other", "fake").name


@pytest.fixture
def as_agent(monkeypatch: pytest.MonkeyPatch) -> Callable[[str | None], None]:
    def act(agent_id: str | None) -> None:
        if agent_id:
            monkeypatch.setenv("ORCH_AGENT_ID", agent_id)
        else:
            monkeypatch.delenv("ORCH_AGENT_ID", raising=False)

    return act


def messages(orch: Orchestrator, agent_id: str) -> list[str]:
    return [run.prompt for run in orch.store.runs_of(agent_id)]


def sent(result: dict[str, Any]) -> dict[str, list[int]]:
    return {r["project"]: [item["id"] for item in r["items"]] for r in result["results"]}


def board(orch: Orchestrator, project: str) -> dict[str, int]:
    """One item in each state, titled by it: {state: id}."""
    return {
        state: orch.add_board_item(project, state, state=state).id
        for state in ("idea", "ready", "in_progress", "done")
    }


def test_named_items_go_to_each_projects_lead_in_one_message(
    orch: Orchestrator, project: str, other: str
) -> None:
    mine, theirs = board(orch, project), board(orch, other)
    left = orch.add_board_item(project, "not named", state="ready").id

    ids = [theirs["ready"], mine["idea"], mine["ready"], mine["done"], 999, mine["idea"]]
    ids.append(theirs["in_progress"])
    result = orch.dispatch_items(ids)

    assert sent(result) == {project: [mine["ready"], mine["idea"]], other: [theirs["ready"]]}
    assert result["skipped"] == [
        {"id": mine["done"], "reason": "already done"},
        {"id": 999, "reason": "no such item"},
        {"id": theirs["in_progress"], "reason": "already in_progress"},
    ]
    for name, ours in ((project, mine), (other, theirs)):
        (entry,) = [r for r in result["results"] if r["project"] == name]
        lead_id = orch.lead(name).id
        assert entry["agent_id"] == lead_id
        (text,) = messages(orch, lead_id)
        assert entry["run_id"] == orch.store.runs_of(lead_id)[0].id
        assert orch.store.meta(entry["run_id"]).sender is None  # from the owner
        assert "dispatched by the owner:" in text and f"#{ours['ready']} ready" in text
        assert all(item["state"] == "in_progress" for item in entry["items"])
        assert all(item["agent_id"] == lead_id for item in entry["items"])
    assert "#" + str(left) not in messages(orch, orch.lead(project).id)[0]
    assert orch.board_item(left).state == "ready"


def test_a_project_limits_which_named_items_go(
    orch: Orchestrator, project: str, other: str
) -> None:
    mine, theirs = board(orch, project), board(orch, other)

    result = orch.dispatch_items([mine["ready"], theirs["ready"]], project)

    assert sent(result) == {project: [mine["ready"]]}
    reason = f"in project {other}, not {project}"
    assert result["skipped"] == [{"id": theirs["ready"], "reason": reason}]
    with pytest.raises(OrchestratorError, match="unknown project"):
        orch.dispatch_items([mine["idea"]], "nope")


def test_nothing_to_send_sends_nothing(orch: Orchestrator, project: str) -> None:
    mine = board(orch, project)

    result = orch.dispatch_items([mine["done"]])

    assert result == {"results": [], "skipped": [{"id": mine["done"], "reason": "already done"}]}
    assert orch.agents(project) == []  # no lead created for nothing
    for bad in ("all", [], [True], [1, "2"], None):
        with pytest.raises(OrchestratorError, match="ids must be a non-empty list"):
            orch.dispatch_items(bad)


def test_one_projects_failed_send_skips_only_its_items(
    orch: Orchestrator, project: str, other: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    mine, theirs = board(orch, project), board(orch, other)
    blocked = lead(orch, other)
    original = Orchestrator.send

    def send(self: Orchestrator, agent_id: str, *args: Any, **kwargs: Any) -> Any:
        if agent_id == blocked:
            raise OrchestratorError("cannot send")
        return original(self, agent_id, *args, **kwargs)

    monkeypatch.setattr(Orchestrator, "send", send)
    result = orch.dispatch_items([theirs["idea"], mine["idea"]])

    assert sent(result) == {project: [mine["idea"]]}
    assert result["skipped"] == [{"id": theirs["idea"], "reason": "cannot send"}]
    restored = orch.board_item(theirs["idea"])
    assert (restored.state, restored.agent_id) == ("idea", None)


def test_api_dispatches_named_items(orch: Orchestrator, project: str, other: str) -> None:
    client = TestClient(app, base_url="http://localhost")
    mine, theirs = board(orch, project), board(orch, other)
    body = {"ids": [mine["idea"], theirs["ready"], mine["done"]]}

    assert client.post("/api/board/dispatch-items", json=body).status_code == 403
    answer = client.post("/api/board/dispatch-items", json=body, headers=ORIGIN)
    assert answer.status_code == 200
    result = answer.json()
    assert sent(result) == {project: [mine["idea"]], other: [theirs["ready"]]}
    assert set(result["results"][0]) == {"project", "agent_id", "run_id", "items"}
    assert result["skipped"] == [{"id": mine["done"], "reason": "already done"}]
    bad = client.post("/api/board/dispatch-items", json={"ids": "all"}, headers=ORIGIN)
    assert bad.status_code == 400 and "ids must be" in bad.json()["error"]


def test_cli_dispatches_named_items(orch: Orchestrator, project: str, other: str) -> None:
    runner = CliRunner()
    mine, theirs = board(orch, project), board(orch, other)

    out = runner.invoke(cli.app, ["board", "dispatch", str(mine["idea"]), str(theirs["ready"])])
    assert out.exit_code == 0, out.output
    first, second = orch.lead(project).id, orch.lead(other).id
    assert out.output.splitlines() == [
        f"#{mine['idea']} -> {first}  run {orch.store.runs_of(first)[0].id}",
        f"#{theirs['ready']} -> {second}  run {orch.store.runs_of(second)[0].id}",
    ]
    limited = runner.invoke(
        cli.app, ["board", "dispatch", str(mine["ready"]), str(mine["done"]), "-p", other]
    )
    assert limited.exit_code == 0 and limited.output.splitlines() == [
        f"#{mine['ready']} not sent: in project {project}, not {other}",
        f"#{mine['done']} not sent: in project {project}, not {other}",
        "nothing sent",
    ]
    bare = runner.invoke(cli.app, ["board", "dispatch"])
    assert bare.exit_code == 1 and "name item ids or --project" in bare.output
    whole = runner.invoke(cli.app, ["board", "dispatch", "-p", project])  # as before
    assert whole.output == f"#{mine['ready']} -> {first}  run {orch.store.runs_of(first)[1].id}\n"


# -- the orchestrator's MCP tool -----------------------------------------------------


def test_the_orchestrator_dispatches_through_the_mcp_tool(
    orch: Orchestrator, project: str, other: str, as_agent: Callable
) -> None:
    mine, theirs = board(orch, project), board(orch, other)
    second = orch.add_board_item(project, "second", state="ready").id
    old_lead = lead(orch, project)  # made before the orchestrator agent: adopted
    orch.orchestrator()
    as_agent(ORCHESTRATOR_ID)

    reply = mcp_server.dispatch_board(project)
    assert reply == {
        "sent": [mine["ready"], second],
        "lead": old_lead,
        "run_id": orch.store.runs_of(old_lead)[0].id,
    }
    assert orch.agent(old_lead).parent_id == ORCHESTRATOR_ID
    assert orch.store.meta(reply["run_id"]).sender == ORCHESTRATOR_ID
    assert "dispatched by orchestrator:" in messages(orch, old_lead)[0]

    named = mcp_server.dispatch_board(other, (theirs["idea"], theirs["done"], mine["idea"]))
    lead_id = orch.lead(other).id
    assert named == {
        "sent": [theirs["idea"]],
        "skipped": {
            str(theirs["done"]): "already done",
            str(mine["idea"]): f"in project {project}, not {other}",
        },
        "lead": lead_id,
        "run_id": orch.store.runs_of(lead_id)[0].id,
    }
    assert mcp_server.dispatch_board(project) == {"sent": [], "lead": None, "run_id": None}
    nothing = mcp_server.dispatch_board(project, (mine["done"],))
    assert nothing == {
        "sent": [],
        "skipped": {str(mine["done"]): "already done"},
        "lead": None,
        "run_id": None,
    }


def test_a_lead_cannot_dispatch(orch: Orchestrator, project: str, as_agent: Callable) -> None:
    item = orch.add_board_item(project, "x", state="ready").id
    as_agent(lead(orch, project))
    with pytest.raises(OrchestratorError, match="only the top-level orchestrator"):
        mcp_server.dispatch_board(project)
    as_agent(None)  # the interactive session may: the message comes from the user
    reply = mcp_server.dispatch_board(project, (item,))
    assert reply["sent"] == [item] and orch.store.meta(reply["run_id"]).sender is None


def test_an_id_beyond_sqlites_range_is_skipped(orch: Orchestrator, project: str) -> None:
    mine = board(orch, project)
    client = TestClient(app, base_url="http://localhost")

    body = {"ids": [2**63, mine["ready"]]}
    answer = client.post("/api/board/dispatch-items", json=body, headers=ORIGIN)

    assert answer.status_code == 200
    assert sent(answer.json()) == {project: [mine["ready"]]}
    assert answer.json()["skipped"] == [{"id": 2**63, "reason": "no such item"}]


def test_the_mcp_server_takes_the_tools_arguments(
    orch: Orchestrator, project: str, as_agent: Callable
) -> None:
    mine = board(orch, project)
    as_agent(None)

    reply = anyio.run(
        mcp_server.server.call_tool, "dispatch_board", {"project": project, "items": [mine["idea"]]}
    )
    assert json.loads(reply.content[0].text)["sent"] == [mine["idea"]]
    with pytest.raises(Exception, match="project\n  Field required"):  # validated as before
        anyio.run(mcp_server.server.call_tool, "dispatch_board", {"items": ["x"]})
