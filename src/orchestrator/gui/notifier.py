"""Notifications posted by Spartito itself on macOS, through the UserNotifications framework
(pyobjc-framework-UserNotifications, in the extra `app`; imported here and nowhere else), by a
process that is the launcher's app (launcher.py: its interpreter copy, signed with the bundle's
identifier; the notification centre accepts no other):

- the window process (`orch app`, window.py): asks for permission once at start and sets the
  centre's delegate before the GUI loop runs. Once allowed, it records the grant
  (desktop.record_grant), keeps its marker fresh (desktop.app_present, so osascript and the
  posting process leave new notices to it) and every POLL_SECONDS claims the new notices
  (through the facade, once each, as a page does) and posts them;
- the posting process (`orch app --notify`, started hidden by desktop.bundle_helper when no
  window runs): claims and posts the pending notices younger than MAX_AGE_SECONDS, then exits;
  when it is not allowed (any more), it forgets the grant and shows them with osascript.

Each notification carries a userInfo {"home": <digest of this home>, "target": "decision:<n>"
or the agent id}. A click on it (the default action) reaches the delegate of the running app,
or of the app the system starts for it (`orch app` without arguments, the same window code).
A target for this home, checked like a deep link (app_open.check_target), becomes an app-open
request: the window applies it and comes to the front (window.watching_requests). A click
that reaches the posting process is handed to its caller, which opens the window there.
"""

from __future__ import annotations

import contextlib
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from .. import app_open, autostart, config, desktop

POLL_SECONDS = 2.0  # the window process looks for new notices this often
MAX_AGE_SECONDS = 600.0  # the posting process skips older notices, as a page does
WAIT_SECONDS = 5.0  # for an answer of the notification centre
LINGER_SECONDS = 1.0  # the posting process stays this long after posting, for a quick click
AUTHORIZED = frozenset({2, 3, 4})  # UNAuthorizationStatus: authorized, provisional, ephemeral
DEFAULT_ACTION = "com.apple.UNNotificationDefaultActionIdentifier"
TEXT_CHARS = 240


def available() -> bool:
    """The framework binding is installed (it is not imported for this)."""
    import importlib.util

    try:
        return importlib.util.find_spec("UserNotifications") is not None
    except ImportError, ValueError:
        return False


def home_digest() -> str:
    """What /api/ping reports as this home."""
    return autostart.home_hash(autostart.home())[:16]


def user_info(notice: dict[str, Any]) -> dict[str, str]:
    return {
        "home": home_digest(),
        "target": desktop.target_of(notice["agent_id"], notice.get("decision_id")),
    }


def target_from(action: str, info: dict[str, Any]) -> str | None:
    """The target a click on a notification opens: only for the default action (a click on
    the notification itself), a notification of this home and a valid target."""
    if action != DEFAULT_ACTION or info.get("home") != home_digest():
        return None
    target = info.get("target")
    if not isinstance(target, str) or not target:
        return None
    try:
        return app_open.check_target(target)
    except ValueError:
        return None


class Center(Protocol):
    """What this module uses of the notification centre (MacCenter, or a fake)."""

    def authorize(self, done: Callable[[bool, str | None], None]) -> None: ...
    def status(self, done: Callable[[int], None]) -> None: ...
    def post(
        self, identifier: str, title: str, body: str, info: dict[str, str], done: Callable
    ) -> None: ...
    def spin(self, seconds: float) -> None: ...


# -- the framework ---------------------------------------------------------------------------

_respond: Callable[[str, dict[str, Any]], None] | None = None  # the delegate's receiver
_delegate_class: Any = None


def respond(action: str, info: dict[str, Any]) -> None:
    if _respond is not None:
        _respond(action, info)


def delegate_class() -> Any:
    """The Objective-C class of the centre's delegate (made once per process)."""
    global _delegate_class
    if _delegate_class is None:
        import Foundation
        import objc
        import UserNotifications as UN

        present = (
            UN.UNNotificationPresentationOptionBanner
            | UN.UNNotificationPresentationOptionList
            | UN.UNNotificationPresentationOptionSound
        )

        class SpartitoNotificationDelegate(
            Foundation.NSObject, protocols=[objc.protocolNamed("UNUserNotificationCenterDelegate")]
        ):
            def userNotificationCenter_didReceiveNotificationResponse_withCompletionHandler_(
                self, center, response, handler
            ):
                try:
                    info = response.notification().request().content().userInfo() or {}
                    respond(
                        str(response.actionIdentifier()),
                        {str(key): str(value) for key, value in info.items()},
                    )
                except Exception:
                    pass
                finally:
                    handler()

            def userNotificationCenter_willPresentNotification_withCompletionHandler_(
                self, center, notification, handler
            ):
                handler(present)  # shown also while the window is the active app

        _delegate_class = SpartitoNotificationDelegate
    return _delegate_class


def describe(error: Any) -> str | None:
    if error is None:
        return None
    try:
        return f"{error.domain()} {error.code()}: {error.localizedDescription()}"
    except Exception:
        return str(error)


class MacCenter:
    """UNUserNotificationCenter of this process, with this module's delegate set at once."""

    def __init__(self, on_response: Callable[[str, dict[str, Any]], None]) -> None:
        global _respond
        import UserNotifications as UN

        self.UN = UN
        self.center = UN.UNUserNotificationCenter.currentNotificationCenter()
        _respond = on_response
        self.delegate = delegate_class().alloc().init()
        self.center.setDelegate_(self.delegate)

    def authorize(self, done: Callable[[bool, str | None], None]) -> None:
        options = self.UN.UNAuthorizationOptionAlert | self.UN.UNAuthorizationOptionSound
        self.center.requestAuthorizationWithOptions_completionHandler_(
            options, lambda granted, error: done(bool(granted), describe(error))
        )

    def status(self, done: Callable[[int], None]) -> None:
        self.center.getNotificationSettingsWithCompletionHandler_(
            lambda settings: done(int(settings.authorizationStatus()))
        )

    def post(
        self, identifier: str, title: str, body: str, info: dict[str, str], done: Callable
    ) -> None:
        content = self.UN.UNMutableNotificationContent.alloc().init()
        content.setTitle_(title)
        content.setBody_(body)
        content.setUserInfo_(info)
        content.setSound_(self.UN.UNNotificationSound.defaultSound())
        request = self.UN.UNNotificationRequest.requestWithIdentifier_content_trigger_(
            identifier, content, None
        )
        self.center.addNotificationRequest_withCompletionHandler_(
            request, lambda error: done(describe(error))
        )

    def spin(self, seconds: float) -> None:
        """Let this thread's run loop deliver what the centre answers (the posting process
        has no GUI loop)."""
        import Foundation

        Foundation.NSRunLoop.currentRunLoop().runMode_beforeDate_(
            Foundation.NSDefaultRunLoopMode,
            Foundation.NSDate.dateWithTimeIntervalSinceNow_(seconds),
        )


# -- posting ---------------------------------------------------------------------------------


class Facade(Protocol):
    def notices(self, after: int = 0) -> list[dict[str, Any]]: ...
    def notice_cursor(self) -> int: ...
    def claim_notice(self, notice_id: int) -> bool: ...


def text_of(notice: dict[str, Any]) -> str:
    return desktop.one_line(str(notice["text"]), TEXT_CHARS)


def post(center: Center, notice: dict[str, Any], done: Callable[[str | None], None]) -> None:
    """Post a claimed notice; a refused one is shown with osascript instead."""

    def posted(error: str | None) -> None:
        if error:
            desktop.plain(None, None, str(notice["text"]), notice.get("decision_id"))
        done(error)

    try:
        center.post(
            f"spartito-notice-{notice['id']}",
            config.PROJECT_NAME,
            text_of(notice),
            user_info(notice),
            posted,
        )
    except Exception as exc:
        posted(str(exc) or type(exc).__name__)


def wait_for(center: Center, ask: Callable[[Callable], None], seconds: float = WAIT_SECONDS):
    """Ask the centre (`ask(callback)`) and wait for its answer: the callback's arguments, or
    None when none came in time."""
    answer: list[tuple] = []
    ask(lambda *args: answer.append(args))
    deadline = time.monotonic() + seconds
    while not answer and time.monotonic() < deadline:
        center.spin(0.05)
    return answer[0] if answer else None


class WindowNotifier:
    """The window process's part: permission, marker, polling, clicks."""

    def __init__(
        self,
        orch: Facade,
        center: Center,
        identifier: str,
        bundle: Path,
        say: Callable[[str], None],
    ) -> None:
        self.orch, self.center, self.say = orch, center, say
        self.identifier, self.bundle = identifier, bundle
        self.cursor = orch.notice_cursor()  # a window opened now replays nothing older
        self.stop = threading.Event()
        self.thread: threading.Thread | None = None

    def start(self) -> None:
        """Ask for permission (the system prompts once); polling starts once allowed."""
        self.center.authorize(self.authorized)

    def authorized(self, granted: bool, error: str | None) -> None:
        if self.stop.is_set():
            return
        if not granted:
            desktop.forget_grant(self.bundle)
            why = f"the system refused: {error}" if error else "not allowed"
            self.say(
                f"notifications from {config.PROJECT_NAME}: {why} (System Settings ›"
                " Notifications); without a dashboard page osascript shows them"
            )
            return
        desktop.record_grant(self.identifier, self.bundle)
        self.say(f"notifications: {config.PROJECT_NAME} shows them; a click opens it there")
        self.thread = threading.Thread(target=self.loop, name="orch-app-notices", daemon=True)
        self.thread.start()

    def loop(self) -> None:
        while not self.stop.is_set():
            with contextlib.suppress(Exception):  # the store was busy: next time
                self.tick()
            self.stop.wait(POLL_SECONDS)

    def tick(self) -> None:
        desktop.app_present()
        for notice in self.orch.notices(self.cursor):
            if self.orch.claim_notice(notice["id"]):
                post(self.center, notice, lambda error: None)
            self.cursor = notice["id"]

    def close(self) -> None:
        self.stop.set()
        if self.thread is not None:
            self.thread.join(POLL_SECONDS + 1)
        desktop.app_marker().unlink(missing_ok=True)


def opener(open_target: Callable[[str], Any]) -> Callable[[str, dict[str, Any]], None]:
    """The delegate's receiver in the window process: a click opens its target."""

    def on_response(action: str, info: dict[str, Any]) -> None:
        target = target_from(action, info)
        if target is not None:
            open_target(target)

    return on_response


def fresh(orch: Facade, now: float | None = None) -> list[dict[str, Any]]:
    """The unclaimed notices younger than MAX_AGE_SECONDS, oldest first."""
    now = time.time() if now is None else now
    found = []
    for notice in orch.notices(0):
        try:
            created = datetime.fromisoformat(str(notice["created_at"]))
        except ValueError:
            continue
        if created.tzinfo is None:
            created = created.replace(tzinfo=UTC)
        if 0 <= now - created.timestamp() <= MAX_AGE_SECONDS:
            found.append(notice)
    return found


def post_pending(
    orch: Facade,
    center: Center,
    bundle: Path | None,
    clicked: list[str],
    say: Callable[[str], None] = lambda line: None,
) -> str | None:
    """The posting process: claim and post the pending notices (shown with osascript when
    this app may not post), wait for the centre, then return the target of a click that came
    meanwhile (`clicked`, filled by the delegate), else None."""
    answer = wait_for(center, center.status)
    allowed = answer is not None and answer[0] in AUTHORIZED
    if answer is None:  # no answer in time: osascript this once, the grant stays
        say(f"notifications from {config.PROJECT_NAME}: the system did not answer; osascript")
    elif not allowed:
        say(
            f"notifications from {config.PROJECT_NAME}: not allowed (authorization status"
            f" {answer[0]}); osascript"
        )
        desktop.forget_grant(bundle)
    pending: list[int] = []
    finished: list[str | None] = []
    for notice in fresh(orch):
        if not orch.claim_notice(notice["id"]):
            continue
        if allowed:
            pending.append(notice["id"])
            post(center, notice, finished.append)
        else:
            desktop.plain(None, None, str(notice["text"]), notice.get("decision_id"))
    deadline = time.monotonic() + WAIT_SECONDS
    while len(finished) < len(pending) and time.monotonic() < deadline:
        center.spin(0.05)
    if pending:
        center.spin(LINGER_SECONDS)
    return clicked[0] if clicked else None


def notify(
    orch: Facade, bundle: Path | None, say: Callable[[str], None] = lambda line: None
) -> str | None:
    """`orch app --notify`: post the pending notices as this app (see post_pending); the
    target of a click that reached this process, or None. Without the framework: osascript."""
    clicked: list[str] = []
    try:
        center: Center = MacCenter(opener(clicked.append))
    except Exception:
        center = Unavailable()
    return post_pending(orch, center, bundle, clicked, say)


class Unavailable:
    """No notification centre here: nothing is allowed, osascript shows them."""

    def authorize(self, done: Callable[[bool, str | None], None]) -> None:
        done(False, "UserNotifications is not installed")

    def status(self, done: Callable[[int], None]) -> None:
        done(0)

    def post(self, identifier, title, body, info, done) -> None:
        done("UserNotifications is not installed")

    def spin(self, seconds: float) -> None:
        time.sleep(seconds)
