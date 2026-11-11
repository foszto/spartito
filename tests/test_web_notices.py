"""The notice routes a dashboard page polls to show desktop notifications itself
(GET /api/notices, POST /api/notices/{id}/claim; desktop.py for the notices)."""

from __future__ import annotations

import os
import time

import pytest
from starlette.testclient import TestClient

from orchestrator import desktop
from orchestrator.service import Orchestrator
from orchestrator.web.app import app

ORIGIN = {"origin": "http://localhost"}  # what the page itself sends with a POST


@pytest.fixture
def client(orch: Orchestrator) -> TestClient:
    return TestClient(app, base_url="http://localhost")


def add(orch: Orchestrator, kind: str = "finished", decision_id: int | None = None) -> int:
    return orch.store.add_notice(kind, "a-1", f"{kind} text", decision_id)


def test_without_after_only_the_cursor(client: TestClient, orch: Orchestrator) -> None:
    assert client.get("/api/notices").json() == {"notices": [], "cursor": 0}
    add(orch)
    second = add(orch, "decision", 7)

    assert client.get("/api/notices").json() == {"notices": [], "cursor": second}


def test_after_a_cursor_the_unclaimed_notices_oldest_first(
    client: TestClient, orch: Orchestrator
) -> None:
    first = add(orch)
    second = add(orch, "decision", 7)
    third = add(orch)
    orch.claim_notice(third)

    data = client.get(f"/api/notices?after={first - 1}").json()

    assert data["cursor"] == third
    assert [n["id"] for n in data["notices"]] == [first, second]
    assert data["notices"][1] == {
        "id": second,
        "kind": "decision",
        "agent_id": "a-1",
        "decision_id": 7,
        "text": "decision text",
        "created_at": data["notices"][1]["created_at"],
    }
    assert client.get(f"/api/notices?after={second}").json() == {"notices": [], "cursor": third}


def test_a_bad_after_is_refused(client: TestClient) -> None:
    response = client.get("/api/notices?after=x")
    assert response.status_code == 400 and "after" in response.json()["error"]


def test_notify_marks_a_page_present_and_its_absence_does_not(client: TestClient) -> None:
    marker = desktop.page_marker()
    client.get("/api/notices?after=0")
    assert not marker.exists() and not desktop.page_fresh()

    client.get("/api/notices?notify=1")
    assert desktop.page_fresh()

    then = time.time() - desktop.PAGE_FRESH_SECONDS - 5
    os.utime(marker, (then, then))
    client.get("/api/notices?after=0&notify=0")
    assert not desktop.page_fresh()
    client.get("/api/notices?after=0&notify=1")
    assert desktop.page_fresh()


def test_a_claim_succeeds_once(client: TestClient, orch: Orchestrator) -> None:
    notice = add(orch)

    first = client.post(f"/api/notices/{notice}/claim", json={}, headers=ORIGIN)
    second = client.post(f"/api/notices/{notice}/claim", json={}, headers=ORIGIN)

    assert first.status_code == 200 and first.json() == {"claimed": True}
    assert second.json() == {"claimed": False}
    assert client.post("/api/notices/999/claim", json={}, headers=ORIGIN).json() == {
        "claimed": False
    }
    assert client.get("/api/notices?after=0").json()["notices"] == []


def test_the_routes_keep_the_host_and_origin_checks(client: TestClient, orch: Orchestrator) -> None:
    notice = add(orch)
    claim = f"/api/notices/{notice}/claim"

    for path in ("/api/notices", "/api/notices?after=0&notify=1"):
        refused = client.get(path, headers={"host": "evil.example"})
        assert refused.status_code == 403 and refused.json() == {"error": "unexpected Host header"}
    assert not desktop.page_marker().exists()  # a refused poll does not count as a page
    assert client.post(claim, json={}, headers={"host": "evil.example"}).status_code == 403
    cross = client.post(claim, json={}, headers={"origin": "http://evil.example"})
    assert cross.status_code == 403 and cross.json() == {"error": "cross-origin request refused"}
    assert client.post(claim, content=b"{}", headers=ORIGIN).status_code == 415  # not JSON
    assert client.get("/api/notices", headers={"origin": "http://evil.example"}).status_code == 200

    assert [n["id"] for n in client.get("/api/notices?after=0").json()["notices"]] == [notice]
