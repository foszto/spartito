"""The task board of each project (board.py): storage, MCP tools, CLI, API and dispatch."""

from __future__ import annotations

import sqlite3
import subprocess
from collections.abc import Callable
from pathlib import Path

import pytest
from starlette.testclient import TestClient
from test_notifications import lead, settle
from test_store import OLD_RUN_COLUMNS, OLD_SCHEMA, columns
from typer.testing import CliRunner

from orchestrator import board, cli, mcp_server
from orchestrator.service import ORCHESTRATOR_ID, Orchestrator, OrchestratorError
from orchestrator.store import Store
from orchestrator.web.app import app

ORIGIN = {"origin": "http://localhost"}


@pytest.fixture
def as_agent(monkeypatch: pytest.MonkeyPatch) -> Callable[[str | None], None]:
    """Make the MCP tools act for an agent (None: the interactive session)."""

    def act(agent_id: str | None) -> None:
        if agent_id:
            monkeypatch.setenv("ORCH_AGENT_ID", agent_id)
        else:
            monkeypatch.delenv("ORCH_AGENT_ID", raising=False)

    return act


@pytest.fixture
def other(orch: Orchestrator, tmp_path: Path, repo: Path) -> str:
    """A second project (a second repository)."""
    second = tmp_path / "second"
    second.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(second)], check=True)
    subprocess.run(
        ["git", "-C", str(second), "-c", "user.name=t", "-c", "user.email=t@example.com",
         "commit", "-q", "--allow-empty", "-m", "init"],
        check=True,
    )  # fmt: skip
    return orch.add_project(str(second), "other", "fake").name


def titles(orch: Orchestrator, project: str, state: str) -> list[str]:
    return [item.title for item in orch.board_items(project, state)]


def messages(orch: Orchestrator, agent_id: str) -> list[str]:
    return [run.prompt for run in orch.store.runs_of(agent_id)]


# -- storage and service -----------------------------------------------------


def test_add_edit_and_list_in_order(orch: Orchestrator, project: str) -> None:
    first = orch.add_board_item(project, "  Login page ", "with OAuth")
    second = orch.add_board_item(project, "Search")
    ready = orch.add_board_item(project, "Fix CI", state="ready")

    assert (first.title, first.description, first.state) == ("Login page", "with OAuth", "idea")
    assert (first.position, second.position, ready.position) == (0, 1, 0)
    assert first.created_at and first.updated_at == first.created_at
    assert [i.id for i in orch.board_items(project)] == [first.id, second.id, ready.id]
    assert titles(orch, project, "idea") == ["Login page", "Search"]

    edited = orch.update_board_item(first.id, {"title": "Login", "description": ""})
    assert (edited.title, edited.description, edited.state) == ("Login", "", "idea")
    assert edited.position == 0


def test_a_state_change_goes_to_the_end_of_the_new_state(orch: Orchestrator, project: str) -> None:
    a = orch.add_board_item(project, "a", state="ready")
    b = orch.add_board_item(project, "b", state="ready")
    c = orch.add_board_item(project, "c")

    moved = orch.update_board_item(c.id, {"state": "ready"})
    assert moved.state == "ready" and moved.position == 2
    assert titles(orch, project, "ready") == ["a", "b", "c"]
    back = orch.update_board_item(a.id, {"state": "idea"})
    assert back.position == 0 and titles(orch, project, "idea") == ["a"]
    same = orch.update_board_item(b.id, {"state": "ready", "title": "B"})
    assert same.position == b.position  # no change of state: stays where it is


def test_move_up_and_down_within_a_state(orch: Orchestrator, project: str) -> None:
    a, b, c = (orch.add_board_item(project, name) for name in "abc")
    orch.add_board_item(project, "elsewhere", state="ready")

    orch.move_board_item(c.id, "up")
    assert titles(orch, project, "idea") == ["a", "c", "b"]
    orch.move_board_item(a.id, "down")
    assert titles(orch, project, "idea") == ["c", "a", "b"]
    orch.move_board_item(c.id, "up")  # at the top: nothing changes
    orch.move_board_item(b.id, "down")  # at the bottom: nothing changes
    assert titles(orch, project, "idea") == ["c", "a", "b"]
    assert titles(orch, project, "ready") == ["elsewhere"]
    with pytest.raises(OrchestratorError, match="'up' or 'down'"):
        orch.move_board_item(a.id, "sideways")


def test_moves_after_state_changes_and_equal_positions(orch: Orchestrator, project: str) -> None:
    a, b, c = (orch.add_board_item(project, name) for name in "abc")
    orch.update_board_item(b.id, {"state": "done"})  # leaves a gap
    with orch.store.connection() as conn:  # a tie, as a restored dispatch could leave
        conn.execute("UPDATE board_items SET position = 0 WHERE id = ?", (c.id,))
    assert titles(orch, project, "idea") == ["a", "c"]
    orch.move_board_item(c.id, "up")
    assert titles(orch, project, "idea") == ["c", "a"]
    assert [i.position for i in orch.board_items(project, "idea")] == [0, 1]


def test_unknown_project_state_item_and_bad_fields_are_refused(
    orch: Orchestrator, project: str, other: str
) -> None:
    with pytest.raises(OrchestratorError, match="unknown project 'nope'"):
        orch.add_board_item("nope", "x")
    with pytest.raises(OrchestratorError, match="unknown project"):
        orch.board_items("nope")
    with pytest.raises(OrchestratorError, match="unknown state 'started'"):
        orch.add_board_item(project, "x", state="started")
    with pytest.raises(OrchestratorError, match="unknown state"):
        orch.board_items(project, "started")
    with pytest.raises(OrchestratorError, match="title must not be empty"):
        orch.add_board_item(project, "  ")
    with pytest.raises(OrchestratorError, match="too long"):
        orch.add_board_item(project, "x" * (board.TITLE_CHARS + 1))
    item = orch.add_board_item(project, "x")
    with pytest.raises(OrchestratorError, match="unknown state"):
        orch.update_board_item(item.id, {"state": "started"})
    with pytest.raises(OrchestratorError, match="no board item #999"):
        orch.update_board_item(999, {"title": "y"})
    with pytest.raises(OrchestratorError, match="unknown fields: position"):
        orch.update_board_item(item.id, {"position": 5})
    stranger = lead(orch, other)
    with pytest.raises(OrchestratorError, match=f"no agent '{stranger}' in project '{project}'"):
        orch.update_board_item(item.id, {"agent_id": stranger})
    assert orch.board_item(item.id).title == "x" and orch.board_item(item.id).agent_id is None


def test_a_removed_projects_items_are_kept_but_not_listed(
    orch: Orchestrator, project: str, repo: Path
) -> None:
    item = orch.add_board_item(project, "x")
    orch.remove_project(project)
    assert orch.board_items() == []
    orch.add_project(str(repo), project, "fake")
    assert orch.board_items() == [item]


def test_links_to_an_agent_and_a_branch_are_set_and_cleared(
    orch: Orchestrator, project: str
) -> None:
    lead_id = lead(orch, project)
    item = orch.add_board_item(project, "x")

    linked = orch.update_board_item(item.id, {"agent_id": lead_id, "branch": "orch/x-1"})
    assert (linked.agent_id, linked.branch) == (lead_id, "orch/x-1")
    cleared = orch.update_board_item(item.id, {"agent_id": "", "branch": ""})
    assert (cleared.agent_id, cleared.branch) == (None, None)


def test_an_old_database_opens_with_the_board_table(tmp_path: Path) -> None:
    path = tmp_path / "old.db"
    with sqlite3.connect(path) as conn:
        conn.executescript(OLD_SCHEMA)
    conn.close()

    store = Store(path)
    item = board.add(store, "p", "Keep it", "on the board")

    assert board.listing(store) == [item] and item.project == "p"
    assert "board_items" in {
        row[0] for row in sqlite3.connect(path).execute("SELECT name FROM sqlite_master")
    }
    assert columns(path, "runs") == OLD_RUN_COLUMNS  # older code still reads its runs
    Store(path)  # opening it again changes nothing
    assert board.listing(store) == [item]


# -- MCP tools ---------------------------------------------------------------


def test_mcp_tools_add_list_and_update(orch: Orchestrator, project: str) -> None:
    added = mcp_server.add_board_item(project, "Login page", "x" * 500)
    assert added == {"id": added["id"], "project": project, "state": "idea"}
    mcp_server.add_board_item(project, "Fix CI", state="ready")

    listed = mcp_server.list_board(project)
    assert listed["project"] == project and listed["counts"] == {"idea": 1, "ready": 1}
    first, second = listed["items"]
    assert first["id"] == added["id"] and first["state"] == "idea"
    assert len(first["description"]) < 300 and "cut at 200 of 500" in first["description"]
    assert second == {"id": second["id"], "state": "ready", "title": "Fix CI"}
    assert mcp_server.list_board(project, "ready")["items"] == [second]

    lead_id = lead(orch, project)
    updated = mcp_server.update_board_item(added["id"], state="in_progress", agent=lead_id)
    assert updated["state"] == "in_progress" and updated["agent_id"] == lead_id
    assert mcp_server.update_board_item(added["id"], agent="")["state"] == "in_progress"
    assert "agent_id" not in mcp_server.list_board(project, "in_progress")["items"][0]
    with pytest.raises(OrchestratorError, match="nothing to change"):
        mcp_server.update_board_item(added["id"])


def test_list_board_is_bounded(orch: Orchestrator, project: str) -> None:
    for n in range(mcp_server.BOARD_ITEMS + 5):
        orch.add_board_item(project, f"item {n}", "d" * 4000, state="done" if n % 2 else "idea")

    reply = mcp_server.list_board(project)
    assert len(reply["items"]) == mcp_server.BOARD_ITEMS
    assert reply["counts"] == {"idea": 28, "done": 27}
    assert "5 more not shown" in reply["note"]
    assert len(str(reply)) < mcp_server.BOARD_ITEMS * 400


def test_a_lead_is_confined_to_its_own_project(
    orch: Orchestrator, project: str, other: str, as_agent: Callable
) -> None:
    lead_id = lead(orch, project, parent=None)
    foreign = orch.add_board_item(other, "theirs")
    as_agent(lead_id)

    own = mcp_server.add_board_item(project, "mine")
    assert mcp_server.list_board(project)["items"][0]["id"] == own["id"]
    with pytest.raises(OrchestratorError, match=f"only act within project '{project}'"):
        mcp_server.list_board(other)
    with pytest.raises(OrchestratorError, match="only act within"):
        mcp_server.add_board_item(other, "sneaky")
    with pytest.raises(OrchestratorError, match="only act within"):
        mcp_server.update_board_item(foreign.id, state="done")
    as_agent(None)
    assert orch.board_item(foreign.id).state == "idea"
    assert titles(orch, other, "idea") == ["theirs"]


def test_the_orchestrator_names_the_project(
    orch: Orchestrator, project: str, as_agent: Callable
) -> None:
    orch.orchestrator()
    as_agent(ORCHESTRATOR_ID)
    with pytest.raises(OrchestratorError, match="project is required"):
        mcp_server.list_board("")
    assert mcp_server.add_board_item(project, "an idea")["project"] == project


# -- dispatch ----------------------------------------------------------------


def test_dispatch_sends_the_ready_items_to_the_lead_in_one_message(
    orch: Orchestrator, project: str
) -> None:
    idea = orch.add_board_item(project, "Later")
    one = orch.add_board_item(project, "Login page", "with OAuth\nand tests", state="ready")
    two = orch.add_board_item(project, "Fix CI", state="ready")
    orch.move_board_item(two.id, "up")
    started = orch.add_board_item(project, "Already going", state="in_progress")

    result = orch.dispatch_board(project)
    lead_id = result["agent_id"]
    assert orch.agent(lead_id).role == "lead" and orch.lead(project).id == lead_id
    assert [item["id"] for item in result["items"]] == [two.id, one.id]
    (text,) = messages(orch, lead_id)
    assert result["run_id"] == orch.store.runs_of(lead_id)[0].id
    assert orch.store.meta(result["run_id"]).sender is None  # a message from the user
    assert text.startswith(f"Board items ready for project {project} (2 items)")
    assert f"#{two.id} Fix CI" in text and f"#{one.id} Login page\nwith OAuth\nand tests" in text
    assert text.index(f"#{two.id}") < text.index(f"#{one.id}")
    assert "update_board_item" in text.splitlines()[-1]
    assert "Later" not in text and "Already going" not in text

    progress = orch.board_items(project, "in_progress")
    assert [i.id for i in progress] == [started.id, two.id, one.id]
    assert [i.agent_id for i in progress] == [None, lead_id, lead_id]
    assert orch.board_items(project, "ready") == [] and orch.board_item(idea.id).state == "idea"

    again = orch.dispatch_board(project)  # nothing is ready any more: nothing is sent
    assert again == {"items": [], "agent_id": None, "run_id": None}
    assert len(messages(orch, lead_id)) == 1
    settle(orch, lead_id)


def test_nothing_ready_sends_nothing_and_creates_no_lead(orch: Orchestrator, project: str) -> None:
    orch.add_board_item(project, "just an idea")
    assert orch.dispatch_board(project) == {"items": [], "agent_id": None, "run_id": None}
    assert orch.store.list_agents(project) == []
    with pytest.raises(OrchestratorError, match="unknown project"):
        orch.dispatch_board("nope")


def test_a_failed_send_leaves_the_items_ready(
    orch: Orchestrator, project: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    item = orch.add_board_item(project, "x", state="ready")

    def broken(*args, **kwargs):
        raise OrchestratorError("cannot send")

    lead_id = lead(orch, project)
    monkeypatch.setattr(orch, "send", broken)
    with pytest.raises(OrchestratorError, match="cannot send"):
        orch.dispatch_board(project)
    restored = orch.board_item(item.id)
    assert (restored.state, restored.agent_id, restored.position) == ("ready", None, 0)
    assert messages(orch, lead_id) == []


# -- CLI and API -------------------------------------------------------------


def test_cli_lists_adds_edits_moves_and_dispatches(orch: Orchestrator, project: str) -> None:
    runner = CliRunner()

    assert runner.invoke(cli.app, ["board"]).output.strip() == "the board is empty"
    added = runner.invoke(cli.app, ["board", "add", "-p", project, "Login", "-d", "OAuth"])
    assert added.exit_code == 0, added.output
    first = orch.board_items(project)[0].id
    assert f"#{first} added to {project} · idea" in added.output
    runner.invoke(cli.app, ["board", "add", "-p", project, "Search", "--state", "idea"])
    second = orch.board_items(project)[1].id

    listed = runner.invoke(cli.app, ["board", "-p", project])
    assert listed.exit_code == 0
    assert f"{project} · idea" in listed.output and f"#{first}  Login" in listed.output
    assert "OAuth" in listed.output

    moved = runner.invoke(cli.app, ["board", "move", str(second), "up"])
    assert moved.exit_code == 0 and f"#{second} is now 1 of 2 in {project} · idea" in moved.output
    edited = runner.invoke(
        cli.app, ["board", "edit", str(first), "--state", "ready", "--branch", "orch/login"]
    )
    assert edited.exit_code == 0, edited.output
    assert f"#{first} {project} · ready" in edited.output and "[orch/login]" in edited.output
    ready = runner.invoke(cli.app, ["board", "-p", project, "--state", "ready"]).output
    assert f"#{first}  Login" in ready and "Search" not in ready

    bad = runner.invoke(cli.app, ["board", "edit", str(first), "--state", "nope"])
    assert bad.exit_code == 1 and "unknown state" in bad.output
    unknown = runner.invoke(cli.app, ["board", "add", "-p", "nope", "x"])
    assert unknown.exit_code == 1 and "unknown project" in unknown.output
    assert runner.invoke(cli.app, ["board", "edit", str(first)]).exit_code == 1

    sent = runner.invoke(cli.app, ["board", "dispatch", "-p", project])
    assert sent.exit_code == 0, sent.output
    lead_id = orch.lead(project).id
    assert f"#{first} -> {lead_id}  run" in sent.output
    none = runner.invoke(cli.app, ["board", "dispatch", "-p", project])
    assert "nothing sent" in none.output and len(messages(orch, lead_id)) == 1
    settle(orch, lead_id)


def test_api_lists_adds_edits_moves_and_dispatches(orch: Orchestrator, project: str) -> None:
    client = TestClient(app, base_url="http://localhost")
    lead_id = lead(orch, project)

    assert client.post("/api/board/add", json={"project": project, "title": "x"}).status_code == 403
    added = client.post(
        "/api/board/add", json={"project": project, "title": "Login", "description": "d"},
        headers=ORIGIN,
    )  # fmt: skip
    assert added.status_code == 200 and added.json()["state"] == "idea"
    first = added.json()["id"]
    second = client.post(
        "/api/board/add", json={"project": project, "title": "CI", "state": "ready"}, headers=ORIGIN
    ).json()["id"]
    bad = client.post("/api/board/add", json={"project": project, "title": ""}, headers=ORIGIN)
    assert bad.status_code == 400 and "title" in bad.json()["error"]
    assert client.get("/api/board").status_code == 400  # project is required

    items = client.get(f"/api/board?project={project}").json()["items"]
    assert [item["id"] for item in items] == [first, second]
    assert client.get(f"/api/board?project={project}&state=ready").json()["items"] == [items[1]]

    edited = client.post(
        f"/api/board/{first}",
        json={"title": "Login page", "state": "ready", "agent": lead_id, "branch": "b"},
        headers=ORIGIN,
    )
    assert edited.status_code == 200
    assert edited.json() | {"updated_at": ""} == items[0] | {
        "title": "Login page", "state": "ready", "position": 1, "agent_id": lead_id,
        "branch": "b", "updated_at": "",
    }  # fmt: skip
    cleared = client.post(f"/api/board/{first}", json={"agent": None}, headers=ORIGIN).json()
    assert cleared["agent_id"] is None and cleared["branch"] == "b"
    moved = client.post(f"/api/board/{first}/move", json={"direction": "up"}, headers=ORIGIN)
    assert moved.status_code == 200
    ready = client.get(f"/api/board?project={project}&state=ready").json()["items"]
    assert [item["id"] for item in ready] == [first, second]
    missing = client.post("/api/board/999", json={"title": "y"}, headers=ORIGIN)
    assert missing.status_code == 400 and "#999" in missing.json()["error"]

    sent = client.post("/api/board/dispatch", json={"project": project}, headers=ORIGIN).json()
    assert sent["agent_id"] == lead_id and [item["id"] for item in sent["items"]] == [first, second]
    assert all(item["state"] == "in_progress" for item in sent["items"])
    again = client.post("/api/board/dispatch", json={"project": project}, headers=ORIGIN).json()
    assert again["items"] == [] and len(messages(orch, lead_id)) == 1
    settle(orch, lead_id)
