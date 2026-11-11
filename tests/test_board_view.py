"""The board across projects (core/boards.py): the view, and moving items on it."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from starlette.testclient import TestClient

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
