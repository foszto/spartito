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
    caller returns after STORE_WAIT_SECONDS (0.25 s); the notice is spooled, recorded by the helper
    once the store is free, and left to the window: no osascript, nothing unrecorded."""
    desktop.app_present("allowed")
    holder = sqlite3.connect(orch.store.path, isolation_level=None, check_same_thread=False)
    holder.execute("BEGIN IMMEDIATE")
    threading.Timer(busy, lambda: (holder.execute("ROLLBACK"), holder.close())).start()
    begin = time.monotonic()
    desktop.show(orch.store, "a-1 finished", "a-1")
    assert time.monotonic() - begin < 0.5  # the caller goes on (STORE_WAIT_SECONDS: 0.25)
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
    assert traced["steps"][0]["detail"].startswith("spooled: the store was busy for 0.")
    late = traced["steps"][2]["detail"]
    assert late.startswith("recorded from the spool, ") and late.endswith(" s after it was spooled")
    assert float(late.split(", ")[1].split(" s ")[0]) >= busy - 0.5  # how long it was busy
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


def spooled_notice(orch: Orchestrator, monkeypatch, no_detached_helpers) -> list[str]:
    """A notice the busy store did not take: spooled; the args of its helper."""
    real = orch.store.add_notice

    def busy(*args: object, **options: object) -> int:
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(orch.store, "add_notice", busy)
    desktop.show(orch.store, "a-1 finished", "a-1")
    monkeypatch.setattr(orch.store, "add_notice", real)
    ((kind, spooled, opener),) = no_detached_helpers
    assert kind == "spool"
    return [kind, spooled, opener]


def test_the_spool_helper_survives_a_store_that_does_not_open(
    orch: Orchestrator, fake_osascript, macos, monkeypatch, no_detached_helpers
):
    """Opening the store is part of each try: a store busy beyond Store()'s own wait (it
    raises then) neither ends the helper nor loses the notice; it is recorded late."""
    desktop.app_present("allowed")
    args = spooled_notice(orch, monkeypatch, no_detached_helpers)
    opened: list[int] = []

    def busy_then_free() -> Store:
        opened.append(1)
        if len(opened) < 4:
            raise sqlite3.OperationalError("database is locked")
        return Store(orch.store.path)

    monkeypatch.setattr(desktop, "Store", busy_then_free)
    helper = threading.Thread(target=desktop.helper, args=(args,))
    helper.start()

    def window_takes() -> bool:
        listed = orch.notices()
        return bool(listed) and orch.claim_notice(listed[0]["id"], "window")

    until(window_takes, what="the notice recorded late")
    helper.join(30)
    assert len(opened) == 4 and not Path(args[1]).exists()
    (traced,) = orch.notice_trace(1)
    assert traced["steps"][2]["detail"].startswith("recorded from the spool, ")
    assert "recorded from the spool, " in desktop.notice_log().read_text()
    assert calls(fake_osascript) == []


def test_the_spool_helper_says_when_it_gives_up_and_why(
    orch: Orchestrator, macos, monkeypatch, no_detached_helpers
):
    args = spooled_notice(orch, monkeypatch, no_detached_helpers)
    monkeypatch.setattr(desktop, "SPOOL_GIVE_UP_SECONDS", 0.3)

    def busy() -> Store:
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(desktop, "Store", busy)
    assert desktop.helper(args) == 0
    (line,) = [x for x in desktop.notice_log().read_text().splitlines() if " fallback " in x]
    assert f"spool:{Path(args[1]).name} helper:{os.getpid()} fallback" in line
    assert "gave up after 0." in line and "(OperationalError: database is locked)" in line
    assert Path(args[1]).exists()  # left for the next poll
    monkeypatch.setattr(desktop, "Store", Store)
    desktop.page_present()
    assert [n["text"] for n in orch.page_notices(0)] == ["a-1 finished"]


def test_a_helper_ending_with_an_error_says_so(orch: Orchestrator, macos, monkeypatch):
    notice = orch.store.add_notice("finished", "a-1", "a-1 finished", route="window")

    def broken(*args: object) -> None:
        raise RuntimeError("boom")

    monkeypatch.setattr(desktop, "deliver", broken)
    assert desktop.helper(["deliver", str(notice), desktop.OPEN]) == 1
    assert f"#{notice} helper:{os.getpid()} error -: the helper ended: RuntimeError: boom" in (
        desktop.notice_log().read_text()
    )
    traced = trace_of(orch, notice)
    assert traced["steps"][-1]["step"] == "error"
    assert traced["summary"]["reason"] == "the helper ended: RuntimeError: boom"


# -- notices that waited: one rule for the window, the launcher's app and a page -------------


def shown_by(orch: Orchestrator, taker: str, tmp_path: Path, after: int) -> list[tuple]:
    """(id, text) of what the launcher's posting process posts (fake centre, allowed) or what
    a page's poll lists, in order."""
    if taker == "launcher":
        center = FakeCenter(status=2)
        notifier.post_pending(orch, center, tmp_path / "X.app", [])
        return [(int(post[0].rsplit("-", 1)[1]), post[2]) for post in center.posted]
    return [(n["id"], n["text"]) for n in orch.page_notices(after)]


@pytest.mark.parametrize("taker", ["launcher", "page"])
def test_a_burst_routed_at_once_is_shown_in_full(
    orch: Orchestrator, fake_osascript, macos, tmp_path, no_detached_helpers, taker
):
    """Notices routed to a taker just now are fresh, however many: none is cut."""
    if taker == "launcher":
        desktop.record_grant("local.x.app", fake_bundle(tmp_path))
    else:
        desktop.page_present()
    before = orch.notice_cursor()
    for n in range(5):
        desktop.show(orch.store, f"a-{n} finished", f"a-{n}")
    burst = [n["id"] for n in orch.notices()]
    assert len(burst) == 5 and {n["route"] for n in orch.notices()} == {
        "helper" if taker == "launcher" else "page"
    }
    shown = shown_by(orch, taker, tmp_path, before)
    assert shown == [(nid, f"a-{n} finished") for n, nid in enumerate(burst)]
    if taker == "launcher":
        for nid in burst:
            traced = trace_of(orch, nid)["summary"]
            assert traced["path"] == "launcher-helper"
            assert traced["reason"].startswith("the launcher's app may post")
    assert calls(fake_osascript) == []


@pytest.mark.parametrize("taker", ["launcher", "page"])
def test_a_backlog_is_cut_and_counted_and_fresh_ones_and_open_decisions_are_not(
    orch: Orchestrator, project: str, fake_osascript, macos, tmp_path, taker
):
    """Mixed: waiting notices (decisions open and gone, finished ones) and fresh ones. Gone
    decisions are dropped; open decisions and fresh notices are all shown; of the finished
    ones that waited the newest three, the newest of them saying how many more waited."""
    orch.update_settings({settings.DESKTOP_NOTIFICATIONS: "off"})
    lead_id = lead(orch, project)
    still = orch.request_decision(lead_id, "Which one?")
    open_one = orch.store.add_notice("decision", lead_id, "Which one?", still.id, route="waiting")
    gone = [
        orch.store.add_notice("decision", "a-0", "Which?", 900 + n, route="waiting")
        for n in range(2)
    ]
    finished = [
        orch.store.add_notice("finished", f"a-{n}", f"a-{n} finished", route="waiting")
        for n in range(5)
    ]
    after = orch.notice_cursor()
    route = "helper" if taker == "launcher" else "page"
    fresh = [
        orch.store.add_notice("finished", f"b-{n}", f"b-{n} finished", route=route)
        for n in range(4)
    ]
    shown = dict(shown_by(orch, taker, tmp_path, after))
    assert list(shown) == [open_one, *finished[2:], *fresh]
    assert shown[finished[-1]] == "(2 more waited) a-4 finished"  # the owner learns of them
    assert shown[finished[2]] == "a-2 finished" and shown[fresh[0]] == "b-0 finished"
    for n, notice in enumerate(gone):
        traced = trace_of(orch, notice)["summary"]
        assert (traced["path"], traced["reason"]) == ("dropped", f"decision #{900 + n} is gone")
    for notice in finished[:2]:
        traced = trace_of(orch, notice)["summary"]
        assert traced["path"] == "dropped"
        assert traced["reason"] == (
            "waited; only the newest 3 finished notices that waited are shown, this one is"
            f" counted in the text of #{finished[-1]}"
        )
    assert calls(fake_osascript) == []


def test_the_osascript_path_keeps_the_caller_quick_when_the_store_locks_after_recording(
    orch: Orchestrator, fake_osascript, macos, monkeypatch
):
    real = orch.store.add_notice
    holder = sqlite3.connect(orch.store.path, isolation_level=None, check_same_thread=False)

    def recorded_then_locked(*args: object, **options: object) -> int:
        notice = real(*args, **options)
        holder.execute("BEGIN IMMEDIATE")  # another process takes the lock right after
        return notice

    monkeypatch.setattr(orch.store, "add_notice", recorded_then_locked)
    begin = time.monotonic()
    try:
        desktop.show(orch.store, "a-1 finished", "a-1")
        took = time.monotonic() - begin
    finally:
        holder.execute("ROLLBACK")
        holder.close()
    assert took < desktop.STORE_WAIT_SECONDS + 0.4
    until(lambda: calls(fake_osascript), what="the notification")
    log = desktop.notice_log().read_text()
    assert " shown osascript: osascript started" in log and " lost osascript: " in log
    assert trace_of(orch, 1)["summary"]["path"] == "osascript"


# -- the one-line summary ----------------------------------------------------------------------


def test_a_claim_by_older_code_says_only_what_is_known(orch: Orchestrator):
    item = desktop.step("route", "window-system", "the window process takes it")
    notice = orch.store.add_notice("finished", "a-1", "a-1 finished", route="window", steps=[item])
    with sqlite3.connect(orch.store.path) as old:
        old.execute(OLD_CLAIM, ("2026-01-01T00:00:00+00:00", notice))
    old.close()
    assert trace_of(orch, notice)["summary"] == {
        "path": "not recorded",
        "actor": "unknown (older code)",
        "at": "2026-01-01T00:00:00+00:00",
        "reason": "claimed by a process that records no steps (older code)",
    }


def test_the_summary_gives_the_reason_of_the_final_outcome(orch: Orchestrator, tmp_path):
    notice = orch.store.add_notice("finished", "a-1", "a-1 finished", route="helper")
    assert orch.claim_notice(notice, "helper", "the posting process")
    assert orch.release_notice(notice, "post failed: boom", "helper")
    notifier.post_pending(orch, FakeCenter(status=2), tmp_path / "X.app", [])
    traced = trace_of(orch, notice)
    assert traced["summary"]["path"] == "launcher-helper"
    assert traced["summary"]["reason"] == "the posting process"
    assert "post failed: boom" in [item["detail"] for item in traced["steps"]]  # kept


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
    more = view["notices"][0]
    assert more["kind"] == "more" and more["target"] == ""
    assert more["text"] == "2 more notice(s): `spartito notices` lists them"
    assert [n["id"] for n in more["hidden"]] == waited[:2]
    assert window.inside.attention_calls == 1
    assert orch.notices() == []
    assert trace_of(orch, dropped)["summary"]["path"] == "dropped"
    assert trace_of(orch, dropped)["summary"]["reason"] == f"decision #{answered.id} is withdrawn"
    counted = trace_of(orch, waited[0])["steps"][-1]
    assert (counted["step"], counted["path"], counted["detail"]) == (
        "counted",
        "window-count",
        "waited for a window; not shown, counted in its banner (the newest 3 finished notices"
        " that waited are shown one by one)",
    )
    assert trace_of(orch, waited[0])["summary"]["path"] == "window-count"
    # read by code from before the `counted` step (its desktop.summary takes the last of these
    # as the outcome; none: "not recorded … (older code)"): claimed on the same path
    steps = trace_of(orch, waited[0])["steps"]
    older = next(s for s in reversed(steps) if s["step"] in ("shown", "drop", "claim"))
    assert (older["step"], older["path"]) == ("claim", "window-count")
    counted = trace_of(orch, waited[1])["steps"]  # in the backlog, but no banner was free
    assert [s["step"] for s in counted][-2:] == ["claim", "counted"]
    assert trace_of(orch, waited[1])["summary"]["path"] == "window-count"
    for shown in (*waited[2:], open_one):
        assert trace_of(orch, shown)["summary"]["path"] == "window-banner"
    assert window.lines[-1] == "notifications: 5 notice(s) waited for a window; shown in it"
    assert calls(fake_osascript) == []


def placed(orch: Orchestrator, window) -> tuple[list[int], list[int], dict]:
    """The ids in the window's banners and those counted in its 'more' banner (oldest first),
    and that banner (or {}), each checked against its trace."""
    notices = window.inside.last["notices"]
    more = next((n for n in notices if n["kind"] == "more"), {})
    banners = [n["id"] for n in notices if n["kind"] != "more"]
    hidden = [n["id"] for n in more.get("hidden", [])]
    for notice in banners:
        assert trace_of(orch, notice)["summary"]["path"] == "window-banner"
    for notice in hidden:
        steps = trace_of(orch, notice)["steps"]
        assert trace_of(orch, notice)["summary"]["path"] == "window-count"
        assert steps[-1]["step"] == "counted"
    return banners, hidden, more


@pytest.mark.parametrize(("deciding", "finishing", "cut"), [(2, 3, 0), (4, 3, 0), (1, 5, 2)])
def test_what_waited_for_the_window_is_in_a_banner_or_counted_and_decisions_keep_theirs(
    orch: Orchestrator, project: str, window, fake_osascript, macos, deciding, finishing, cut
):
    """More notices waited than the window has banners for: none drops out unseen and
    uncounted; open decisions keep the banners (the newest of them), finished ones the rest."""
    orch.update_settings({settings.DESKTOP_NOTIFICATIONS: "off"})
    lead_id = lead(orch, project)
    decided = []
    for n in range(deciding):
        number = orch.request_decision(lead_id, f"Which {n}?").id
        decided.append(orch.store.add_notice("decision", lead_id, "?", number, route="waiting"))
    finished = [
        orch.store.add_notice("finished", f"a-{n}", f"a-{n} finished", route="waiting")
        for n in range(finishing)
    ]
    window.cursor = orch.notice_cursor()
    window.tick()
    banners, hidden, more = placed(orch, window)
    assert orch.notices() == []
    assert sorted(banners + hidden) == sorted(decided + finished)  # each once, none lost
    in_banners = decided[-notifier.IN_WINDOW :]
    room = notifier.IN_WINDOW - len(in_banners)
    in_banners += finished[len(finished) - room :] if room else []
    assert sorted(banners) == sorted(in_banners)
    assert hidden[:cut] == finished[:cut]  # cut from the backlog, counted first
    pushed = [d for d in decided if d not in banners]
    assert more["text"].startswith(f"{len(hidden)} more notice(s)")
    if pushed:
        assert f", {len(pushed)} of them decision(s)" in more["text"]
        number = orch.store.notice(pushed[-1])["decision_id"]
        assert more["target"] == f"decision:{number}"  # the Decisions window lists them all
    else:
        assert more["target"] == ""
    assert window.lines[-1] == (
        f"notifications: {deciding + finishing} notice(s) waited for a window; shown in it"
    )
    assert calls(fake_osascript) == []


def one_after_another(orch: Orchestrator, project: str, window, kinds_: str) -> list[int]:
    """Notices left to the window one by one (d: a decision, open; f: finished), a poll after
    each; their ids. The decision of a notice: orch.store.notice(id)["decision_id"]."""
    orch.update_settings({settings.DESKTOP_NOTIFICATIONS: "off"})  # only these notices
    lead_id = lead(orch, project)
    window.tick()
    made = []
    for n, kind in enumerate(kinds_):
        if kind == "d":
            number = orch.request_decision(lead_id, f"Which {n}?").id
            made.append(orch.store.add_notice("decision", lead_id, "?", number, route="banner"))
        else:
            made.append(orch.store.add_notice("finished", f"a-{n}", "done", route="banner"))
        window.tick()
    return made


def decision_of(orch: Orchestrator, notice: int) -> int:
    return orch.store.notice(notice)["decision_id"]


def test_notices_shown_one_after_another_beyond_the_banners_are_counted(
    orch: Orchestrator, project: str, window, fake_osascript, macos
):
    """The window may not post: each new notice is shown inside; one pushed out of its banner
    is counted (and traced so), a decision is never pushed out by a finished notice, and a
    finished one finding only decisions in the banners is counted at once."""
    d0, f1, f2, f3, d4, d5, d6, f7 = one_after_another(orch, project, window, "dfffdddf")
    banners, hidden, more = placed(orch, window)
    assert banners == [d4, d5, d6]
    assert hidden == [d0, f1, f2, f3, f7]
    assert more["target"] == f"decision:{decision_of(orch, d0)}"
    assert ", 1 of them decision(s)" in more["text"]
    pushed = trace_of(orch, f1)["steps"]
    assert [s["step"] for s in pushed][-2:] == ["shown", "counted"]  # it was shown, then out
    assert pushed[-1]["detail"].startswith("pushed out of its banner by a newer notice")
    never = trace_of(orch, f7)["steps"]
    assert "shown" not in [s["step"] for s in never] and never[-1]["step"] == "counted"
    assert "all banners hold open decisions" in never[-1]["detail"]
    assert trace_of(orch, f7)["summary"]["reason"] == never[-1]["detail"]
    assert window.inside.holds[-1:] in ([], [0])  # the page has them all: no badge left
    assert window.unseen == {}
    assert calls(fake_osascript) == []


def test_a_decision_no_longer_open_or_a_closed_banner_frees_its_place(
    orch: Orchestrator, project: str, window, fake_osascript, macos
):
    """Decisions in the banners are answered (here: withdrawn) or banners closed in the page:
    on the next poll they leave the window, and what waits in the count takes the freed
    banners by the same rule; the count names only open decisions."""
    d0, d1, d2, d3, f4, f5, f6 = one_after_another(orch, project, window, "ddddfff")
    banners, hidden, more = placed(orch, window)
    assert (banners, hidden) == ([d1, d2, d3], [d0, f4, f5, f6])
    lead_id = orch.store.notice(d0)["agent_id"]
    for notice in (d0, d1, d2, d3):
        orch.withdraw_decision(decision_of(orch, notice), lead_id)
    window.tick()
    banners, hidden, more = placed(orch, window)
    assert (banners, hidden) == ([f4, f5, f6], [])  # finished ones shown, no stale decision
    assert more == {}
    for notice in (d0, d1):
        left = trace_of(orch, notice)["steps"][-1]
        assert left["step"] == "leave"
        assert left["detail"] == (
            f"decision #{decision_of(orch, notice)} is no longer open: it leaves the window"
        )
    assert trace_of(orch, d0)["summary"]["path"] == "window-count"  # it never had a banner
    assert trace_of(orch, d1)["summary"]["path"] == "window-banner"
    came = trace_of(orch, f4)["steps"][-1]
    assert (came["step"], came["detail"]) == ("shown", "a banner came free")
    # more than fit again; then the page closes a banner, and the count with its ×
    f7, f8, d9 = one_after_another(orch, project, window, "ffd")
    banners, hidden, more = placed(orch, window)
    assert (banners, hidden) == ([f7, f8, d9], [f4, f5, f6])
    window.inside.closes.add(f8)
    window.tick()
    banners, hidden, more = placed(orch, window)
    assert (banners, hidden) == ([f6, f7, d9], [f4, f5])
    assert trace_of(orch, f8)["steps"][-1]["detail"] == (
        "closed in the window's page: it leaves the window"
    )
    window.inside.closes |= {f4, f5}  # the count banner's ×: all it counted
    window.tick()
    banners, hidden, more = placed(orch, window)
    assert (banners, hidden, more) == ([f6, f7, d9], [], {})
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
    assert posted == sorted(f"spartito-notice-{n}" for n in notices)  # each once
    assert orch.notices() == []


def test_racing_takers_announce_every_cut_notice_once(orch: Orchestrator, tmp_path):
    """The launcher's posting process and a page take six finished notices that waited at
    once: each is shown once, and the shown texts announce the three cut, however the claims
    of the cut ones split between them."""
    waited = [
        orch.store.add_notice("finished", f"a-{n}", f"a-{n} finished", route="waiting")
        for n in range(6)
    ]
    center = FakeCenter(status=2)
    page: list[tuple[int, str]] = []
    start = threading.Barrier(2)

    def launcher() -> None:
        start.wait()
        notifier.post_pending(orch, center, tmp_path / "X.app", [])

    def a_page() -> None:
        start.wait()
        for notice in orch.page_notices(0):
            if orch.claim_notice(notice["id"], "page"):
                page.append((notice["id"], notice["text"]))

    racers = [threading.Thread(target=launcher), threading.Thread(target=a_page)]
    for racer in racers:
        racer.start()
    for racer in racers:
        racer.join(30)
    shown = page + [(int(p[0].rsplit("-", 1)[1]), p[2]) for p in center.posted]
    assert sorted(n for n, _ in shown) == waited[3:]  # each once
    announced = [text.split(" more waited)")[0][1:] for _, text in shown if "more waited" in text]
    assert announced == ["3"]
    assert orch.notices() == []


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
