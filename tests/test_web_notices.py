"""The notice routes a dashboard page polls to show desktop notifications itself
(POST /api/notices/poll, POST /api/notices/{id}/claim; desktop.py for the notices)."""

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


def poll(client: TestClient, after: object = None, **headers: str):
    return client.post("/api/notices/poll", json={"after": after}, headers={**ORIGIN, **headers})


def add(orch: Orchestrator, kind: str = "finished", decision_id: int | None = None) -> int:
    return orch.store.add_notice(kind, "a-1", f"{kind} text", decision_id)


def test_without_after_only_the_cursor(client: TestClient, orch: Orchestrator) -> None:
    assert poll(client).json() == {"notices": [], "cursor": 0}
    add(orch)
    second = add(orch, "decision", 7)

    assert poll(client).json() == {"notices": [], "cursor": second}
    assert client.post("/api/notices/poll", json={}, headers=ORIGIN).json()["notices"] == []


def test_after_a_cursor_the_unclaimed_notices_oldest_first(
    client: TestClient, orch: Orchestrator
) -> None:
    first = add(orch)
    second = add(orch, "decision", 7)
    third = add(orch)
    orch.claim_notice(third)

    data = poll(client, first - 1).json()

    assert data["cursor"] == third
    assert [n["id"] for n in data["notices"]] == [first, second]
    assert data["notices"][1] == {
        "id": second,
        "kind": "decision",
        "agent_id": "a-1",
        "decision_id": 7,
        "text": "decision text",
        "created_at": data["notices"][1]["created_at"],
        "route": None,
    }
    assert poll(client, second).json() == {"notices": [], "cursor": third}
    assert poll(client, 10**6).json() == {"notices": [], "cursor": third}  # above: the page resets


@pytest.mark.parametrize("after", ["x", "3", 1.5, True, -1, 2**63, 10**30])
def test_a_bad_after_is_refused_without_marking_a_page(client: TestClient, after: object) -> None:
    response = poll(client, after)

    assert response.status_code == 400 and response.json() == {
        "error": "'after' must be a notice id"
    }
    assert not desktop.page_marker().exists()


def test_the_largest_id_sqlite_holds_is_accepted(client: TestClient, orch: Orchestrator) -> None:
    add(orch)
    assert poll(client, 2**63 - 1).json() == {"notices": [], "cursor": 1}
    claim = client.post(f"/api/notices/{2**63 - 1}/claim", json={}, headers=ORIGIN)
    assert claim.json() == {"claimed": False}


def test_a_claim_of_an_id_beyond_sqlite_is_refused(client: TestClient) -> None:
    response = client.post(f"/api/notices/{2**63}/claim", json={}, headers=ORIGIN)
    assert response.status_code == 400 and response.json() == {
        "error": "'notice id' must be a notice id"
    }


def test_only_a_same_origin_poll_marks_a_page_present(client: TestClient) -> None:
    marker = desktop.page_marker()
    assert poll(client).status_code == 200 and desktop.page_fresh()

    then = time.time() - desktop.PAGE_FRESH_SECONDS - 5
    os.utime(marker, (then, then))
    cross = poll(client, 0, origin="http://evil.example")
    assert cross.status_code == 403 and cross.json() == {"error": "cross-origin request refused"}
    assert not desktop.page_fresh()

    assert poll(client, 0).status_code == 200 and desktop.page_fresh()


@pytest.mark.parametrize("path", ["/api/notices?notify=1", "/api/notices/poll?notify=1&after=0"])
def test_no_get_touches_the_marker(client: TestClient, path: str) -> None:
    """What an <img> or a link on another site can send: a GET with no Origin, or one."""
    for headers in ({}, {"origin": "http://evil.example"}, {"referer": "http://evil.example/"}):
        assert client.get(path, headers=headers).status_code in (404, 405)
    assert not desktop.page_marker().exists()


def test_a_claim_succeeds_once(client: TestClient, orch: Orchestrator) -> None:
    notice = add(orch)

    first = client.post(f"/api/notices/{notice}/claim", json={}, headers=ORIGIN)
    second = client.post(f"/api/notices/{notice}/claim", json={}, headers=ORIGIN)

    assert first.status_code == 200 and first.json() == {"claimed": True}
    assert second.json() == {"claimed": False}
    assert client.post("/api/notices/999/claim", json={}, headers=ORIGIN).json() == {
        "claimed": False
    }
    assert poll(client, 0).json()["notices"] == []


def test_the_routes_keep_the_host_and_origin_checks(client: TestClient, orch: Orchestrator) -> None:
    notice = add(orch)
    claim = f"/api/notices/{notice}/claim"

    for path in ("/api/notices/poll", claim):
        refused = client.post(path, json={"after": 0}, headers={**ORIGIN, "host": "evil.example"})
        assert refused.status_code == 403 and refused.json() == {"error": "unexpected Host header"}
        cross = client.post(path, json={"after": 0}, headers={"origin": "http://evil.example"})
        assert cross.status_code == 403 and cross.json() == {
            "error": "cross-origin request refused"
        }
        assert client.post(path, json={"after": 0}).status_code == 403  # no Origin at all
        assert client.post(path, content=b'{"after": 0}', headers=ORIGIN).status_code == 415
    assert not desktop.page_marker().exists()  # no refused poll counts as a page

    assert [n["id"] for n in poll(client, 0).json()["notices"]] == [notice]  # still unclaimed


def test_a_page_gets_only_what_route_left_to_it_and_what_was_escalated_to_it_later(
    client: TestClient, orch: Orchestrator
) -> None:
    window = orch.store.add_notice("finished", "a-1", "left to the window", route="window")
    page = orch.store.add_notice("finished", "a-2", "left to a page", route="page")
    data = poll(client, 0).json()
    assert [n["id"] for n in data["notices"]] == [page]
    late = client.post(f"/api/notices/{window}/claim", json={}, headers=ORIGIN)
    assert late.json() == {"claimed": False}  # the window's: a page may not take it
    item = desktop.step("escalate", "page", "the window did not take it")
    assert orch.store.reroute_notice(window, "page", item, was=("window",))
    data = poll(client, data["cursor"]).json()  # behind the cursor, escalated: listed again
    assert [n["id"] for n in data["notices"]] == [window, page]
    assert client.post(f"/api/notices/{window}/claim", json={}, headers=ORIGIN).json() == {
        "claimed": True
    }
    (traced,) = [n for n in orch.notice_trace() if n["id"] == window]
    assert traced["summary"]["path"] == "page" and traced["summary"]["actor"].startswith("page:")
