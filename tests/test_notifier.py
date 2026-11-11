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


@pytest.fixture
def lines() -> list[str]:
    return []


@pytest.fixture
def center() -> FakeCenter:
    return FakeCenter()


@pytest.fixture
def window_notifier(orch: Orchestrator, center: FakeCenter, lines: list[str], tmp_path: Path):
    made = notifier.WindowNotifier(orch, center, "local.x.app", tmp_path / "X.app", lines.append)
    yield made
    made.close()


def texts(log: Path) -> list[str]:
    return [call[-1] for call in calls(log)]


# -- the window process ----------------------------------------------------------------------


def test_the_window_asks_once_and_does_nothing_before_the_answer(
    orch: Orchestrator, center: FakeCenter, window_notifier, tmp_path
):
    window_notifier.start()
    assert len(center.asked) == 1
    orch.store.add_notice("finished", "a-1", "a-1 finished")
    time.sleep(0.1)
    assert not desktop.app_fresh() and desktop.granted_bundle() is None
    assert center.posted == [] and len(orch.notices()) == 1


def test_once_allowed_it_records_the_grant_marks_itself_and_posts_each_notice_once(
    orch: Orchestrator, center: FakeCenter, window_notifier, lines, tmp_path, monkeypatch
):
    monkeypatch.setattr(notifier, "POLL_SECONDS", 0.05)
    old = orch.store.add_notice("finished", "a-0", "before the window")
    window_notifier.cursor = orch.notice_cursor()  # as if the window opened just now
    window_notifier.start()
    center.grant()
    assert lines == ["notifications: Spartito shows them; a click opens it there"]
    assert desktop.granted_bundle() == tmp_path / "X.app"
    until(desktop.app_fresh, what="the marker")
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
    assert [n["id"] for n in orch.notices()] == [old]  # older than the window: not replayed
    window_notifier.close()
    assert not desktop.app_marker().exists()


def test_a_refusal_forgets_the_grant_and_says_so(
    orch: Orchestrator, center: FakeCenter, window_notifier, lines, tmp_path
):
    desktop.record_grant("local.x.app", tmp_path / "X.app")
    window_notifier.start()
    center.grant(False, "UNErrorDomain 1: Notifications are not allowed for this application")
    assert desktop.granted_bundle() is None and window_notifier.thread is None
    assert lines == [
        "notifications from Spartito: the system refused: UNErrorDomain 1: Notifications are"
        " not allowed for this application (System Settings › Notifications); without a"
        " dashboard page osascript shows them"
    ]
    center.asked.clear()
    window_notifier.start()
    center.grant(False)
    assert lines[-1].startswith("notifications from Spartito: not allowed (System Settings")


def test_the_window_and_osascript_never_show_the_same_notice(
    orch: Orchestrator, center: FakeCenter, window_notifier, fake_osascript, monkeypatch
):
    monkeypatch.setattr(desktop, "PLATFORM", "darwin")
    window_notifier.start()
    center.grant()
    window_notifier.close()  # tick by hand from here
    window_notifier.tick()
    desktop.show(orch.store, "a-1 finished", "a-1")  # the marker is fresh: left to the window
    claimed = orch.store.add_notice("finished", "a-2", "a page took it")
    orch.claim_notice(claimed)
    window_notifier.tick()
    window_notifier.tick()
    assert [post[2] for post in center.posted] == ["a-1 finished"]
    time.sleep(0.2)
    assert calls(fake_osascript) == []


def test_a_post_the_centre_refuses_is_shown_by_osascript(
    orch: Orchestrator, window_notifier, center: FakeCenter, fake_osascript, monkeypatch
):
    monkeypatch.setattr(desktop, "PLATFORM", "darwin")
    center.post_error = "UNErrorDomain 1: not allowed"
    window_notifier.start()
    center.grant()
    window_notifier.close()
    orch.store.add_notice("decision", "a-1", "Decision needed", 2)
    window_notifier.tick()
    until(lambda: calls(fake_osascript), what="the notification")
    assert texts(fake_osascript) == ["Decision needed — open Spartito to answer"]
    assert orch.notices() == []


# -- a click ---------------------------------------------------------------------------------


@pytest.mark.parametrize("target", ["decision:5", "a-1"])
def test_a_click_opens_the_window_at_the_target_through_a_deep_link(
    orch: Orchestrator, center: FakeCenter, window_notifier, target
):
    window_notifier.start()
    center.grant()
    window_notifier.close()
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


# -- the posting process (`orch app --notify`) -----------------------------------------------


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
    fresh = orch.store.add_notice("decision", "a-2", "Decision needed", 3)
    desktop.record_grant("local.x.app", tmp_path / "X.app")
    assert notifier.post_pending(orch, center, tmp_path / "X.app", []) is None
    assert [(post[2], post[3]["target"]) for post in center.posted] == [
        ("Decision needed", "decision:3")
    ]
    assert [n["id"] for n in orch.notices()] == [old] and fresh
    assert desktop.granted_bundle() == tmp_path / "X.app"
    assert notifier.post_pending(orch, center, tmp_path / "X.app", []) is None
    assert len(center.posted) == 1
    time.sleep(0.2)
    assert calls(fake_osascript) == []


@pytest.mark.parametrize("status", [0, 1])
def test_not_allowed_it_forgets_the_grant_and_osascript_shows_them(
    orch: Orchestrator, fake_osascript, tmp_path, monkeypatch, status
):
    monkeypatch.setattr(desktop, "PLATFORM", "darwin")
    desktop.record_grant("local.x.app", tmp_path / "X.app")
    orch.store.add_notice("decision", "a-2", "Decision needed", 3)
    lines: list[str] = []
    center = FakeCenter(status=status)
    assert notifier.post_pending(orch, center, tmp_path / "X.app", [], lines.append) is None
    assert center.posted == [] and desktop.granted_bundle() is None
    assert lines == [
        f"notifications from Spartito: not allowed (authorization status {status}); osascript"
    ]
    until(lambda: calls(fake_osascript), what="the notification")
    assert texts(fake_osascript) == ["Decision needed — open Spartito to answer"]
    assert orch.notices() == []


def test_a_click_that_reaches_the_posting_process_is_handed_on(orch: Orchestrator, tmp_path):
    clicked: list[str] = []
    center = FakeCenter()

    def post(identifier, title, body, info, done):
        done(None)
        notifier.opener(clicked.append)(notifier.DEFAULT_ACTION, info)  # clicked at once

    center.post = post  # type: ignore[method-assign]
    orch.store.add_notice("finished", "a-1", "a-1 finished")
    assert notifier.post_pending(orch, center, tmp_path / "X.app", clicked) == "a-1"


def test_without_the_framework_osascript_shows_them(
    orch: Orchestrator, fake_osascript, tmp_path, monkeypatch
):
    """conftest.py makes the real centre unavailable, as a missing framework would."""
    monkeypatch.setattr(desktop, "PLATFORM", "darwin")
    orch.store.add_notice("finished", "a-1", "a-1 finished")
    assert notifier.notify(orch, tmp_path / "X.app") is None
    until(lambda: calls(fake_osascript), what="the notification")
    assert texts(fake_osascript) == ["a-1 finished — open Spartito to see it"]


def test_the_framework_is_imported_only_where_it_is_used():
    import subprocess
    import sys

    code = (
        "import sys; from orchestrator import cli, desktop; from orchestrator.gui import"
        " notifier, window, launcher; print('UserNotifications' in sys.modules)"
    )
    done = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60)
    assert done.stdout.strip() == "False", done.stderr
