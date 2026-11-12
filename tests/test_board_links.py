"""Where a board item came from and what blocks it (P5): items made from a conversation
message, decisions linked to items, the blocker rule, request_decision(item=) and
`orch board link`."""

from __future__ import annotations

import sqlite3
import subprocess
from pathlib import Path
from typing import Any

import pytest
from starlette.testclient import TestClient
from test_notifications import lead
from typer.testing import CliRunner

from orchestrator import board, cli, mcp_server
from orchestrator.service import ORCHESTRATOR_ID, Orchestrator, OrchestratorError
from orchestrator.web.app import app

ORIGIN = {"origin": "http://localhost"}


@pytest.fixture
def client() -> TestClient:
    return TestClient(app, base_url="http://localhost")


@pytest.fixture
def alpha(orch: Orchestrator, tmp_path: Path) -> str:
    """A second project."""
    second = tmp_path / "second"
    second.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(second)], check=True)
    return orch.add_project(str(second), "alpha", "fake").name


def post(client: TestClient, path: str, body: Any) -> Any:
    return client.post(path, json=body, headers=ORIGIN)


def message(orch: Orchestrator, agent_id: str, text: str, run_id: int = 7) -> int:
    """A line of the agent's conversation (an event of its run); its id."""
    orch.store.add_event(run_id, agent_id, "text", text)
    return orch.store.events(agent_id)[-1].id


def written(orch: Orchestrator) -> tuple[int, ...]:
    """How many rows the board's tables hold."""
    with orch.store.connection() as conn:
        return tuple(
            conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in ("board_items", "board_history", "board_sources", "board_blockers")
        )


def actions(orch: Orchestrator, item_id: int) -> list[tuple[str, dict[str, Any]]]:
    return [(entry["action"], entry["changes"]) for entry in orch.board_detail(item_id)["history"]]


# -- items from a message ----------------------------------------------------------------


def test_an_item_made_from_a_message_links_back_to_it(
    orch: Orchestrator, project: str, client: TestClient
) -> None:
    lead_id = lead(orch, project)
    event = message(orch, lead_id, "\n  Add a search box  \nso that the list can be filtered\n")

    answer = post(client, "/api/board/from-message", {"agent_id": lead_id, "event_id": event})

    assert answer.status_code == 200, answer.json()
    item = answer.json()
    assert (item["project"], item["state"], item["title"]) == (project, "idea", "Add a search box")
    assert item["description"] == "Add a search box  \nso that the list can be filtered"
    assert item["agent_id"] is None and item == orch.board_item(item["id"]).to_dict()
    source = {"agent_id": lead_id, "event_id": event, "run_id": 7}
    detail = client.get(f"/api/board/{item['id']}/detail").json()
    assert detail["source"] == source and detail["blockers"] == []
    shown = next(i for i in client.get("/api/board/view").json()["items"] if i["id"] == item["id"])
    assert shown["source"] == source and shown["blockers"] == []
    assert actions(orch, item["id"]) == [
        ("add", {"state": [None, "idea"], "source": f"{lead_id} message {event}"})
    ]
    shown = CliRunner().invoke(cli.app, ["board", "show", str(item["id"])])
    assert f"  from {lead_id}, message {event}" in shown.output.splitlines()


def test_a_message_item_takes_what_it_is_given_cut_to_the_boards_limits(
    orch: Orchestrator, project: str, client: TestClient
) -> None:
    lead_id = lead(orch, project)
    event = message(orch, lead_id, "t" * 300 + "\n" + "d" * 5000)

    long = orch.add_item_from_message(lead_id, event)
    assert long.title == "t" * 199 + "…" and len(long.description) == board.DESCRIPTION_CHARS
    assert long.description.endswith("d…")
    given = post(
        client,
        "/api/board/from-message",
        {
            "agent_id": lead_id,
            "event_id": event,
            "title": " Mine ",
            "description": "",
            "state": "ready",
            "project": project,
        },
    ).json()
    assert (given["title"], given["description"], given["state"]) == ("Mine", "", "ready")


def test_the_orchestrators_message_needs_a_project(
    orch: Orchestrator, project: str, client: TestClient
) -> None:
    orch.orchestrator()
    event = message(orch, ORCHESTRATOR_ID, "A plan")
    before = written(orch)

    refused = post(
        client, "/api/board/from-message", {"agent_id": ORCHESTRATOR_ID, "event_id": event}
    )
    assert refused.status_code == 400 and "name the project" in refused.json()["error"]
    assert written(orch) == before

    body = {"agent_id": ORCHESTRATOR_ID, "event_id": event, "project": project}
    item = post(client, "/api/board/from-message", body).json()
    assert item["project"] == project and item["title"] == "A plan"
    assert orch.board_detail(item["id"])["source"]["agent_id"] == ORCHESTRATOR_ID


def test_a_message_item_refuses_bad_input_and_writes_nothing(
    orch: Orchestrator, project: str, client: TestClient
) -> None:
    lead_id = lead(orch, project)
    other = lead(orch, project, "other")
    mine = message(orch, lead_id, "mine")
    theirs = message(orch, other, "theirs")
    before = written(orch)

    for body, error in [
        ({"agent_id": lead_id, "event_id": theirs}, f"{lead_id} has no message #{theirs}"),
        ({"agent_id": lead_id, "event_id": 999}, "has no message #999"),
        ({"agent_id": lead_id, "event_id": 2**63}, f"has no message #{2**63}"),
        ({"agent_id": lead_id, "event_id": str(mine)}, "event_id (a number)"),
        ({"agent_id": lead_id}, "are required"),
        ({"agent_id": "nobody", "event_id": mine}, "unknown agent 'nobody'"),
        ({"agent_id": lead_id, "event_id": mine, "state": "doing"}, "unknown state"),
        ({"agent_id": lead_id, "event_id": mine, "project": "nope"}, "unknown project"),
        ({"agent_id": lead_id, "event_id": mine, "title": 3}, "title must be a string"),
    ]:
        answer = post(client, "/api/board/from-message", body)
        assert answer.status_code == 400 and error in answer.json()["error"], (body, answer.json())
    assert client.post("/api/board/from-message", json={}).status_code == 403
    assert written(orch) == before


def test_a_failing_source_write_leaves_no_item(
    orch: Orchestrator, project: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    lead_id = lead(orch, project)
    event = message(orch, lead_id, "x")
    before = written(orch)

    def broken(*args: Any) -> None:
        raise sqlite3.OperationalError("disk full")

    monkeypatch.setattr(board, "save_source", broken)
    with pytest.raises(sqlite3.OperationalError):
        orch.add_item_from_message(lead_id, event)
    assert written(orch) == before


# -- decisions that block items ------------------------------------------------------------


def test_linking_a_decision_blocks_only_its_items(
    orch: Orchestrator, project: str, client: TestClient
) -> None:
    lead_id = lead(orch, project)
    a = orch.add_board_item(project, "a", agent_id=lead_id).id
    b = orch.add_board_item(project, "b", agent_id=lead_id).id
    decision = orch.request_decision(lead_id, "Which database?").id

    implicit = [
        {"id": decision, "question": "Which database?", "agent_id": lead_id, "linked": False}
    ]
    assert orch.board_detail(a)["blockers"] == orch.board_detail(b)["blockers"] == implicit

    answer = post(client, f"/api/board/{a}/blockers", {"decision_id": decision})
    explicit = [{**implicit[0], "linked": True}]
    assert answer.status_code == 200 and answer.json() == {"blockers": explicit}
    view = {item["id"]: item for item in client.get("/api/board/view").json()["items"]}
    assert view[a]["blockers"] == explicit and view[b]["blockers"] == []
    assert actions(orch, a)[-1] == ("link", {"decision": [None, decision]})

    again = post(client, f"/api/board/{a}/blockers", {"decision_id": decision, "linked": True})
    assert again.json() == {"blockers": explicit} and len(actions(orch, a)) == 2  # no-op
    unlinked = post(client, f"/api/board/{a}/blockers", {"decision_id": decision, "linked": False})
    assert unlinked.json() == {"blockers": implicit}  # without links it blocks all again
    assert actions(orch, a)[-1] == ("unlink", {"decision": [decision, None]})
    assert orch.board_detail(a)["unrecorded"] is False


def test_the_blocker_rule(
    orch: Orchestrator, project: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(Orchestrator, "_launch", lambda self, run: None)
    lead_id, other = lead(orch, project), lead(orch, project, "other")
    open_item = orch.add_board_item(project, "open", agent_id=lead_id).id
    done = orch.add_board_item(project, "done", state="done", agent_id=lead_id).id
    theirs = orch.add_board_item(project, "theirs", agent_id=other).id
    nobodys = orch.add_board_item(project, "nobody's").id
    unlinked = orch.request_decision(lead_id, "unlinked").id
    linked = orch.request_decision(other, "linked").id
    orch.link_decision(nobodys, linked)
    answered = orch.request_decision(lead_id, "answered").id
    orch.link_decision(open_item, answered)
    orch.answer_decision(answered, "yes")
    withdrawn = orch.request_decision(lead_id, "withdrawn").id
    orch.withdraw_decision(withdrawn, lead_id)

    blocked = {
        item["id"]: [blocker["id"] for blocker in item["blockers"]]
        for item in orch.board_view()["items"]
    }
    assert blocked == {open_item: [unlinked], done: [], theirs: [], nobodys: [linked]}


def test_linking_refuses_bad_input_and_writes_nothing(
    orch: Orchestrator, project: str, alpha: str, client: TestClient
) -> None:
    lead_id = lead(orch, project)
    item = orch.add_board_item(project, "x").id
    decision = orch.request_decision(lead_id, "q").id
    elsewhere = orch.request_decision(lead(orch, alpha, "a"), "theirs").id
    before = written(orch)

    for path, body, error in [
        (f"/api/board/{item}/blockers", {"decision_id": 999}, "no decision #999"),
        (f"/api/board/{item}/blockers", {"decision_id": 2**63}, f"no decision #{2**63}"),
        (f"/api/board/{item}/blockers", {"decision_id": "1"}, "must be numbers"),
        (f"/api/board/{item}/blockers", {"decision_id": decision, "linked": "no"}, "true or false"),
        (f"/api/board/{item}/blockers", {"decision_id": elsewhere}, "about project alpha"),
        ("/api/board/999/blockers", {"decision_id": decision}, "no board item #999"),
        (f"/api/board/{2**63}/blockers", {"decision_id": decision}, f"no board item #{2**63}"),
    ]:
        answer = post(client, path, body)
        assert answer.status_code == 400 and error in answer.json()["error"], (body, answer.json())
    assert post(
        client, f"/api/board/{item}/blockers", {"decision_id": decision, "linked": False}
    ).json() == {"blockers": []}  # not linked: nothing to unlink
    assert (
        client.post(f"/api/board/{item}/blockers", json={"decision_id": decision}).status_code
        == 403
    )
    assert written(orch) == before


def test_request_decision_links_the_item_it_blocks(
    orch: Orchestrator, project: str, alpha: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    lead_id = lead(orch, project)
    item = orch.add_board_item(project, "x").id
    foreign = orch.add_board_item(alpha, "theirs").id
    monkeypatch.setenv("ORCH_AGENT_ID", lead_id)

    reply = mcp_server.request_decision("Which?", item=item)
    plain = mcp_server.request_decision("Unrelated?")

    assert set(reply) == {"decision_id", "next"}
    assert [b["id"] for b in orch.board_detail(item)["blockers"]] == [reply["decision_id"]]
    assert actions(orch, item)[-1] == ("link", {"decision": [None, reply["decision_id"]]})
    assert orch.board_detail(item)["history"][-1]["actor"] == lead_id
    before = (written(orch), len(orch.list_decisions()))
    with pytest.raises(OrchestratorError, match="only act within project 'demo'"):
        mcp_server.request_decision("Theirs?", item=foreign)
    with pytest.raises(OrchestratorError, match="no board item #999"):
        mcp_server.request_decision("Gone?", item=999)
    assert (written(orch), len(orch.list_decisions())) == before
    assert plain["decision_id"] not in [b["id"] for b in orch.board_detail(foreign)["blockers"]]

    orch.orchestrator()
    monkeypatch.setenv("ORCH_AGENT_ID", ORCHESTRATOR_ID)
    theirs = mcp_server.request_decision("For alpha?", item=foreign)["decision_id"]
    assert [b["id"] for b in orch.board_detail(foreign)["blockers"]] == [theirs]


def test_cli_links_and_unlinks(orch: Orchestrator, project: str) -> None:
    lead_id = lead(orch, project)
    item = orch.add_board_item(project, "x").id
    decision = orch.request_decision(lead_id, "Which?").id

    linked = CliRunner().invoke(cli.app, ["board", "link", str(item), str(decision)])
    assert linked.exit_code == 0, linked.output
    assert linked.output == f"#{item} blocked by decision #{decision} (linked): Which?\n"
    shown = CliRunner().invoke(cli.app, ["board", "show", str(item)]).output.splitlines()
    assert f"  blocked by decision #{decision}: Which?" in shown
    assert any(line.endswith(f"  owner  link  decision: – → {decision}") for line in shown)
    removed = CliRunner().invoke(cli.app, ["board", "link", str(item), str(decision), "--remove"])
    assert removed.output == f"#{item} is not blocked\n"
    unknown = CliRunner().invoke(cli.app, ["board", "link", str(item), "999"])
    assert unknown.exit_code == 1 and "no decision #999" in unknown.output
