"""Notifications posted by Spartito itself on macOS, through the UserNotifications framework
(pyobjc-framework-UserNotifications, in the extra `app`; imported here and nowhere else), by a
process that is the launcher's app (launcher.py: its interpreter copy, signed with the bundle's
identifier; the notification centre accepts no other), and the notices the native window shows
inside itself when it may not post them:

- the window process (`spartito app`, window.py): sets the centre's delegate before the GUI loop
  runs and from then on, every POLL_SECONDS, keeps its marker fresh (desktop.app_present, so
  osascript and the posting process leave new notices to it) and claims the new notices
  (through the facade, once each, as a page does). It asks for permission once the window is
  shown (`ask`; asked earlier, during the launch, the answer was seen to get lost) and reads
  the authorization status on each poll, so a lost answer, or a change in System Settings, is
  seen within a poll. Allowed, it records the grant (desktop.record_grant) and posts the
  notices; otherwise (not asked yet, not allowed, refused by the system, not available) it
  shows them inside the window (`InWindow`: a banner in the page and the Dock's attention).
  Each change of that state is said (window.say: stderr and the app log) and shown in the page.
  A notice it shows inside stays counted on the Dock's badge until the page has it; one the page
  never took (no hook in the page, `evaluate_js` failing) is said once (PAGE_WAIT_SECONDS). As
  the window closes (`closing`, then `close`) it claims nothing more and hands what it took but
  did not get shown, and what is still unclaimed, to the way that applies without a window
  (desktop.without_window: the posting process, else osascript; claims keep it to once);
- the posting process (`spartito app --notify`, started hidden by desktop.bundle_helper when no
  window runs): claims and posts the pending notices younger than MAX_AGE_SECONDS, then exits;
  when it is not allowed (any more), it forgets the grant and shows them with osascript.

Each notification carries a userInfo {"home": <digest of this home>, "target": "decision:<n>"
or the agent id}. A click on it (the default action) reaches the delegate of the running app,
or of the app the system starts for it (`spartito app` without arguments, the same window code).
A target for this home, checked like a deep link (app_open.check_target), becomes an app-open
request: the window applies it and comes to the front (window.watching_requests). A click
that reaches the posting process is handed to its caller, which opens the window there.
"""

from __future__ import annotations

import contextlib
import threading
import time
from collections import deque
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
IN_WINDOW = 3  # the newest notices the window shows inside itself; older ones drop out
NO_ANSWER_SECONDS = 30.0  # asked this long ago without an answer: said once
SETTINGS = "System Settings › Notifications"
STATUS_WAIT_SECONDS = 10.0  # a status read without a completion this long: read again
PAGE_WAIT_SECONDS = 10.0  # notices the page has not taken for this long: said once
# The states of the window process's notifications (WindowNotifier.state).
WAITING, ASKING, ALLOWED, DENIED, REFUSED, UNAVAILABLE = (
    "waiting",  # not asked yet: the window is not shown yet
    "asking",  # asked, no answer yet
    "allowed",
    "denied",  # not allowed (the prompt's Don't Allow, or switched off)
    "refused",  # the system answered the request with an error
    "unavailable",  # this process cannot post notifications (detail: why)
)


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
    def hand_over_notice(self, notice: dict[str, Any]) -> None: ...


def text_of(notice: dict[str, Any]) -> str:
    return desktop.one_line(str(notice["text"]), TEXT_CHARS)


def post(
    center: Center,
    notice: dict[str, Any],
    done: Callable[[str | None], None],
    fallback: Callable[[dict[str, Any]], None] | None = None,
) -> None:
    """Post a claimed notice; a refused one is shown by `fallback` (default: osascript)."""

    def posted(error: str | None) -> None:
        if error:
            (fallback or osascript)(notice)
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


def osascript(notice: dict[str, Any]) -> None:
    desktop.plain(None, None, str(notice["text"]), notice.get("decision_id"))


def wait_for(center: Center, ask: Callable[[Callable], None], seconds: float = WAIT_SECONDS):
    """Ask the centre (`ask(callback)`) and wait for its answer: the callback's arguments, or
    None when none came in time."""
    answer: list[tuple] = []
    ask(lambda *args: answer.append(args))
    deadline = time.monotonic() + seconds
    while not answer and time.monotonic() < deadline:
        center.spin(0.05)
    return answer[0] if answer else None


class InWindow(Protocol):
    """Where the window process shows what it does not post (window.WindowView, or a fake)."""

    def show(self, view: dict[str, Any]) -> bool: ...  # True: the page has this view now
    def attention(self) -> None: ...  # a new notice inside the window: draw the eye to it
    def hold(self, count: int) -> None: ...  # notices the page has not taken: keep the badge


class Nowhere:
    """No window to show anything in."""

    def show(self, view: dict[str, Any]) -> bool:
        return True

    def attention(self) -> None:
        pass

    def hold(self, count: int) -> None:
        pass


def in_window_item(notice: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": notice["id"],
        "kind": notice.get("kind"),
        "text": text_of(notice),
        "target": desktop.target_of(notice["agent_id"], notice.get("decision_id")),
    }


class WindowNotifier:
    """The window process's part: marker, polling, permission and its state, clicks, and the
    notices shown inside the window. `why_not`: this process cannot post at all (the state is
    then UNAVAILABLE with that reason; `center` is not used)."""

    def __init__(
        self,
        orch: Facade,
        center: Center,
        identifier: str,
        bundle: Path | None,
        say: Callable[[str], None],
        inside: InWindow | None = None,
        why_not: str | None = None,
    ) -> None:
        self.orch, self.center, self.say = orch, center, say
        self.identifier, self.bundle = identifier, bundle
        self.inside: InWindow = inside or Nowhere()
        self.app_name = bundle.stem if bundle is not None else config.PROJECT_NAME
        self.cursor = orch.notice_cursor()  # a window opened now replays nothing older
        self.stop = threading.Event()
        self.thread: threading.Thread | None = None
        self.lock = threading.RLock()
        self.state, self.detail = (UNAVAILABLE, why_not) if why_not else (WAITING, "")
        self.asked_at: float | None = None
        self.no_answer_said = False
        self.checking_since: float | None = None  # a status request is out since then
        self.shown: deque[dict[str, Any]] = deque(maxlen=IN_WINDOW)
        self.unseen: dict[int, dict[str, Any]] = {}  # shown inside, not taken by the page yet
        self.held = 0  # the count the Dock's badge keeps for the page
        self.stuck_since: float | None = None  # the page has not taken new notices since then
        self.stuck_said = False
        self.handed = False  # closed: what comes now goes the way without a window
        self.version = 0  # of what the page should show (state line and notices)
        self.dirty = True  # the page does not show the current version (yet)

    # -- state -------------------------------------------------------------------------

    def line(self) -> str:
        """The state in one line, as said and as the page shows it."""
        name = self.app_name
        inside = "notices are shown in this window"
        if self.state == ALLOWED:
            return f"notifications: {name} shows them; a click opens it there"
        if self.state == WAITING:
            return f"notifications from {name}: not asked yet; {inside}"
        if self.state == ASKING:
            waited = " — no answer yet (look for its prompt in Notification Centre)"
            return (
                f"notifications from {name}: asked macOS for permission"
                f"{waited if self.no_answer_said else ''}; until allowed, {inside}"
            )
        if self.state == DENIED:
            return f"notifications from {name}: not allowed ({SETTINGS} › {name}); {inside}"
        if self.state == REFUSED:
            return f"notifications from {name}: macOS refused the request ({self.detail}); {inside}"
        return f"notifications from {name}: not available ({self.detail}); {inside}"

    def view(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "state": self.state,
            "line": self.line(),
            "notices": list(self.shown),
        }

    def become(self, state: str, detail: str = "") -> None:
        """Move to `state`; a change is said, recorded (grant file) and shown in the page."""
        with self.lock:
            if self.state == UNAVAILABLE or (state, detail) == (self.state, self.detail):
                return
            was = self.state
            self.state, self.detail = state, detail
            self.changed()
        if state == ALLOWED and self.bundle is not None:
            desktop.record_grant(self.identifier, self.bundle)
        elif was == ALLOWED or state in (DENIED, REFUSED):
            desktop.forget_grant(self.bundle)
        self.say(self.line())

    def changed(self) -> None:
        with self.lock:
            self.version += 1
            self.dirty = True

    @property
    def allowed(self) -> bool:
        return self.state == ALLOWED

    # -- the window's life -------------------------------------------------------------

    def start(self) -> None:
        """Take the notices from now on (posted once allowed, else shown in the window)."""
        if self.thread is None:
            self.thread = threading.Thread(target=self.loop, name="orch-app-notices", daemon=True)
            self.thread.start()

    def ask(self) -> None:
        """Ask for permission (the system prompts once per app); called once the window is
        shown. Unavailable, it says why instead."""
        with self.lock:
            if self.state == UNAVAILABLE and self.asked_at is None:
                self.asked_at = time.monotonic()
                self.say(self.line())
            if self.state != WAITING or self.stop.is_set():
                return
            self.asked_at = time.monotonic()
        self.become(ASKING)
        try:
            self.center.authorize(self.authorized)
        except Exception as exc:
            self.authorized(False, str(exc) or type(exc).__name__)

    def authorized(self, granted: bool, error: str | None) -> None:
        """The answer to `ask`."""
        if self.stop.is_set():
            return
        if granted:
            self.become(ALLOWED)
        elif error:
            self.become(REFUSED, error)
        else:
            self.become(DENIED)

    def status(self, code: int) -> None:
        """An authorization status read on a poll: allowed or not, whatever the request's
        answer was (or whether one came). Never raises (it runs in the centre's callback)."""
        self.checking_since = None
        with contextlib.suppress(Exception):
            self.read_status(code)

    def read_status(self, code: int) -> None:
        if self.stop.is_set() or self.state in (WAITING, UNAVAILABLE):
            return
        if self.state == ASKING and code in (1, *AUTHORIZED):
            self.say(
                f"notifications from {self.app_name}: no answer to the request; the system's"
                f" authorization status is {code}"
            )
        if code in AUTHORIZED:
            self.become(ALLOWED)
        elif code == 1 and self.state != REFUSED:
            self.become(DENIED)
        # 0 (not determined): no answer yet, or a status read that crossed the answer

    def check(self) -> None:
        now = time.monotonic()
        if self.state in (WAITING, UNAVAILABLE) or (
            self.checking_since is not None and now - self.checking_since < STATUS_WAIT_SECONDS
        ):
            return  # a read without a completion does not stop the later ones
        if (
            self.state == ASKING
            and not self.no_answer_said
            and self.asked_at is not None
            and time.monotonic() - self.asked_at > NO_ANSWER_SECONDS
        ):
            self.no_answer_said = True
            self.changed()
            self.say(self.line())
        self.checking_since = now
        try:
            self.center.status(self.status)
        except Exception:
            self.checking_since = None

    def loop(self) -> None:
        while not self.stop.is_set():
            with contextlib.suppress(Exception):  # the store was busy: next time
                self.tick()
            self.stop.wait(POLL_SECONDS)

    def tick(self) -> None:
        if self.stop.is_set():
            return  # closing: no marker, no claims
        desktop.app_present()
        self.check()
        for notice in self.orch.notices(self.cursor):
            if self.stop.is_set():
                return
            if self.orch.claim_notice(notice["id"]):
                self.deliver(notice)
            self.cursor = notice["id"]
        self.push()

    def deliver(self, notice: dict[str, Any]) -> None:
        """A claimed notice: posted when allowed, else (or when the post fails) shown in the
        window."""
        if self.handed:
            self.hand_over(notice)
        elif self.allowed:
            post(self.center, notice, lambda error: None, self.show_inside)
        else:
            self.show_inside(notice)

    def show_inside(self, notice: dict[str, Any]) -> None:
        with self.lock:
            late = self.handed
            if not late:
                self.shown.append(in_window_item(notice))
                self.unseen[notice["id"]] = notice
                self.changed()
        if late:  # a post refused after the window closed
            self.hand_over(notice)
            return
        with contextlib.suppress(Exception):
            self.inside.attention()
        self.push()

    def push(self) -> None:
        """Hand the page the current view when it does not have it (a reload: `repush`);
        while it does not take new notices, keep them on the badge and say so once."""
        if self.stop.is_set():
            return  # the window is going: what it did not take is handed over at close
        with self.lock:
            if not self.dirty:
                return
            view = self.view()
        try:
            taken = self.inside.show(view)
        except Exception:
            taken = False
        now = time.monotonic()
        with self.lock:
            if taken and view["version"] == self.version:
                self.dirty = False
            if taken:
                for item in view["notices"]:
                    self.unseen.pop(item["id"], None)
            waiting = self.dirty and bool(self.unseen)
            held = len(self.unseen) if waiting else 0
            if not waiting:
                self.stuck_since = None
            elif self.stuck_since is None:
                self.stuck_since = now
            stuck = waiting and not self.stuck_said and now - self.stuck_since >= PAGE_WAIT_SECONDS
            back = not waiting and self.stuck_said
            self.stuck_said = (self.stuck_said or stuck) and not back
        if held != self.held:
            self.held = held
            with contextlib.suppress(Exception):
                self.inside.hold(held)
        if stuck:
            self.say(
                f"notifications: the window's page did not take {held} notice(s) (no hook in"
                " the page, or it did not answer); the Dock's badge keeps them, and macOS shows"
                " them when the window closes"
            )
        elif back:
            self.say("notifications: the window's page shows the notices again")

    def repush(self) -> None:
        """The page was (re)loaded: it shows nothing of this yet."""
        with self.lock:
            self.dirty = True

    def closing(self) -> None:
        """The window is going (closed, a stop signal, time up): claim and push nothing more."""
        self.stop.set()

    def close(self) -> None:
        """After the GUI loop: stop, remove the marker, then hand over what the window took
        but did not get shown and what came after its last poll (`desktop.show` left those
        to the window while its marker was fresh)."""
        self.closing()
        if self.thread is not None:
            self.thread.join(POLL_SECONDS + 1)
        desktop.app_marker().unlink(missing_ok=True)
        with self.lock:
            self.handed = True
            left = list(self.unseen.values())
            self.unseen.clear()
        try:
            last = self.orch.notice_cursor()  # recorded later: desktop.show's own
            left += [n for n in self.orch.notices(self.cursor) if n["id"] <= last]
        except Exception:
            pass
        for notice in left:
            self.hand_over(notice)
        if left:
            self.say(
                f"notifications: the window closed with {len(left)} notice(s) it had not"
                " shown; handed to macOS"
            )

    def hand_over(self, notice: dict[str, Any]) -> None:
        with contextlib.suppress(Exception):
            self.orch.hand_over_notice(notice)


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
            osascript(notice)
    deadline = time.monotonic() + WAIT_SECONDS
    while len(finished) < len(pending) and time.monotonic() < deadline:
        center.spin(0.05)
    if pending:
        center.spin(LINGER_SECONDS)
    return clicked[0] if clicked else None


def notify(
    orch: Facade, bundle: Path | None, say: Callable[[str], None] = lambda line: None
) -> str | None:
    """`spartito app --notify`: post the pending notices as this app (see post_pending); the
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
