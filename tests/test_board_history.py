"""The history of each board item (board_history): who changed what in which run, the
item's detail with the runs that worked on it, and the gaps left by older code."""

from __future__ import annotations

import importlib.util
import sqlite3
import sys
from collections.abc import Callable
from pathlib import Path
from types import ModuleType

import pytest
from starlette.testclient import TestClient
from test_notifications import lead
from typer.testing import CliRunner

from orchestrator import board, cli, mcp_server
from orchestrator.models import RunStatus
from orchestrator.service import ORCHESTRATOR_ID, Orchestrator, OrchestratorError
from orchestrator.store import Store
from orchestrator.web.app import app

FIXTURES = Path(__file__).parent / "fixtures"
OWNER = {"actor_kind": "owner", "actor": None}


@pytest.fixture(autouse=True)
def no_runner(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(Orchestrator, "_launch", lambda self, run: None)


@pytest.fixture
def as_agent(monkeypatch: pytest.MonkeyPatch) -> Callable[[str | None], None]:
    def act(agent_id: str | None) -> None:
        if agent_id:
            monkeypatch.setenv("ORCH_AGENT_ID", agent_id)
        else:
            monkeypatch.delenv("ORCH_AGENT_ID", raising=False)

    return act


def entries(orch: Orchestrator, item_id: int) -> list[tuple]:
    """(actor_kind, actor, action, changes, run_id) of each entry."""
    return [
        (e["actor_kind"], e["actor"], e["action"], e["changes"], e["run_id"])
        for e in orch.board_detail(item_id)["history"]
    ]


def running(orch: Orchestrator, agent_id: str) -> int:
    run = orch.store.add_run(agent_id, "a turn")
    orch.store.update_run(run.id, status=RunStatus.RUNNING, started_at=run.created_at)
    return run.id


# -- recording -------------------------------------------------------------------


def test_every_change_is_recorded_with_who_made_it(orch: Orchestrator, project: str) -> None:
    item = orch.add_board_item(project, "Login", "first", branch="orch/login").id
    other = orch.add_board_item(project, "Search").id
    orch.update_board_item(item, {"title": "Login page", "description": "second"})
    orch.update_board_item(item, {"state": "ready"})
    orch.update_board_item(other, {"state": "ready"})
    orch.move_board_item(other, "up")
    orch.move_board_item(item, "up")
    orch.move_board_item(item, "up")  # at the top already: nothing happens
    result = orch.dispatch_board(project, [item])

    o = ("owner", None)
    assert entries(orch, item) == [
        (*o, "add", {"state": [None, "idea"], "branch": [None, "orch/login"]}, None),
        (*o, "edit", {"title": ["Login", "Login page"], "description": "changed"}, None),
        (*o, "state", {"state": ["idea", "ready"]}, None),
        (*o, "move", {"position": [1, 0]}, None),
        (
            *o,
            "dispatch",
            {"state": ["ready", "in_progress"], "agent_id": [None, result["agent_id"]]},
            result["run_id"],
        ),
    ]
    assert entries(orch, other) == [
        (*o, "add", {"state": [None, "idea"]}, None),
        (*o, "state", {"state": ["idea", "ready"]}, None),
        (*o, "move", {"position": [1, 0]}, None),
    ]
    detail = orch.board_detail(item)
    assert detail["unrecorded"] is False and detail["item"] == orch.board_item(item).to_dict()
    assert detail["history"][-1]["ts"] == detail["item"]["updated_at"]
    assert set(detail["history"][0]) == {"ts", "actor_kind", "actor", "action", "changes", "run_id"}


def test_an_agents_change_names_it_and_its_running_turn(
    orch: Orchestrator, project: str, as_agent: Callable
) -> None:
    lead_id = lead(orch, project)
    as_agent(lead_id)
    added = mcp_server.add_board_item(project, "found while working")["id"]  # no turn running
    turn = running(orch, lead_id)
    mcp_server.update_board_item(added, state="in_progress", agent=lead_id)
    as_agent(None)
    mcp_server.update_board_item(added, title="renamed in the chat session")

    assert entries(orch, added) == [
        ("agent", lead_id, "add", {"state": [None, "idea"]}, None),
        (
            "agent",
            lead_id,
            "edit",
            {"state": ["idea", "in_progress"], "agent_id": [None, lead_id]},
            turn,
        ),
        ("agent", None, "edit", {"title": ["found while working", "renamed in the chat session"]}, None),
    ]  # fmt: skip
    assert [run["id"] for run in orch.board_detail(added)["runs"]] == [turn]


def test_the_orchestrators_dispatch_names_it(orch: Orchestrator, project: str) -> None:
    item = orch.add_board_item(project, "x", state="ready").id
    orch.orchestrator()
    reply = orch.dispatch_board(project, actor=board.Actor(board.AGENT_KIND, ORCHESTRATOR_ID))

    kind, actor, action, changes, run_id = entries(orch, item)[-1]
    assert (kind, actor, action, run_id) == ("agent", ORCHESTRATOR_ID, "dispatch", reply["run_id"])
    assert changes == {"state": ["ready", "in_progress"], "agent_id": [None, reply["agent_id"]]}


def test_a_failed_dispatch_records_its_restore(
    orch: Orchestrator, project: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    item = orch.add_board_item(project, "x", state="ready").id
    lead_id = lead(orch, project)

    def broken(*args, **kwargs):
        raise OrchestratorError("cannot send")

    monkeypatch.setattr(orch, "send", broken)
    with pytest.raises(OrchestratorError, match="cannot send"):
        orch.dispatch_board(project)
    assert entries(orch, item)[1:] == [
        ("owner", None, "dispatch", {"state": ["ready", "in_progress"], "agent_id": [None, lead_id]}, None),
        ("owner", None, "restore", {"state": ["in_progress", "ready"], "agent_id": [lead_id, None]}, None),
    ]  # fmt: skip
    assert orch.board_detail(item)["unrecorded"] is False  # restored as it was
    orch.update_board_item(item, {"title": "y"})  # its old updated_at is no gap
    assert [e[2] for e in entries(orch, item)] == ["add", "dispatch", "restore", "edit"]


# -- the runs that worked on an item -------------------------------------------------


def test_the_runs_are_those_its_history_names(
    orch: Orchestrator, project: str, as_agent: Callable
) -> None:
    item = orch.add_board_item(project, "x", state="ready").id
    other = orch.add_board_item(project, "y", state="ready").id
    lead_id = lead(orch, project)
    before = orch.store.add_run(lead_id, "earlier work").id
    dispatched = orch.dispatch_board(project, [item])["run_id"]
    turn = running(orch, lead_id)
    as_agent(lead_id)
    mcp_server.update_board_item(item, branch="orch/x")  # in that turn
    mcp_server.update_board_item(other, title="y2")  # the same turn, another item
    as_agent(None)
    after = orch.store.add_run(lead_id, "other work").id

    runs = orch.board_detail(item)["runs"]
    assert [run["id"] for run in runs] == [dispatched, turn]
    assert {before, after}.isdisjoint(run["id"] for run in runs)
    assert runs[0] == {
        "id": dispatched,
        "agent_id": lead_id,
        "status": "waiting",
        "created_at": orch.get_run(dispatched).created_at,
        "ended_at": None,
    }
    assert [run["id"] for run in orch.board_detail(other)["runs"]] == [turn]


def at(minute: int) -> str:
    return f"2026-01-01T10:{minute:02d}:00+00:00"


# -- API and CLI --------------------------------------------------------------------


def test_api_and_cli_show_the_detail(orch: Orchestrator, project: str) -> None:
    item = orch.add_board_item(project, "Login", "with OAuth\nand tests", state="ready").id
    orch.update_board_item(item, {"description": "with OAuth"})
    sent = orch.dispatch_board(project)

    answer = TestClient(app, base_url="http://localhost").get(f"/api/board/{item}/detail")
    assert answer.status_code == 200 and answer.json() == orch.board_detail(item)
    assert set(answer.json()) == {"item", "history", "runs", "unrecorded"}
    missing = TestClient(app, base_url="http://localhost").get("/api/board/999/detail")
    assert missing.status_code == 400 and "#999" in missing.json()["error"]

    shown = CliRunner().invoke(cli.app, ["board", "show", str(item)])
    assert shown.exit_code == 0, shown.output
    lines = shown.output.splitlines()
    lead_id = sent["agent_id"]
    assert lines[0] == f"#{item}  Login"
    assert lines[1] == f"  {project} · in_progress  [{lead_id}]"
    assert lines[3] == "  | with OAuth" and lines[4] == "history"
    assert lines[5].endswith("  owner  add  state: – → ready")
    assert lines[6].endswith("  owner  edit  description changed")
    assert lines[7].endswith(
        f"  owner (run {sent['run_id']})  dispatch  state: ready → in_progress,"
        f" agent_id: – → {lead_id}"
    )
    assert lines[8] == "runs" and len(lines) == 10
    assert lines[9].startswith(f"  run {sent['run_id']}  {lead_id}  waiting  20")
    unknown = CliRunner().invoke(cli.app, ["board", "show", "999"])
    assert unknown.exit_code == 1 and "#999" in unknown.output


# -- older databases and older code ----------------------------------------------------


def master_database(path: Path) -> None:
    """A database as master's code made it, with board items in it."""
    with sqlite3.connect(path) as conn:
        conn.execute("PRAGMA journal_mode=wal")
        conn.executescript((FIXTURES / "board_master_schema.sql").read_text())
        conn.execute("INSERT INTO projects VALUES ('demo', '/tmp/demo', 'main', 'fake', 't')")
        for title, state in (("old idea", "idea"), ("old task", "ready")):
            conn.execute(
                "INSERT INTO board_items (project, title, state, created_at, updated_at)"
                " VALUES ('demo', ?, ?, ?, ?)",
                (title, state, at(0), at(0)),
            )
    conn.close()


@pytest.fixture
def master_board(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    """master's board.py, working against today's store."""
    name = "orchestrator._master_board"
    spec = importlib.util.spec_from_file_location(name, FIXTURES / "master_board.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, name, module)
    spec.loader.exec_module(module)
    return module


def test_a_master_database_with_board_items_opens_and_reads(tmp_path: Path) -> None:
    path = tmp_path / "master.db"
    master_database(path)

    orch = Orchestrator(Store(path))

    assert [item.title for item in orch.board_items("demo")] == ["old idea", "old task"]
    detail = orch.board_detail(1)
    assert detail["history"] == [] and detail["unrecorded"] is True and detail["runs"] == []
    assert orch.board_view()["projects"] == ["demo"]
    orch.update_board_item(1, {"state": "ready"})
    detail = orch.board_detail(1)
    assert detail["unrecorded"] is False
    gap, change = detail["history"]  # what came before is kept as a gap
    assert gap == {
        "ts": at(0), "actor_kind": None, "actor": None, "action": "unrecorded", "changes": {},
        "run_id": None,
    }  # fmt: skip
    assert change["action"] == "state" and change["changes"] == {"state": ["idea", "ready"]}
    Store(path)  # opening it again changes nothing
    assert len(orch.board_detail(1)["history"]) == 2
    with sqlite3.connect(path) as conn:  # master's schema runs on it unchanged
        conn.executescript((FIXTURES / "board_master_schema.sql").read_text())
    conn.close()


def test_masters_board_code_writes_and_its_changes_show_as_unrecorded(
    orch: Orchestrator, project: str, master_board: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    old = master_board.add(orch.store, project, "added by old code")
    assert orch.board_detail(old.id)["unrecorded"] is True

    item = orch.add_board_item(project, "added by new code").id
    assert orch.board_detail(item)["unrecorded"] is False
    monkeypatch.setattr(master_board, "now", lambda: "2999-01-01T00:00:00+00:00")
    master_board.update(orch.store, item, {"state": "ready"})
    master_board.move(orch.store, old.id, "down")

    detail = orch.board_detail(item)
    assert detail["unrecorded"] is True and detail["item"]["state"] == "ready"
    assert [entry["action"] for entry in detail["history"]] == ["add"]
    assert [i.title for i in orch.board_items(project, "idea")] == ["added by old code"]
    lead_id = lead(orch, project)
    items, run_id = master_board.dispatch(orch.store, project, lead_id, lambda a, t: 7)
    assert [i.id for i in items] == [item] and run_id == 7
    assert orch.board_detail(item)["unrecorded"] is True
    orch.update_board_item(item, {"state": "done"})  # recorded again: the gap stays
    detail = orch.board_detail(item)
    assert detail["unrecorded"] is False
    assert [entry["action"] for entry in detail["history"]] == ["add", "unrecorded", "state"]
    assert detail["history"][1]["ts"] == "2999-01-01T00:00:00+00:00"
    assert detail["history"][-1]["changes"] == {"state": ["in_progress", "done"]}
    orch.update_board_item(item, {"title": "again"})  # no new gap
    assert [entry["action"] for entry in orch.board_detail(item)["history"]][-2:] == [
        "state",
        "edit",
    ]
    assert board.listing(orch.store, project, "done")[0].id == item


def test_a_change_that_changes_nothing_leaves_no_entry_and_no_gap(
    orch: Orchestrator, project: str
) -> None:
    item = orch.add_board_item(project, "same", "text", state="ready", branch="b").id

    orch.update_board_item(item, {"title": "same", "state": "ready", "branch": "b"})
    mcp_server.update_board_item(item, description="text")

    detail = orch.board_detail(item)
    assert [entry["action"] for entry in detail["history"]] == ["add"]
    assert detail["unrecorded"] is False
    orch.update_board_item(item, {"title": "changed"})
    assert [entry["action"] for entry in orch.board_detail(item)["history"]] == ["add", "edit"]


def test_detail_answers_400_for_an_id_beyond_sqlites_range(orch: Orchestrator) -> None:
    client = TestClient(app, base_url="http://localhost")
    for item_id in (2**63, 10**30):
        answer = client.get(f"/api/board/{item_id}/detail")
        assert answer.status_code == 400
        assert answer.json() == {"error": f"there is no board item #{item_id}"}
    shown = CliRunner().invoke(cli.app, ["board", "show", str(2**63)])
    assert shown.exit_code == 1 and f"no board item #{2**63}" in shown.output
