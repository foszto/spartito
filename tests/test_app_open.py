"""Requests to the open window of `spartito app` (app_open.py) and /api/state carrying them."""

from __future__ import annotations

import json
import os
import time

import pytest
from starlette.testclient import TestClient

from orchestrator import app_open, config
from orchestrator.service import Orchestrator
from orchestrator.web.app import app


def request_file():
    return config.runtime_dir() / app_open.REQUEST


def age(seconds: float) -> None:
    then = time.time() - seconds
    os.utime(request_file(), (then, then))


def test_requests_count_up_and_are_fresh_for_a_while():
    first = Orchestrator.request_app_open("decision:5")
    assert json.loads(request_file().read_text()) == {"id": first, "target": "decision:5"}
    assert app_open.latest() == {"id": first, "target": "decision:5"}
    second = Orchestrator.request_app_open()
    assert second > first
    assert app_open.last_id() == second
    assert app_open.latest() == {"id": second, "target": ""}
    age(app_open.FRESH_SECONDS - 1)
    assert app_open.latest() is not None
    age(app_open.FRESH_SECONDS + 1)
    assert app_open.latest() is None
    assert app_open.last_id() == second  # still the baseline of a new window


def test_ids_rise_even_when_the_clock_is_behind():
    request_file().write_text(json.dumps({"id": 10**15, "target": ""}))
    assert app_open.request() == 10**15 + 1


def test_a_request_from_the_future_does_not_count():
    app_open.request("worker-1a2b")
    age(-60)
    assert app_open.latest() is None


@pytest.mark.parametrize(
    "target",
    ["decision:", "decision:x", "Worker", "-x", "a b", "a/../b", "x" * 65, "lead:proj", "<i>"],
)
def test_bad_targets_are_refused(target: str):
    with pytest.raises(ValueError, match="not an agent id"):
        app_open.request(target)
    assert not request_file().exists()


@pytest.mark.parametrize(
    "content",
    [
        "",
        "[]",
        '{"id": "1", "target": ""}',
        '{"id": true, "target": ""}',
        '{"id": -1, "target": ""}',
        '{"id": 3, "target": "javascript:alert(1)"}',
        '{"id": 3, "target": 5}',
    ],
)
def test_a_broken_request_file_is_no_request(content: str):
    request_file().write_text(content)
    assert app_open.latest() is None


def test_answers_belong_to_their_request():
    number = app_open.request()
    assert app_open.answered(number) is None
    app_open.answer(number - 1, True)
    assert app_open.answered(number) is None
    app_open.answer(number, False, "why")
    assert app_open.answered(number) == {"raised": False, "detail": "why"}


def test_state_carries_the_fresh_request():
    client = TestClient(app, base_url="http://localhost")
    assert client.get("/api/state").json()["app_open"] is None
    number = Orchestrator.request_app_open("decision:12")
    assert client.get("/api/state").json()["app_open"] == {"id": number, "target": "decision:12"}
    age(app_open.FRESH_SECONDS + 1)
    assert client.get("/api/state").json()["app_open"] is None
