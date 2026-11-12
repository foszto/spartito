"""The live state of board items wired through (P3): `live` in the view and the detail,
the throttled sync with its recorded move to done, list_board(live=True), `orch board
--live`, and that a failing sync breaks nothing."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from starlette.testclient import TestClient
from test_board_git import commit, merge_into_main, run_git
from test_notifications import lead
from typer.testing import CliRunner

from orchestrator import board, cli, mcp_server
from orchestrator.core import board_git
from orchestrator.service import Orchestrator
from orchestrator.web.app import app

NOTHING = {"agent": None, "git": None, "pr": None}


@pytest.fixture
def client() -> TestClient:
    return TestClient(app, base_url="http://localhost")


@pytest.fixture(autouse=True)
def roomy(monkeypatch: pytest.MonkeyPatch) -> None:
    """A pass that always has the time to check every item, however busy the machine."""
    monkeypatch.setattr(board_git, "SYNC_BUDGET", 60)


@pytest.fixture
def every_time(monkeypatch: pytest.MonkeyPatch) -> None:
    """No throttle: every board read runs a pass."""
    monkeypatch.setattr(board_git, "SYNC_EVERY", 0)


def branch_ahead(repo: Path, branch: str = "feat") -> str:
    """`branch` with one commit of its own (main stays checked out); its tip."""
    run_git(repo, "checkout", "-q", "-b", branch)
    tip = commit(repo, f"{branch}.txt")
    run_git(repo, "checkout", "-q", "main")
    return tip


def item_of(view: dict[str, Any], item_id: int) -> dict[str, Any]:
    return next(item for item in view["items"] if item["id"] == item_id)


def test_the_view_and_the_detail_show_the_agent_and_the_branch(
    orch: Orchestrator, project: str, repo: Path, client: TestClient
) -> None:
    branch_ahead(repo)
    lead_id = lead(orch, project)
    linked = orch.add_board_item(project, "feat", state="in_progress", branch="feat").id
    orch.update_board_item(linked, {"agent_id": lead_id})
    plain = orch.add_board_item(project, "nothing linked").id

    view = orch.board_view()

    assert view["synced_at"] is not None and view["synced_at"] == board_git.last_sync()
    live = item_of(view, linked)["live"]
    assert live["agent"] == {"id": lead_id, "status": "new"} and live["pr"] is None
    git = live["git"]
    assert set(git) == {"branch", "ahead", "behind", "dirty", "merged", "checked_at", "error"}
    assert (git["branch"], git["ahead"], git["behind"], git["merged"]) == ("feat", 1, 0, False)
    assert item_of(view, plain)["live"] == NOTHING
    assert client.get("/api/board/view").json() == view
    detail = client.get(f"/api/board/{linked}/detail").json()
    assert detail["live"] == live and detail == orch.board_detail(linked)
    assert orch.board_detail(plain)["live"] == NOTHING


def test_an_archived_or_unknown_agent_is_shown_as_such(orch: Orchestrator, project: str) -> None:
    lead_id = lead(orch, project)
    item = orch.add_board_item(project, "x", agent_id=lead_id).id
    orch.archive(lead_id, confirm=True)
    assert orch.board_detail(item)["live"]["agent"] == {"id": lead_id, "status": "archived"}

    with orch.store.transaction() as conn:  # as if the agent were gone
        conn.execute("UPDATE board_items SET agent_id = 'gone-1' WHERE id = ?", (item,))
    assert orch.board_detail(item)["live"]["agent"] == {"id": "gone-1", "status": "unknown"}


def test_a_merged_item_moves_to_done_and_its_history_says_so(
    orch: Orchestrator, project: str, repo: Path, every_time: None, client: TestClient
) -> None:
    tip = branch_ahead(repo)
    item = orch.add_board_item(project, "feat", state="in_progress", branch="feat")
    assert item_of(orch.board_view(), item.id)["state"] == "in_progress"

    merge_into_main(orch.project(project))
    view = client.get("/api/board/view").json()

    shown = item_of(view, item.id)
    assert shown["state"] == "done" and shown["live"]["git"]["merged"] is True
    entry = orch.board_detail(item.id)["history"][-1]
    assert entry["actor_kind"] == "auto" and entry["actor"] == board_git.AUTO_RULE == "merged"
    assert entry["action"] == "state" and entry["run_id"] is None
    assert entry["changes"] == {"state": ["in_progress", "done"], "commit": tip[:7]}
    assert entry["ts"] == orch.board_item(item.id).updated_at
    assert [e["action"] for e in orch.board_detail(item.id)["history"]] == ["add", "state"]

    shown = CliRunner().invoke(cli.app, ["board", "show", str(item.id)])
    assert f"auto: merged  state  state: in_progress → done, commit {tip[:7]}" in shown.output


def test_a_move_by_older_code_before_the_merge_stays_a_gap(
    orch: Orchestrator, project: str, repo: Path, every_time: None
) -> None:
    branch_ahead(repo)
    item = orch.add_board_item(project, "feat", state="ready", branch="feat").id
    orch.board_view()
    with orch.store.transaction() as conn:  # a change that keeps no history
        conn.execute(
            "UPDATE board_items SET title = 'renamed', updated_at = '2999-01-01T00:00:00+00:00'"
            " WHERE id = ?",
            (item,),
        )
    merge_into_main(orch.project(project))

    detail = orch.board_detail(item)
    assert detail["item"]["state"] == "done"
    assert [e["action"] for e in detail["history"]] == ["add", "unrecorded", "state"]


def test_overview_and_the_board_reads_run_the_throttled_sync(
    orch: Orchestrator, project: str, monkeypatch: pytest.MonkeyPatch, client: TestClient
) -> None:
    calls: list[Any] = []
    real = board_git.check
    monkeypatch.setattr(board_git, "check", lambda *args: calls.append(args) or real(*args))
    item = orch.add_board_item(project, "x").id

    orch.overview()
    orch.overview(archived=True)
    orch.board_view()
    client.get(f"/api/board/{item}/detail")
    mcp_server.list_board(project, live=True)
    mcp_server.list_board(project)

    assert len(calls) == 1  # once a minute at most, across processes
    monkeypatch.setattr(board_git, "SYNC_EVERY", 0)
    orch.overview()
    orch.overview(archived=True)  # the archived listing does not check
    orch.board_view()
    client.get(f"/api/board/{item}/detail")
    mcp_server.list_board(project, live=True)
    mcp_server.list_board(project)  # the default reply does not check
    CliRunner().invoke(cli.app, ["board", "--live"])
    CliRunner().invoke(cli.app, ["board"])
    assert len(calls) == 6


def test_a_failing_sync_breaks_neither_overview_nor_the_board(
    orch: Orchestrator, project: str, monkeypatch: pytest.MonkeyPatch, client: TestClient
) -> None:
    def broken(*args: Any) -> None:
        raise RuntimeError("no git today")

    monkeypatch.setattr(board_git, "sync", broken)
    item = orch.add_board_item(project, "x").id
    lead(orch, project)

    assert [view["role"] for view in orch.overview()] == ["lead"]
    assert client.get("/api/board/view").status_code == 200
    assert client.get(f"/api/board/{item}/detail").json()["live"] == NOTHING
    assert mcp_server.list_board(project, live=True)["items"] == [
        {"id": item, "state": "idea", "title": "x"}
    ]
    assert CliRunner().invoke(cli.app, ["board", "--live"]).exit_code == 0


def test_list_board_live_adds_a_short_state_per_item(
    orch: Orchestrator, project: str, repo: Path
) -> None:
    branch_ahead(repo)
    lead_id = lead(orch, project)
    linked = orch.add_board_item(project, "feat", state="ready", agent_id=lead_id, branch="feat")
    orch.add_board_item(project, "plain")

    plain = mcp_server.list_board(project)
    live = mcp_server.list_board(project, live=True)

    assert live["items"][0] == {**plain["items"][0], "run": "new", "git": "+1"}
    assert live["items"][0]["id"] == linked.id and live["items"][1] == plain["items"][1]
    assert {key: live[key] for key in ("project", "counts")} == {
        key: plain[key] for key in ("project", "counts")
    }

    listed = CliRunner().invoke(cli.app, ["board", "--live"])
    assert listed.exit_code == 0, listed.output
    assert f"  #{linked.id}  feat  [{lead_id}] [feat]  · agent new  · branch +1" in listed.output
    assert "  #2  plain\n" in listed.output
    assert "· agent" not in CliRunner().invoke(cli.app, ["board"]).output


@pytest.mark.parametrize(
    ("git", "text"),
    [
        ({"ahead": 3, "behind": 1, "dirty": 1}, "+3 -1 dirty"),
        ({"ahead": 3, "behind": None, "dirty": None}, "+3"),
        ({"ahead": 0, "behind": 2, "dirty": 0}, "-2"),
        ({"ahead": 0, "behind": 0, "dirty": 0}, "even"),
        ({"ahead": None, "behind": None, "dirty": None}, "no branch"),
        ({"ahead": 0, "behind": 2, "dirty": 0, "merged": True}, "merged"),
        ({"ahead": 1, "behind": 0, "dirty": 0, "error": "git timed out"}, "error"),
    ],
)
def test_git_text(git: dict[str, Any], text: str) -> None:
    assert board.git_text({"merged": False, "error": None, **git}) == text
