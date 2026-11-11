"""The board across projects (core/boards.py): the view, and moving items on it."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from orchestrator import board
from orchestrator.service import Orchestrator, OrchestratorError
from orchestrator.web.app import app

ORIGIN = {"origin": "http://localhost"}


@pytest.fixture
def other(orch: Orchestrator, tmp_path: Path) -> str:
    """A second project (a second repository), named to sort before the first."""
    second = tmp_path / "second"
    second.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(second)], check=True)
    return orch.add_project(str(second), "alpha", "fake").name


@pytest.fixture
def client() -> TestClient:
    return TestClient(app, base_url="http://localhost")


def ids(view: dict) -> list[int]:
    return [item["id"] for item in view["items"]]


# -- the view ------------------------------------------------------------------


def test_the_view_shows_every_project_in_order(
    orch: Orchestrator, project: str, other: str, client: TestClient
) -> None:
    done = orch.add_board_item(project, "shipped", state="done")
    idea = orch.add_board_item(project, "later", "why", agent_id=None, branch="orch/later")
    theirs = orch.add_board_item(other, "theirs", state="ready")

    view = orch.board_view()
    assert view["projects"] == [other, project] and view["synced_at"] is None
    assert ids(view) == [theirs.id, idea.id, done.id]  # by project, then state
    assert view["items"][1] == idea.to_dict()
    assert ids(orch.board_view(project)) == [idea.id, done.id]
    assert orch.board_view(project)["projects"] == [other, project]  # the filter's choices
    assert ids(orch.board_view(state="ready")) == [theirs.id]

    answer = client.get("/api/board/view")
    assert answer.status_code == 200 and answer.json() == view
    assert ids(client.get(f"/api/board/view?project={project}&state=done").json()) == [done.id]
    assert client.get("/api/board/view?project=&state=").json() == view  # empty: no filter


def test_the_view_refuses_unknown_projects_and_states(
    orch: Orchestrator, project: str, client: TestClient
) -> None:
    with pytest.raises(OrchestratorError, match="unknown project"):
        orch.board_view("nope")
    with pytest.raises(OrchestratorError, match="unknown state"):
        orch.board_view(state="doing")
    answer = client.get("/api/board/view?state=doing")
    assert answer.status_code == 400 and "unknown state" in answer.json()["error"]


def test_a_removed_projects_items_are_not_in_the_view(
    orch: Orchestrator, project: str, other: str
) -> None:
    orch.add_board_item(other, "kept for later")
    mine = orch.add_board_item(project, "mine")
    orch.remove_project(other)

    view = orch.board_view()
    assert view["projects"] == [project] and ids(view) == [mine.id]


# -- placing an item -------------------------------------------------------------


def positions(orch: Orchestrator, project: str, state: str) -> list[tuple[int, int]]:
    return [(item.id, item.position) for item in orch.board_items(project, state)]


def dense(orch: Orchestrator, project: str, state: str) -> list[int]:
    """The ids in their order, after checking that the positions are 0, 1, …"""
    listed = positions(orch, project, state)
    assert [position for _, position in listed] == list(range(len(listed)))
    return [item_id for item_id, _ in listed]


def test_place_reorders_within_a_state(orch: Orchestrator, project: str) -> None:
    a, b, c = (orch.add_board_item(project, name, state="ready").id for name in "abc")

    placed = orch.place_board_item(c, "ready", a)
    assert (placed.state, placed.position) == ("ready", 0)
    assert dense(orch, project, "ready") == [c, a, b]
    orch.place_board_item(c, "ready")  # at the end
    assert dense(orch, project, "ready") == [a, b, c]
    orch.place_board_item(a, "ready", c)
    assert dense(orch, project, "ready") == [b, a, c]


def test_place_where_it_already_is_changes_nothing(orch: Orchestrator, project: str) -> None:
    a = orch.add_board_item(project, "a", state="ready")
    b = orch.add_board_item(project, "b", state="ready")

    assert orch.place_board_item(a.id, "ready", b.id) == orch.board_item(a.id) == a
    assert orch.place_board_item(b.id, "ready") == b


def test_place_into_another_state_closes_the_gap_it_leaves(
    orch: Orchestrator, project: str
) -> None:
    a, b, c = (orch.add_board_item(project, name).id for name in "abc")
    going = orch.add_board_item(project, "going", state="in_progress").id

    moved = orch.place_board_item(b, "in_progress", going)
    assert (moved.state, moved.position) == ("in_progress", 0)
    assert moved.updated_at >= moved.created_at
    assert dense(orch, project, "in_progress") == [b, going]
    assert dense(orch, project, "idea") == [a, c]
    orch.place_board_item(a, "done")
    assert dense(orch, project, "done") == [a] and dense(orch, project, "idea") == [c]


def test_place_refuses_what_is_not_in_that_state_of_the_project(
    orch: Orchestrator, project: str, other: str
) -> None:
    item = orch.add_board_item(project, "x")
    idea = orch.add_board_item(project, "an idea").id
    theirs = orch.add_board_item(other, "theirs", state="ready").id

    for state, before, error in [
        ("ready", idea, f"#{idea} is not in {project} · ready"),
        ("ready", theirs, f"#{theirs} is not in {project} · ready"),
        ("idea", 999, "there is no board item #999"),
        ("idea", item.id, "cannot be placed before itself"),
        ("idea", True, "before must be a board item id"),
        ("idea", "3", "before must be a board item id"),
        ("doing", None, "unknown state"),
    ]:
        with pytest.raises(OrchestratorError, match=error):
            orch.place_board_item(item.id, state, before)
    with pytest.raises(OrchestratorError, match="no board item #998"):
        orch.place_board_item(998, "idea")
    assert orch.board_item(item.id) == item


def test_positions_stay_dense_with_moves_and_edits_in_between(
    orch: Orchestrator, project: str
) -> None:
    ids = [orch.add_board_item(project, f"r{n}", state="ready").id for n in range(5)]
    orch.place_board_item(ids[4], "ready", ids[0])
    orch.move_board_item(ids[4], "down")
    orch.update_board_item(ids[2], {"state": "idea"})  # leaves a gap, as before
    orch.place_board_item(ids[2], "ready", ids[1])
    orch.move_board_item(ids[3], "up")
    orch.place_board_item(ids[0], "ready")

    assert dense(orch, project, "ready") == [ids[4], ids[2], ids[3], ids[1], ids[0]]
    orch.update_board_item(ids[1], {"state": "done"})
    added = orch.add_board_item(project, "late", state="ready").id
    orch.place_board_item(added, "ready", ids[4])
    assert dense(orch, project, "ready")[0] == added


def test_api_places_an_item(orch: Orchestrator, project: str, client: TestClient) -> None:
    a = orch.add_board_item(project, "a", state="ready").id
    b = orch.add_board_item(project, "b", state="ready").id
    post = lambda path, body: client.post(path, json=body, headers=ORIGIN)  # noqa: E731

    assert client.post(f"/api/board/{b}/place", json={"state": "ready"}).status_code == 403
    placed = post(f"/api/board/{b}/place", {"state": "ready", "before": a})
    assert placed.status_code == 200
    assert placed.json() == orch.board_item(b).to_dict() and placed.json()["position"] == 0
    assert (
        post(f"/api/board/{a}/place", {"state": "done", "before": None}).json()["state"] == "done"
    )
    assert post(f"/api/board/{b}/place", {"state": "idea"}).json()["state"] == "idea"
    missing = post(f"/api/board/{b}/place", {"before": a})
    assert missing.status_code == 400 and "'state' is required" in missing.json()["error"]
    foreign = post(f"/api/board/{b}/place", {"state": "ready", "before": a})
    assert foreign.status_code == 400 and f"#{a} is not in" in foreign.json()["error"]


def test_a_place_reaches_the_record_hooks(
    orch: Orchestrator, project: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    a = orch.add_board_item(project, "a", state="ready").id
    b = orch.add_board_item(project, "b")
    seen: list[tuple] = []
    monkeypatch.setattr(
        board, "record", lambda conn, item_id, actor, action, changes, *rest: seen.append(
            (item_id, action, changes)
        )
    )  # fmt: skip
    monkeypatch.setattr(board, "record_gap", lambda conn, *args: seen.append(("gap", *args)))

    orch.place_board_item(b.id, "ready", a)
    orch.place_board_item(b.id, "ready", a)  # already there: nothing happens

    # position 0 in both states
    assert seen == [("gap", b.id, b.updated_at), (b.id, "place", {"state": ["idea", "ready"]})]


def test_api_place_answers_400_for_ids_beyond_sqlites_range(
    orch: Orchestrator, project: str, client: TestClient
) -> None:
    item = orch.add_board_item(project, "a").id
    post = lambda path, body: client.post(path, json=body, headers=ORIGIN)  # noqa: E731

    huge = post(f"/api/board/{2**63}/place", {"state": "ready"})
    assert huge.status_code == 400 and huge.json() == {"error": f"there is no board item #{2**63}"}
    before = post(f"/api/board/{item}/place", {"state": "ready", "before": 10**30})
    assert before.status_code == 400 and f"no board item #{10**30}" in before.json()["error"]
