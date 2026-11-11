"""Notices: desktop notifications recorded for a dashboard page (store table `notices`,
desktop.show, core/notices.py).

The fake osascript of conftest.py stands in for the real one; the `macos` fixture makes this
process count as macOS.
"""

from __future__ import annotations

import os
import sqlite3
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from test_decisions import bodies, calls, turn
from test_notifications import lead, until
from test_store import MASTER_ERA_SCHEMA

from orchestrator import config, desktop, settings
from orchestrator.service import Orchestrator
from orchestrator.store import Store


@pytest.fixture
def macos(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(desktop, "PLATFORM", "darwin")


def rows(store: Store) -> list[dict]:
    with store.connection() as conn:
        return [dict(row) for row in conn.execute("SELECT * FROM notices ORDER BY id")]


def fresh_page() -> None:
    desktop.page_present()


def stale_page() -> None:
    desktop.page_present()
    then = time.time() - desktop.PAGE_FRESH_SECONDS - 5
    os.utime(desktop.page_marker(), (then, then))


# -- the store ---------------------------------------------------------------


def test_notices_are_listed_after_an_id_until_claimed(tmp_path: Path) -> None:
    store = Store(tmp_path / "n.db")
    assert store.notice_cursor() == 0 and store.notices() == []

    first = store.add_notice("finished", "a-1", "a-1 finished")
    second = store.add_notice("decision", "lead-1", "Decision needed (p): Go?", 7)

    assert store.notice_cursor() == second > first
    listed = store.notices()
    assert [n["id"] for n in listed] == [first, second]
    assert listed[1] == {
        "id": second,
        "kind": "decision",
        "agent_id": "lead-1",
        "decision_id": 7,
        "text": "Decision needed (p): Go?",
        "created_at": listed[1]["created_at"],
    }
    assert listed[0]["decision_id"] is None
    assert [n["id"] for n in store.notices(after=first)] == [second]
    assert store.notices(after=second) == []

    assert store.claim_notice(first) is True
    assert store.claim_notice(first) is False  # once only
    assert [n["id"] for n in store.notices()] == [second]
    assert store.notice_cursor() == second  # claiming does not move the cursor
    assert store.claim_notice(999) is False


def test_of_concurrent_claimers_exactly_one_wins(tmp_path: Path) -> None:
    path = tmp_path / "n.db"
    notice = Store(path).add_notice("finished", "a-1", "done")
    results: list[bool] = []
    start = threading.Barrier(8)

    def claim() -> None:
        store = Store(path)
        start.wait()
        results.append(store.claim_notice(notice))

    threads = [threading.Thread(target=claim) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert sorted(results) == [False] * 7 + [True]


def test_adding_a_notice_prunes_those_older_than_a_day(tmp_path: Path) -> None:
    store = Store(tmp_path / "n.db")
    old = store.add_notice("finished", "a-1", "old")
    recent = store.add_notice("finished", "a-1", "recent")
    with store.connection() as conn:
        long_ago = (datetime.now(UTC) - timedelta(days=1, minutes=1)).isoformat(timespec="seconds")
        conn.execute("UPDATE notices SET created_at = ? WHERE id = ?", (long_ago, old))
        nearly = (datetime.now(UTC) - timedelta(hours=23)).isoformat(timespec="seconds")
        conn.execute("UPDATE notices SET created_at = ? WHERE id = ?", (nearly, recent))

    new = store.add_notice("finished", "a-1", "new")

    assert [row["id"] for row in rows(store)] == [recent, new]


def test_a_master_era_database_gets_the_notices_table(tmp_path: Path) -> None:
    path = tmp_path / "master.db"
    with sqlite3.connect(path) as conn:
        conn.execute("PRAGMA journal_mode=wal")
        conn.executescript(MASTER_ERA_SCHEMA.read_text())
    conn.close()

    store = Store(path)
    notice = store.add_notice("finished", "a-1", "done")

    assert store.notices()[0]["id"] == notice
    Store(path)  # opening it again changes nothing
    assert [n["id"] for n in store.notices()] == [notice]


# -- desktop.show --------------------------------------------------------------


def test_switched_off_nothing_is_recorded_or_shown(
    orch: Orchestrator, fake_osascript: Path, macos: None
) -> None:
    orch.update_settings({settings.DESKTOP_NOTIFICATIONS: "off"})
    desktop.show(orch.store, "a-1 finished", "a-1")
    fresh_page()
    desktop.show(orch.store, "a-1 finished", "a-1")
    time.sleep(0.5)
    assert rows(orch.store) == [] and calls(fake_osascript) == []


def test_without_a_page_osascript_shows_it_and_claims_the_notice(
    orch: Orchestrator, fake_osascript: Path, macos: None
) -> None:
    desktop.show(orch.store, "a-1 finished", "a-1")

    until(lambda: calls(fake_osascript), what="the notification")
    assert bodies(fake_osascript) == ["a-1 finished"]
    (row,) = rows(orch.store)
    assert row["claimed_at"] is not None and row["kind"] == "finished"
    assert orch.notices() == []


def test_with_a_fresh_page_the_notice_is_left_to_it(
    orch: Orchestrator, fake_osascript: Path, macos: None
) -> None:
    fresh_page()
    desktop.show(orch.store, "a-1 finished", "a-1")
    time.sleep(0.5)

    assert calls(fake_osascript) == []
    (notice,) = orch.notices()
    assert notice["kind"] == "finished" and notice["agent_id"] == "a-1"
    assert notice["decision_id"] is None and notice["text"] == "a-1 finished"


def test_a_stale_page_counts_as_none(orch: Orchestrator, fake_osascript: Path, macos: None) -> None:
    stale_page()
    desktop.show(orch.store, "a-1 finished", "a-1")

    until(lambda: calls(fake_osascript), what="the notification")
    assert orch.notices() == []


def test_a_notice_a_page_claimed_first_is_not_shown_again(
    orch: Orchestrator, fake_osascript: Path, macos: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(orch.store, "claim_notice", lambda notice_id: False)
    desktop.show(orch.store, "a-1 finished", "a-1")
    time.sleep(0.5)
    assert calls(fake_osascript) == [] and len(rows(orch.store)) == 1


def test_elsewhere_than_macos_the_notice_stays_unclaimed(
    orch: Orchestrator, fake_osascript: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(desktop, "PLATFORM", "linux")
    desktop.show(orch.store, "a-1 finished", "a-1")
    time.sleep(0.5)
    assert calls(fake_osascript) == [] and len(orch.notices()) == 1


def test_a_missing_or_broken_osascript_still_records_the_notice(
    orch: Orchestrator, macos: None, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(desktop, "command", lambda title, body: None)  # not found
    desktop.show(orch.store, "missing", "a-1")
    broken = tmp_path / "osascript"
    broken.write_text("not a program")
    broken.chmod(0o644)
    monkeypatch.setattr(desktop, "command", lambda title, body: [str(broken), body])
    desktop.show(orch.store, "broken", "a-1")  # no exception

    assert [row["text"] for row in rows(orch.store)] == ["missing", "broken"]
    assert [n["text"] for n in orch.notices()] == ["missing"]  # the broken one was claimed


def test_a_store_that_fails_still_lets_osascript_show_it(
    orch: Orchestrator, fake_osascript: Path, macos: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail(*args: object) -> int:
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(orch.store, "add_notice", fail)
    fresh_page()
    desktop.show(orch.store, "a-1 finished", "a-1")
    until(lambda: calls(fake_osascript), what="the notification")


def test_a_decision_notice_carries_the_decision_and_the_requesting_agent(
    orch: Orchestrator, project: str, fake_osascript: Path, macos: None
) -> None:
    lead_id = lead(orch, project)
    fresh_page()
    decision = orch.request_decision(lead_id, "Which database?")

    (notice,) = orch.notices()
    assert notice["kind"] == "decision" and notice["agent_id"] == lead_id
    assert notice["decision_id"] == decision.id
    assert notice["text"] == f"Decision needed ({project}): Which database?"


def test_a_finished_notice_carries_the_agent(
    orch: Orchestrator, project: str, fake_osascript: Path, macos: None
) -> None:
    lead_id = lead(orch, project)
    fresh_page()
    desktop.turn_ended(orch.store, turn(orch, lead_id), lead_id)

    (notice,) = orch.notices()
    assert notice["kind"] == "finished" and notice["agent_id"] == lead_id
    assert notice["decision_id"] is None and notice["text"] == f"{lead_id} finished"


# -- the facade ----------------------------------------------------------------


def test_the_facade_methods(orch: Orchestrator, macos: None) -> None:
    assert not desktop.page_fresh()
    orch.page_present()
    assert desktop.page_fresh()
    assert desktop.page_marker() == config.home() / "run" / "page-notifier"
    assert orch.notice_cursor() == 0

    desktop.show(orch.store, "one", "a-1")
    desktop.show(orch.store, "two", "a-2", 3)
    cursor = orch.notice_cursor()
    first, second = orch.notices()
    assert orch.notices(after=first["id"]) == [second] and orch.notices(after=cursor) == []
    assert orch.claim_notice(first["id"]) is True
    assert orch.claim_notice(first["id"]) is False
    assert orch.notices() == [second]
