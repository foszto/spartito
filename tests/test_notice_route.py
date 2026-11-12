"""How a notice is shown, end to end, for each cause found in the field (docs/desktop.md: the
paths and the trace): a busy store, the window that closes or does not poll, the posting
process that may not post, two deciders at once; the trace, `spartito notices`, and a process
of older code on the same database.

Nothing real is shown or started: osascript is the fake of conftest.py, the notification
centre a fake, `desktop.spawn` notes its helper (conftest.py), which tests run themselves.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from pathlib import Path

import pytest
from test_decisions import calls
from test_desktop_paths import fake_bundle
from test_notifications import lead, until
from test_notifier import FakeCenter, FakeInside
from typer.testing import CliRunner

from orchestrator import cli, desktop, settings
from orchestrator.gui import notifier
from orchestrator.service import Orchestrator
from orchestrator.store import Store


@pytest.fixture
def macos(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(desktop, "PLATFORM", "darwin")


def trace_of(orch: Orchestrator, notice: int) -> dict:
    return next(n for n in orch.notice_trace(100) if n["id"] == notice)


def kinds(orch: Orchestrator, notice: int) -> list[tuple[str, str | None]]:
    return [(item["step"], item["path"]) for item in trace_of(orch, notice)["steps"]]


# -- a busy store ----------------------------------------------------------------------------


@pytest.mark.parametrize("busy", [3.0, 10.0])
def test_a_store_busy_for_seconds_records_the_notice_late_and_the_window_takes_it(
    orch: Orchestrator, fake_osascript, macos, no_detached_helpers, busy
):
    """The field case: the store held beyond STORE_WAIT_SECONDS while the window runs. The
    caller returns after STORE_WAIT_SECONDS; the notice is spooled, recorded by the helper
    once the store is free, and left to the window: no osascript, nothing unrecorded."""
    desktop.app_present("allowed")
    holder = sqlite3.connect(orch.store.path, isolation_level=None, check_same_thread=False)
    holder.execute("BEGIN IMMEDIATE")
    threading.Timer(busy, lambda: (holder.execute("ROLLBACK"), holder.close())).start()
    begin = time.monotonic()
    desktop.show(orch.store, "a-1 finished", "a-1")
    assert time.monotonic() - begin < desktop.STORE_WAIT_SECONDS + 1  # the caller goes on
    ((kind, spooled, opener),) = no_detached_helpers
    assert kind == "spool" and Path(spooled).exists()
    helper = threading.Thread(target=desktop.helper, args=([kind, spooled, opener],))
    helper.start()  # the detached helper, here: it retries beyond STORE_WAIT_SECONDS

    def window_takes() -> bool:
        listed = orch.notices()
        return bool(listed) and orch.claim_notice(listed[0]["id"], "window")

    until(window_takes, timeout=busy + 20, what="the recorded notice")
    helper.join(30)
    assert time.monotonic() - begin >= busy  # recorded only once the store was free
    assert calls(fake_osascript) == []
    (traced,) = orch.notice_trace(1)
    steps = [(item["step"], item["path"]) for item in traced["steps"]]
    assert steps[:4] == [
        ("record", None),
        ("route", "window-system"),
        ("record", None),
        ("route", "window-system"),
    ]
    assert traced["steps"][0]["detail"].startswith("spooled: the store was busy for 2.")
    assert traced["steps"][2]["detail"] == "recorded from the spool"
    assert traced["summary"]["path"] == "window-system"
    assert list(desktop.spool_dir().iterdir()) == []


def test_a_poll_records_a_spooled_notice_when_the_helper_did_not(
    orch: Orchestrator, fake_osascript, macos, monkeypatch
):
    def busy(*args: object, **options: object) -> int:
        raise sqlite3.OperationalError("database is locked")

    real = orch.store.add_notice
    monkeypatch.setattr(orch.store, "add_notice", busy)
    desktop.show(orch.store, "Decision needed", "a-1", 4)
    monkeypatch.setattr(orch.store, "add_notice", real)
    desktop.page_present()
    assert orch.page_notices(0)[0]["text"] == "Decision needed"  # the page's poll took it in
    assert list(desktop.spool_dir().glob("*.json")) == []


# -- the window ------------------------------------------------------------------------------


@pytest.fixture
def window(orch: Orchestrator, tmp_path: Path):
    lines: list[str] = []
    made = notifier.WindowNotifier(
        orch, FakeCenter(status=0), "local.x.app", tmp_path / "X.app", lines.append, FakeInside()
    )
    made.lines = lines  # type: ignore[attr-defined]
    yield made
    made.close()


def test_a_window_closing_with_a_fresh_page_leaves_its_notices_to_the_page(
    orch: Orchestrator, window, fake_osascript, macos, no_detached_helpers
):
    """desktop.without_window ignored a fresh page and used osascript."""
    window.tick()
    desktop.show(orch.store, "a-1 finished", "a-1")  # left to the window
    (notice,) = orch.notices()
    desktop.page_present()
    window.close()
    assert [(n["id"], n["route"]) for n in orch.notices()] == [(notice["id"], "page")]
    assert kinds(orch, notice["id"])[-1] == ("escalate", "page")
    time.sleep(0.3)
    assert calls(fake_osascript) == []


def test_the_window_shows_what_waited_for_it_once_without_a_flood(
    orch: Orchestrator, project: str, window, fake_osascript, macos
):
    """Notices that waited (no window, the launcher's app failed): shown inside when the
    window opens, the newest few as banners, the rest as a count; none for an answered
    decision."""
    orch.update_settings({settings.DESKTOP_NOTIFICATIONS: "off"})
    lead_id = lead(orch, project)
    answered = orch.request_decision(lead_id, "Which?")
    still = orch.request_decision(lead_id, "Which one?")
    orch.withdraw_decision(answered.id, lead_id)
    dropped = orch.store.add_notice("decision", lead_id, "Which?", answered.id, route="waiting")
    waited = [
        orch.store.add_notice("finished", f"a-{n}", f"a-{n} finished", route="waiting")
        for n in range(4)
    ]
    open_one = orch.store.add_notice("decision", lead_id, "Which one?", still.id, route="waiting")
    window.cursor = orch.notice_cursor()  # these are older than the window
    window.tick()
    window.tick()
    view = window.inside.last
    assert [n["id"] for n in view["notices"]] == [-window.cursor - 1, *waited[-2:], open_one]
    assert view["notices"][0]["text"].startswith("2 earlier notice(s) waited")
    assert window.inside.attention_calls == 1
    assert orch.notices() == []
    assert trace_of(orch, dropped)["summary"]["path"] == "dropped"
    assert trace_of(orch, dropped)["summary"]["reason"] == f"decision #{answered.id} is withdrawn"
    counted = trace_of(orch, waited[0])["steps"][-1]
    assert (counted["step"], counted["path"], counted["detail"]) == (
        "shown",
        "window-banner",
        "counted in the window",
    )
    assert window.lines[-1] == "notifications: 5 notice(s) waited for a window; shown in it"
    assert calls(fake_osascript) == []


def test_a_window_marker_late_but_its_process_running_gets_the_notice(
    orch: Orchestrator, window, fake_osascript, macos, monkeypatch, no_detached_helpers
):
    """App Nap or a stalled poll: the marker is late, the process runs: still the window's,
    and the window takes it on its next poll."""
    window.tick()
    then = time.time() - 60
    os.utime(desktop.app_marker(), (then, then))
    desktop.show(orch.store, "a-1 finished", "a-1")
    (notice,) = orch.notices()
    assert notice["route"] == "window"
    window.tick()
    assert orch.notices() == [] and calls(fake_osascript) == []
    assert trace_of(orch, notice["id"])["summary"]["path"] == "window-banner"


# -- deciders and takers at once -------------------------------------------------------------


def test_racing_posting_processes_show_each_notice_once(orch: Orchestrator, tmp_path):
    notices = [
        orch.store.add_notice("finished", f"a-{n}", f"a-{n} finished", route="helper")
        for n in range(6)
    ]
    centers = [FakeCenter(status=2), FakeCenter(status=2)]
    racers = [
        threading.Thread(target=notifier.post_pending, args=(orch, c, tmp_path / "X.app", []))
        for c in centers
    ]
    for racer in racers:
        racer.start()
    for racer in racers:
        racer.join(30)
    posted = sorted(post[0] for center in centers for post in center.posted)
    assert posted == sorted(f"spartito-notice-{n}" for n in notices)


def test_racing_escalations_move_a_notice_once(orch: Orchestrator, macos, monkeypatch):
    monkeypatch.setattr(desktop, "ESCALATE_SECONDS", 0.2)
    notice = orch.store.add_notice("finished", "a-1", "a-1 finished", route="page")
    racers = [threading.Thread(target=desktop.deliver, args=(orch.store, notice)) for _ in "ab"]
    for racer in racers:
        racer.start()
    for racer in racers:
        racer.join(30)
    assert [step for step, _ in kinds(orch, notice)].count("escalate") == 1


# -- the trace and `spartito notices` --------------------------------------------------------


def test_every_step_is_logged_and_the_log_is_capped(orch: Orchestrator, macos, monkeypatch):
    desktop.show(orch.store, "a-1 finished", "a-1")  # nothing present: osascript
    lines = desktop.notice_log().read_text().splitlines()
    assert [line.split()[3:5] for line in lines] == [
        ["record", "-:"],
        ["route", "osascript:"],
        ["shown", "osascript:"],
    ]
    assert all(line.split()[1] == "#1" and ":" in line.split()[2] for line in lines)
    monkeypatch.setattr(desktop, "NOTICE_LOG_LIMIT", 10)
    desktop.show(orch.store, "a-2 finished", "a-2")
    assert desktop.notice_log().with_name("notices.log.1").exists()
    assert len(desktop.notice_log().read_text().splitlines()) < 3


def test_the_notices_command(orch: Orchestrator, macos, tmp_path, no_detached_helpers):
    desktop.show(orch.store, "a-1 finished", "a-1")  # osascript: nothing else here
    desktop.record_grant("local.x.app", fake_bundle(tmp_path))
    desktop.show(orch.store, "Decision needed", "a-2", 3)  # left to the launcher's app
    runner = CliRunner()
    out = runner.invoke(cli.app, ["notices"]).output.splitlines()
    assert out[0].startswith("#2  ") and " decision  launcher-helper (not taken yet)  " in out[0]
    assert "— the launcher's app may post; no window; no page; launcher ready" in out[0]
    assert out[1].startswith("#1  ") and " finished  osascript  cli:" in out[1]
    assert "no launcher (no grant file (not installed, or not allowed))" in out[1]
    assert runner.invoke(cli.app, ["notices", "-n", "1"]).output.count("\n") == 1
    verbose = runner.invoke(cli.app, ["notices", "-v", "-n", "1"]).output
    assert "  Decision needed\n" in verbose and " route launcher-helper: " in verbose
    data = json.loads(runner.invoke(cli.app, ["notices", "--json"]).output)
    assert [n["id"] for n in data] == [2, 1]
    assert [s["step"] for s in data[1]["steps"]] == ["record", "route", "shown"]
    assert data[1]["summary"]["path"] == "osascript"
    assert runner.invoke(cli.app, ["notices", "-n", "0"]).exit_code != 0


def test_no_notices(orch: Orchestrator):
    assert CliRunner().invoke(cli.app, ["notices"]).output == "no notices\n"


# -- a process of older code on the same database --------------------------------------------

OLD_INSERT = (
    "INSERT INTO notices (created_at, kind, agent_id, decision_id, text) VALUES (?, ?, ?, ?, ?)"
)
OLD_LIST = (
    "SELECT id, kind, agent_id, decision_id, text, created_at FROM notices"
    " WHERE id > ? AND claimed_at IS NULL ORDER BY id"
)
OLD_CLAIM = "UPDATE notices SET claimed_at = ? WHERE id = ? AND claimed_at IS NULL"
OLD_RELEASE = "UPDATE notices SET claimed_at = NULL WHERE id = ?"
FIRST_NOTICES = """
CREATE TABLE notices (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    kind TEXT NOT NULL,
    agent_id TEXT NOT NULL,
    decision_id INTEGER,
    text TEXT NOT NULL,
    claimed_at TEXT
);
"""


def test_older_code_keeps_working_on_the_same_database(tmp_path: Path):
    path = tmp_path / "orch.db"
    with sqlite3.connect(path) as conn:
        conn.executescript(FIRST_NOTICES)  # the notices table as older code made it
        conn.execute(OLD_INSERT, (store_now(), "finished", "a-0", None, "old"))
    conn.close()
    store = Store(path)  # the new columns and table, added on open
    with store.connection() as conn:
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(notices)")}
    assert {"route", "claimed_by"} <= columns
    new = store.add_notice("finished", "a-1", "new", route="window")
    old = sqlite3.connect(path)  # older code: its own statements, unchanged
    try:
        with old:
            older = old.execute(OLD_INSERT, (store_now(), "decision", "a-2", 5, "older")).lastrowid
        assert [row[0] for row in old.execute(OLD_LIST, (0,))] == [1, new, older]
        with old:
            assert old.execute(OLD_CLAIM, (store_now(), new)).rowcount == 1
            assert old.execute(OLD_CLAIM, (store_now(), new)).rowcount == 0
            old.execute(OLD_RELEASE, (new,))
    finally:
        old.close()
    assert [n["route"] for n in store.notices()] == [None, "window", None]
    assert desktop.claim(store, older, "page")  # no route: anyone may take it
    assert not desktop.claim(store, new, "page")  # the window's
    traced = {n["id"]: n for n in store.notice_trace()}
    assert traced[1]["steps"] == []
    assert desktop.summary(traced[1])["reason"] == "recorded by older code: no trace"
    Store(path)  # opening it again changes nothing


def store_now() -> str:
    from orchestrator.store import now

    return now()
