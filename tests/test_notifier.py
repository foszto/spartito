"""Notifications Spartito posts itself on macOS (gui/notifier.py), with a fake notification
centre: permission, claim-once polling, the posting process, and a click back to a deep link.
No real notification centre is reached (conftest.py makes notifier.MacCenter fail)."""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from test_decisions import calls
from test_notifications import until

from orchestrator import app_open, desktop
from orchestrator.gui import notifier
from orchestrator.service import Orchestrator


class FakeCenter:
    """The notification centre: answers authorize later (`grant`), status with `status`,
    and notes each post, answering it with `post_error`."""

    def __init__(self, status: int = 2, post_error: str | None = None) -> None:
        self.asked: list = []
        self.status_value = status
        self.post_error = post_error
        self.posted: list[tuple[str, str, str, dict]] = []

    def authorize(self, done) -> None:
        self.asked.append(done)

    def grant(self, granted: bool = True, error: str | None = None) -> None:
        for done in self.asked:
            done(granted, error)

    def status(self, done) -> None:
        done(self.status_value)

    def post(self, identifier, title, body, info, done) -> None:
        self.posted.append((identifier, title, body, info))
        done(self.post_error)

    def spin(self, seconds: float) -> None:
        time.sleep(min(seconds, 0.01))


class FakeInside:
    """The window's inside (notifier.InWindow): notes what the page was handed and each call
    for attention; `takes` False stands for a page that is not loaded yet."""

    def __init__(self) -> None:
        self.views: list[dict] = []
        self.attention_calls = 0
        self.holds: list[int] = []
        self.takes = True
        self.closes: set[int] = set()  # what the page closed

    def show(self, view: dict) -> bool:
        if self.takes:
            self.views.append(view)
        return self.takes

    def attention(self) -> None:
        self.attention_calls += 1

    def hold(self, count: int) -> None:
        self.holds.append(count)

    def closed(self) -> set[int]:
        return set(self.closes)

    @property
    def last(self) -> dict:
        return self.views[-1]


@pytest.fixture
def lines() -> list[str]:
    return []


@pytest.fixture
def center() -> FakeCenter:
    return FakeCenter(status=0)


@pytest.fixture
def inside() -> FakeInside:
    return FakeInside()


@pytest.fixture
def window_notifier(
    orch: Orchestrator, center: FakeCenter, lines: list[str], inside: FakeInside, tmp_path: Path
):
    made = notifier.WindowNotifier(
        orch, center, "local.x.app", tmp_path / "X.app", lines.append, inside
    )
    yield made
    made.close()


def texts(log: Path) -> list[str]:
    return [call[-1] for call in calls(log)]


INSIDE = "notices are shown in this window"
ALLOWED = "notifications: X shows them; a click opens it there"


# -- the window process ----------------------------------------------------------------------


def test_before_it_may_post_the_window_takes_the_notices_and_shows_them_inside(
    orch: Orchestrator, center: FakeCenter, window_notifier, inside: FakeInside, lines, monkeypatch
):
    # not started: ticked by hand
    monkeypatch.setattr(orch, "open_decisions", lambda: {4})  # decision 4 is open
    notice = orch.store.add_notice("decision", "a-1", "Decision needed", 4)
    window_notifier.tick()
    assert desktop.app_fresh()  # whatever the permission
    window_notifier.tick()
    assert center.asked == [] and center.posted == [] and lines == []  # not asked yet
    assert orch.notices() == []  # claimed
    assert inside.last["state"] == "waiting"
    assert inside.last["line"] == f"notifications from X: not asked yet; {INSIDE}"
    assert inside.last["notices"] == [
        {
            "id": notice,
            "kind": "decision",
            "text": "Decision needed",
            "target": "decision:4",
            "decision_id": 4,
        }
    ]
    assert inside.attention_calls == 1


def test_the_marker_is_fresh_while_the_window_runs_whatever_the_permission(
    window_notifier, monkeypatch
):
    monkeypatch.setattr(notifier, "POLL_SECONDS", 0.05)
    window_notifier.start()
    until(desktop.app_fresh, what="the marker")
    assert desktop.granted_bundle() is None
    window_notifier.close()
    assert not desktop.app_marker().exists()


def test_asked_once_the_window_is_shown_then_allowed_it_records_the_grant_and_posts_once(
    orch: Orchestrator, center: FakeCenter, window_notifier, inside, lines, tmp_path, monkeypatch
):
    monkeypatch.setattr(notifier, "POLL_SECONDS", 0.05)
    old = orch.store.add_notice("finished", "a-0", "before the window")
    window_notifier.cursor = orch.notice_cursor()  # as if the window opened just now
    window_notifier.start()
    window_notifier.ask()
    window_notifier.ask()
    assert len(center.asked) == 1  # asked once
    assert lines == [
        "notifications from X: asked macOS for permission; until allowed, notices are shown"
        " in this window"
    ]
    center.grant()
    assert lines[-1] == ALLOWED
    assert desktop.granted_bundle() == tmp_path / "X.app"
    decision = orch.store.add_notice("decision", "a-1", "Decision needed", 5)
    finished = orch.store.add_notice("finished", "a-2", "a-2 finished")
    until(lambda: len(center.posted) == 2, what="the posts")
    time.sleep(0.2)
    digest = notifier.home_digest()
    assert center.posted == [
        (f"spartito-notice-{decision}", "Spartito", "Decision needed",
         {"home": digest, "target": "decision:5"}),
        (f"spartito-notice-{finished}", "Spartito", "a-2 finished",
         {"home": digest, "target": "a-2"}),
    ]  # fmt: skip
    assert orch.notices() == []  # older than the window: it waited, shown inside, not posted
    until(lambda: inside.views and inside.last["state"] == "allowed", what="the page's line")
    assert [n["id"] for n in inside.last["notices"]] == [old]
    window_notifier.close()
    assert not desktop.app_marker().exists()


def test_a_lost_answer_is_made_up_for_by_the_status_read_on_each_poll(
    orch: Orchestrator, center: FakeCenter, window_notifier, lines, tmp_path
):
    """Seen in a real run: asked during the launch, the system recorded the permission but the
    request's answer never came. The status read on the next poll sees it."""
    window_notifier.ask()  # never answered
    window_notifier.tick()
    assert window_notifier.state == "asking" and desktop.granted_bundle() is None
    center.status_value = 2  # allowed in the prompt, or later in System Settings
    window_notifier.tick()
    assert window_notifier.state == "allowed" and lines[-2:] == [
        "notifications from X: no answer to the request; the system's authorization status is 2",
        ALLOWED,
    ]
    assert desktop.granted_bundle() == tmp_path / "X.app"
    orch.store.add_notice("finished", "a-1", "a-1 finished")
    window_notifier.tick()
    assert [post[2] for post in center.posted] == ["a-1 finished"]
    center.status_value = 1  # switched off in System Settings
    window_notifier.tick()
    assert window_notifier.state == "denied" and desktop.granted_bundle() is None
    assert lines[-1] == (
        f"notifications from X: not allowed (System Settings › Notifications › X); {INSIDE}"
    )


def test_no_answer_for_long_is_said_once(
    center: FakeCenter, window_notifier, lines, inside, monkeypatch
):
    window_notifier.ask()
    monkeypatch.setattr(notifier, "NO_ANSWER_SECONDS", 0.0)
    window_notifier.tick()
    window_notifier.tick()
    waited = [line for line in lines if "no answer yet" in line]
    assert waited == [
        "notifications from X: asked macOS for permission — no answer yet (look for its prompt"
        f" in Notification Centre); until allowed, {INSIDE}"
    ]
    assert inside.last["line"] == waited[0]


def test_a_refusal_forgets_the_grant_says_so_and_shows_the_notices_inside(
    orch: Orchestrator, center: FakeCenter, window_notifier, lines, inside, tmp_path,
    fake_osascript, monkeypatch,
):  # fmt: skip
    monkeypatch.setattr(desktop, "PLATFORM", "darwin")
    desktop.record_grant("local.x.app", tmp_path / "X.app")
    window_notifier.ask()
    center.grant(False, "UNErrorDomain 1: Notifications are not allowed for this application")
    assert desktop.granted_bundle() is None
    assert lines[-1] == (
        "notifications from X: macOS refused the request (UNErrorDomain 1: Notifications are"
        f" not allowed for this application); {INSIDE}"
    )
    window_notifier.tick()  # a status of 1 does not hide the system's error
    assert window_notifier.state == "refused"
    desktop.show(orch.store, "Decision needed", "a-1", 3)
    window_notifier.tick()
    assert [n["text"] for n in inside.last["notices"]] == ["Decision needed"]
    assert center.posted == []
    time.sleep(0.2)
    assert calls(fake_osascript) == []


def test_not_allowed_in_the_prompt(window_notifier, center: FakeCenter, lines, inside):
    window_notifier.ask()
    center.grant(False)
    assert window_notifier.state == "denied"
    assert lines[-1].startswith("notifications from X: not allowed (System Settings")
    window_notifier.tick()
    assert inside.last["state"] == "denied" and inside.last["line"] == lines[-1]


def test_unavailable_says_why_once_shown_never_asks_and_shows_the_notices_inside(
    orch: Orchestrator, center: FakeCenter, lines, inside, tmp_path
):
    made = notifier.WindowNotifier(
        orch, center, "", None, lines.append, inside, "no launcher: run `spartito app install`"
    )
    assert lines == []  # said once the window is shown
    made.ask()
    made.ask()
    assert center.asked == []
    assert lines == [
        f"notifications from Spartito: not available (no launcher: run `spartito app install`); {INSIDE}"
    ]
    orch.store.add_notice("finished", "a-1", "a-1 finished")
    made.tick()
    assert inside.last["state"] == "unavailable"
    assert [n["target"] for n in inside.last["notices"]] == ["a-1"]
    assert desktop.granted_bundle() is None


def test_while_the_window_runs_osascript_never_shows_a_notice(
    orch: Orchestrator, center: FakeCenter, window_notifier, inside, fake_osascript, monkeypatch
):
    """desktop.show leaves the notice to the window process whether it may post or not."""
    monkeypatch.setattr(desktop, "PLATFORM", "darwin")
    # not started: ticked by hand
    window_notifier.tick()
    desktop.show(orch.store, "a-1 finished", "a-1")  # not allowed: shown inside
    window_notifier.tick()
    window_notifier.ask()
    center.grant()
    desktop.show(orch.store, "Decision needed", "a-2", 7)  # allowed: posted
    claimed = orch.store.add_notice("finished", "a-3", "a page took it")
    orch.claim_notice(claimed)
    window_notifier.tick()
    window_notifier.tick()
    assert [n["text"] for n in inside.last["notices"]] == ["a-1 finished"]
    assert [post[2] for post in center.posted] == ["Decision needed"]
    time.sleep(0.2)
    assert calls(fake_osascript) == []


def test_a_post_the_centre_refuses_is_shown_inside_not_by_osascript(
    orch: Orchestrator, window_notifier, center: FakeCenter, inside, fake_osascript, monkeypatch
):
    monkeypatch.setattr(desktop, "PLATFORM", "darwin")
    center.post_error = "UNErrorDomain 1: not allowed"
    window_notifier.ask()
    center.grant()
    orch.store.add_notice("decision", "a-1", "Decision needed", 2)
    window_notifier.tick()
    assert [n["target"] for n in inside.last["notices"]] == ["decision:2"]
    assert orch.notices() == []
    time.sleep(0.2)
    assert calls(fake_osascript) == []


def test_only_the_newest_notices_are_kept_inside_each_drawing_the_eye(
    orch: Orchestrator, window_notifier, inside
):
    for n in range(notifier.IN_WINDOW + 2):
        orch.store.add_notice("finished", f"a-{n}", f"a-{n} finished")
    window_notifier.tick()
    more, *banners = inside.last["notices"]
    assert [n["target"] for n in banners] == [f"a-{n}" for n in range(2, notifier.IN_WINDOW + 2)]
    assert more["kind"] == "more"  # the older ones are counted, not lost
    assert [n["target"] for n in more["hidden"]] == ["a-0", "a-1"]
    assert inside.attention_calls == notifier.IN_WINDOW + 2


def test_the_page_gets_the_view_until_it_has_it_and_again_after_a_reload(
    orch: Orchestrator, window_notifier, inside
):
    inside.takes = False  # the page is not loaded yet
    orch.store.add_notice("finished", "a-1", "a-1 finished")
    window_notifier.tick()
    assert inside.views == []
    inside.takes = True
    window_notifier.tick()
    window_notifier.tick()
    assert len(inside.views) == 1  # handed over once
    window_notifier.repush()  # the page was reloaded
    window_notifier.tick()
    assert len(inside.views) == 2 and inside.views[0] == inside.views[1]


# -- the window closing ----------------------------------------------------------------------


def osascript_texts(log: Path, count: int) -> list[str]:
    until(lambda: len(calls(log)) >= count, what=f"{count} osascript call(s)")
    time.sleep(0.2)  # and no more
    return texts(log)


def test_at_close_a_notice_the_page_never_took_is_shown_as_without_a_window_once(
    orch: Orchestrator, window_notifier, inside, lines, fake_osascript, monkeypatch
):
    """(a) Claimed and shown inside, but the page did not take it: osascript at close."""
    monkeypatch.setattr(desktop, "PLATFORM", "darwin")
    inside.takes = False
    orch.store.add_notice("decision", "a-1", "Decision needed", 4)
    window_notifier.tick()
    assert orch.notices() == [] and calls(fake_osascript) == []  # the window's while it runs
    window_notifier.close()
    assert osascript_texts(fake_osascript, 1) == ["Decision needed — open Spartito to answer"]
    assert orch.notices() == []  # claimed again, by osascript
    assert (
        lines[-1]
        == "notifications: the window closed with 1 notice(s) it had not shown; handed over"
        " (`spartito notices` shows where)"
    )
    window_notifier.close()  # the fixture's close: nothing twice
    assert len(calls(fake_osascript)) == 1


def test_at_close_a_notice_the_page_took_is_not_shown_again(
    orch: Orchestrator, window_notifier, inside, lines, fake_osascript, monkeypatch
):
    monkeypatch.setattr(desktop, "PLATFORM", "darwin")
    orch.store.add_notice("finished", "a-1", "a-1 finished")
    window_notifier.tick()
    assert [n["target"] for n in inside.last["notices"]] == ["a-1"]
    window_notifier.close()
    time.sleep(0.3)
    assert calls(fake_osascript) == [] and lines == []


def test_closing_the_window_claims_and_pushes_nothing_more_and_close_hands_it_over(
    orch: Orchestrator, window_notifier, inside, fake_osascript, monkeypatch
):
    """(b) Between the end of the GUI loop and `close`, a poll claims nothing for a page that
    is gone; the notice is left for the hand-over."""
    monkeypatch.setattr(desktop, "PLATFORM", "darwin")
    window_notifier.tick()
    pushed = len(inside.views)
    window_notifier.closing()
    orch.store.add_notice("finished", "a-1", "a-1 finished")
    window_notifier.tick()
    assert [n["agent_id"] for n in orch.notices()] == ["a-1"]  # not claimed
    assert len(inside.views) == pushed and inside.attention_calls == 0
    window_notifier.close()
    assert osascript_texts(fake_osascript, 1) == ["a-1 finished — open Spartito to see it"]


def test_a_notice_recorded_after_the_last_poll_is_shown_once_the_window_closes(
    orch: Orchestrator, window_notifier, fake_osascript, monkeypatch
):
    """(c) desktop.show leaves it to the window (fresh marker, live pid); the window closes
    before its next poll: the hand-over shows it, once."""
    monkeypatch.setattr(desktop, "PLATFORM", "darwin")
    window_notifier.tick()
    desktop.show(orch.store, "Decision needed", "a-1", 5)
    time.sleep(0.2)
    assert calls(fake_osascript) == []  # left to the window
    window_notifier.close()
    assert osascript_texts(fake_osascript, 1) == ["Decision needed — open Spartito to answer"]
    desktop.show(orch.store, "a-2 finished", "a-2")  # no window now: osascript, as before
    assert osascript_texts(fake_osascript, 2)[1] == "a-2 finished — open Spartito to see it"


def test_without_a_window_the_launchers_app_posts_a_handed_over_notice(
    orch: Orchestrator, fake_osascript, tmp_path, monkeypatch, no_detached_helpers
):
    """The hand-over asks desktop.route without the window: the launcher's app when it may
    post (its helper never falls back to osascript), else osascript."""
    monkeypatch.setattr(desktop, "PLATFORM", "darwin")
    monkeypatch.setattr(desktop, "bundle_state", lambda: (tmp_path / "X.app", ""))
    notice = orch.store.add_notice("decision", "a-1", "Decision needed", 4, route="window")
    assert orch.claim_notice(notice, "window")  # the window took it
    orch.hand_over_notice({"id": notice, "text": "Decision needed", "decision_id": 4})
    assert no_detached_helpers == [["deliver", str(notice), desktop.OPEN]]
    assert [(n["id"], n["route"]) for n in orch.notices()] == [(notice, "helper")]
    time.sleep(0.2)
    assert calls(fake_osascript) == []


def test_the_hand_over_shows_a_notice_a_page_claimed_meanwhile_only_once(
    orch: Orchestrator, window_notifier, fake_osascript, monkeypatch
):
    """The window listed it, a page claimed it, the window closed: the hand-over leaves the
    page's claim alone (the double show of item 20)."""
    monkeypatch.setattr(desktop, "PLATFORM", "darwin")
    window_notifier.tick()
    notice = orch.store.add_notice("finished", "a-1", "a-1 finished", route="page")
    listed = orch.notices(0)
    assert orch.claim_notice(notice, "page")  # the page was quicker
    for item in listed:
        orch.hand_over_notice(item)
    window_notifier.close()
    time.sleep(0.3)
    assert calls(fake_osascript) == [] and orch.notices() == []
    (traced,) = orch.notice_trace(1)
    assert traced["summary"]["path"] == "page" and traced["claimed_by"].startswith("page:")


def test_while_the_page_does_not_take_the_notices_it_keeps_trying_and_the_badge_says_so(
    orch: Orchestrator, window_notifier, inside, lines, fake_osascript, monkeypatch
):
    monkeypatch.setattr(desktop, "PLATFORM", "darwin")
    monkeypatch.setattr(notifier, "PAGE_WAIT_SECONDS", 0.0)
    inside.takes = False  # no hook in the page, or evaluate_js fails
    orch.store.add_notice("finished", "a-1", "a-1 finished")
    orch.store.add_notice("finished", "a-2", "a-2 finished")
    window_notifier.tick()
    window_notifier.tick()
    window_notifier.tick()
    assert inside.holds == [1, 2]  # the badge keeps them
    stuck = (
        "notifications: the window's page did not take 1 notice(s) (no hook in the page, or it"
        " did not answer); the Dock's badge keeps them, and macOS shows them when the window"
        " closes"
    )
    assert [line for line in lines if "did not take" in line] == [stuck]  # said once
    assert calls(fake_osascript) == []
    inside.takes = True
    window_notifier.tick()
    assert inside.holds == [1, 2, 0]
    assert [n["target"] for n in inside.last["notices"]] == ["a-1", "a-2"]
    assert lines[-1] == "notifications: the window's page shows the notices again"
    window_notifier.close()
    time.sleep(0.3)
    assert calls(fake_osascript) == []  # the page had them


class DroppingCenter(FakeCenter):
    """Drops the completion of the first status read, as the system was seen to do."""

    def __init__(self) -> None:
        super().__init__(status=2)
        self.reads = 0

    def status(self, done) -> None:
        self.reads += 1
        if self.reads > 1:
            done(self.status_value)


def test_a_status_read_without_a_completion_does_not_stop_later_reads(
    orch: Orchestrator, lines, inside, tmp_path, monkeypatch
):
    center = DroppingCenter()
    made = notifier.WindowNotifier(
        orch, center, "local.x.app", tmp_path / "X.app", lines.append, inside
    )
    made.ask()  # no answer to the request either
    made.tick()
    made.tick()
    assert center.reads == 1 and made.state == notifier.ASKING  # waiting for that read
    monkeypatch.setattr(notifier, "STATUS_WAIT_SECONDS", 0.0)
    made.tick()
    assert center.reads == 2 and made.state == notifier.ALLOWED
    made.close()


class LaterCenter(FakeCenter):
    """Completes each status read later, on another thread (as the system does)."""

    def __init__(self) -> None:
        super().__init__(status=0)
        self.waiting: list = []

    def status(self, done) -> None:
        self.waiting.append(done)


def test_an_error_in_the_status_callback_does_not_stop_later_reads(
    orch: Orchestrator, inside, tmp_path
):
    center = LaterCenter()
    failing = [True]

    def say(line: str) -> None:
        if failing and "no answer to the request" in line:
            failing.clear()
            raise OSError("stderr is gone")

    made = notifier.WindowNotifier(orch, center, "local.x.app", tmp_path / "X.app", say, inside)
    made.ask()
    made.tick()
    center.waiting.pop()(1)  # its callback fails inside: nothing reaches the centre
    made.tick()
    assert len(center.waiting) == 1  # read again at once
    center.waiting.pop()(2)
    assert made.state == notifier.ALLOWED
    made.close()


# -- a click ---------------------------------------------------------------------------------


@pytest.mark.parametrize("target", ["decision:5", "a-1"])
def test_a_click_opens_the_window_at_the_target_through_a_deep_link(
    orch: Orchestrator, center: FakeCenter, window_notifier, target
):
    window_notifier.ask()
    center.grant()
    agent, decision = ("a-0", 5) if target.startswith("decision:") else (target, None)
    orch.store.add_notice("decision" if decision else "finished", agent, "text", decision)
    window_notifier.tick()
    (_, _, _, info) = center.posted[0]
    respond = notifier.opener(Orchestrator.request_app_open)
    respond(notifier.DEFAULT_ACTION, dict(info))  # what the delegate hands on
    assert app_open.latest()["target"] == target


HOME = object()  # stands for this home's digest


@pytest.mark.parametrize(
    "action, info",
    [
        ("com.apple.UNNotificationDismissActionIdentifier", {"home": HOME, "target": "a-1"}),
        (notifier.DEFAULT_ACTION, {"home": "0" * 16, "target": "a-1"}),  # another home's
        (notifier.DEFAULT_ACTION, {"target": "a-1"}),  # no home
        (notifier.DEFAULT_ACTION, {"home": HOME, "target": "../../etc"}),
        (notifier.DEFAULT_ACTION, {"home": HOME, "target": "decision:five"}),
        (notifier.DEFAULT_ACTION, {"home": HOME, "target": ""}),
        (notifier.DEFAULT_ACTION, {"home": HOME, "target": 5}),
        (notifier.DEFAULT_ACTION, {"home": HOME}),
    ],
)
def test_other_clicks_open_nothing(action: str, info: dict):
    info = {key: notifier.home_digest() if value is HOME else value for key, value in info.items()}
    opened: list[str] = []
    notifier.opener(opened.append)(action, info)
    assert opened == [] and notifier.target_from(action, info) is None
    assert app_open.latest() is None
    assert (
        notifier.target_from(
            notifier.DEFAULT_ACTION, {**info, "home": notifier.home_digest(), "target": "a-1"}
        )
        == "a-1"
    )


def test_the_delegate_hands_a_click_on_and_always_completes():
    """The real Objective-C class (pyobjc), called as the centre would, with fake objects; the
    notification centre itself is not touched."""
    pytest.importorskip("UserNotifications")
    import objc

    cls = notifier.delegate_class()
    assert cls is notifier.delegate_class()  # made once
    delegate = cls.alloc().init()
    assert delegate.conformsToProtocol_(objc.protocolNamed("UNUserNotificationCenterDelegate"))
    got: list = []

    class Obj:
        def __init__(self, **values) -> None:
            self.values = values

        def __getattr__(self, name):
            return lambda: self.values[name]

    content = Obj(userInfo={"home": "h", "target": "a-1"})
    response = Obj(
        actionIdentifier=notifier.DEFAULT_ACTION, notification=Obj(request=Obj(content=content))
    )
    old = notifier._respond
    try:
        notifier._respond = lambda action, info: got.append((action, info))
        delegate.userNotificationCenter_didReceiveNotificationResponse_withCompletionHandler_(
            None, response, lambda: got.append("completed")
        )
        delegate.userNotificationCenter_didReceiveNotificationResponse_withCompletionHandler_(
            None, None, lambda: got.append("completed anyway")
        )
        delegate.userNotificationCenter_willPresentNotification_withCompletionHandler_(
            None, None, got.append
        )
    finally:
        notifier._respond = old
    assert got[:3] == [
        (notifier.DEFAULT_ACTION, {"home": "h", "target": "a-1"}),
        "completed",
        "completed anyway",
    ]
    assert isinstance(got[3], int) and got[3] > 0  # banner, list, sound


# -- the posting process (`spartito app --notify`) -----------------------------------------------


def age(orch: Orchestrator, notice: int, seconds: float) -> None:
    then = (datetime.now(UTC) - timedelta(seconds=seconds)).isoformat(timespec="seconds")
    with orch.store.connection() as conn:
        conn.execute("UPDATE notices SET created_at = ? WHERE id = ?", (then, notice))


def test_allowed_it_posts_the_fresh_pending_notices_once(
    orch: Orchestrator, center: FakeCenter, fake_osascript, tmp_path
):
    old = orch.store.add_notice("finished", "a-0", "too old")
    age(orch, old, notifier.MAX_AGE_SECONDS + 60)
    taken = orch.store.add_notice("finished", "a-1", "taken")
    orch.claim_notice(taken)
    fresh = orch.store.add_notice("finished", "a-2", "a-2 finished")
    desktop.record_grant("local.x.app", tmp_path / "X.app")
    center.status_value = 2
    assert notifier.post_pending(orch, center, tmp_path / "X.app", []) is None
    assert [(post[2], post[3]["target"]) for post in center.posted] == [("a-2 finished", "a-2")]
    assert [n["id"] for n in orch.notices()] == [old] and fresh
    assert desktop.granted_bundle() == tmp_path / "X.app"
    assert notifier.post_pending(orch, center, tmp_path / "X.app", []) is None
    assert len(center.posted) == 1
    time.sleep(0.2)
    assert calls(fake_osascript) == []


@pytest.mark.parametrize("status", [0, 1])
def test_not_allowed_it_forgets_the_grant_and_the_notices_wait(
    orch: Orchestrator, fake_osascript, tmp_path, monkeypatch, status
):
    monkeypatch.setattr(desktop, "PLATFORM", "darwin")
    desktop.record_grant("local.x.app", tmp_path / "X.app")
    notice = orch.store.add_notice("decision", "a-2", "Decision needed", 3, route="helper")
    lines: list[str] = []
    center = FakeCenter(status=status)
    assert notifier.post_pending(orch, center, tmp_path / "X.app", [], lines.append) is None
    assert center.posted == [] and desktop.granted_bundle() is None
    why = f"not allowed (authorization status {status})"
    assert lines == [f"notifications from Spartito: {why}; the notices wait"]
    time.sleep(0.3)
    assert calls(fake_osascript) == []
    assert [(n["id"], n["route"]) for n in orch.notices()] == [(notice, "waiting")]
    assert orch.notice_trace(1)[0]["summary"]["reason"] == why


def test_a_post_that_fails_releases_the_notice_to_wait(
    orch: Orchestrator, fake_osascript, tmp_path
):
    notice = orch.store.add_notice("finished", "a-1", "a-1 finished", route="helper")
    center = FakeCenter(status=2, post_error="UNErrorDomain 1: refused")
    assert notifier.post_pending(orch, center, tmp_path / "X.app", []) is None
    assert len(center.posted) == 1
    time.sleep(0.3)
    assert calls(fake_osascript) == []
    assert [(n["id"], n["route"]) for n in orch.notices()] == [(notice, "waiting")]
    summary = orch.notice_trace(1)[0]["summary"]
    assert (
        summary["path"] == "waiting"
        and summary["reason"] == "post failed: UNErrorDomain 1: refused"
    )
    assert summary["actor"].startswith("notify:")


def test_a_click_that_reaches_the_posting_process_is_handed_on(orch: Orchestrator, tmp_path):
    clicked: list[str] = []
    center = FakeCenter()

    def post(identifier, title, body, info, done):
        done(None)
        notifier.opener(clicked.append)(notifier.DEFAULT_ACTION, info)  # clicked at once

    center.post = post  # type: ignore[method-assign]
    orch.store.add_notice("finished", "a-1", "a-1 finished")
    assert notifier.post_pending(orch, center, tmp_path / "X.app", clicked) == "a-1"


def test_without_the_framework_the_notices_wait(
    orch: Orchestrator, fake_osascript, tmp_path, monkeypatch
):
    """conftest.py makes the real centre unavailable, as a missing framework would."""
    monkeypatch.setattr(desktop, "PLATFORM", "darwin")
    orch.store.add_notice("finished", "a-1", "a-1 finished", route="helper")
    assert notifier.notify(orch, tmp_path / "X.app") is None
    time.sleep(0.3)
    assert calls(fake_osascript) == [] and [n["route"] for n in orch.notices()] == ["waiting"]


def test_the_framework_is_imported_only_where_it_is_used():
    import subprocess
    import sys

    code = (
        "import sys; from orchestrator import cli, desktop; from orchestrator.gui import"
        " notifier, window, launcher; print('UserNotifications' in sys.modules)"
    )
    done = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60)
    assert done.stdout.strip() == "False", done.stderr


def test_no_answer_in_time_the_notices_wait_and_the_grant_stays(
    orch: Orchestrator, fake_osascript, tmp_path, monkeypatch
):
    monkeypatch.setattr(desktop, "PLATFORM", "darwin")
    monkeypatch.setattr(notifier, "WAIT_SECONDS", 0.2)
    desktop.record_grant("local.x.app", tmp_path / "X.app")
    orch.store.add_notice("finished", "a-1", "a-1 finished", route="helper")
    center = FakeCenter()
    center.status = lambda done: None  # type: ignore[method-assign]  # never answers
    lines: list[str] = []
    assert notifier.post_pending(orch, center, tmp_path / "X.app", [], lines.append) is None
    why = "the notification centre did not answer within 0.2 s"
    assert lines == [f"notifications from Spartito: {why}; the notices wait"]
    assert desktop.granted_bundle() == tmp_path / "X.app" and center.posted == []
    time.sleep(0.3)
    assert calls(fake_osascript) == [] and [n["route"] for n in orch.notices()] == ["waiting"]
    assert orch.notice_trace(1)[0]["summary"]["reason"] == why
