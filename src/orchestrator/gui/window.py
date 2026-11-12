"""`orch app`: the dashboard in its own window, with the server it needs.

The server: this home's running one when `locate` finds it (never stopped here), otherwise
one of its own on 127.0.0.1 in a thread, stopped when the window is gone.

The window, by the first way that works; each step that fails says so in one line on stderr:
  a. a native window with pywebview (the optional extra `orchestrator[app]`): Cocoa on macOS,
     Qt on Linux, where the backend is first started in a child process (Qt aborts the whole
     process, no exception, when it cannot load its platform plugin);
  b. a Chromium-family browser in app mode, with its own profile under `run/` and its DevTools
     endpoint on a free loopback port: the window counts as closed when no page is left in it
     (macOS browsers keep running without windows), and the browser is then ended. A browser
     that exits before its window was seen (a crash, a refused profile) did not start;
  c. the default browser; the own server then runs in the foreground until Ctrl-C.
On Linux without a display none of them is tried: the URL is printed and served until Ctrl-C.

One `orch app` per home: a second one asks the open window to come to the front, and to show
its target (`--agent`, `--decision`), through app_open.py, and leaves; one that only serves
(c, or no display) gives that up, so the next one reuses its server. SIGTERM and SIGHUP end
it as closing the window does.

Identity: on macOS with the launcher of this home installed (launcher.py), an `orch app`
started from a terminal runs on as that app (exec of the launcher's interpreter copy, or with
CFProcessPath without a current one; same pid, terminal and exit code), so the Dock shows its
name and icon; without the launcher the Dock icon is set at run time. Run through the copy,
the native window also posts the notifications itself (notifier.py), and `--notify` only
posts the pending ones. On Linux the Qt window carries the icon, application name and desktop
file name.

Notices (macOS): the native window takes the notices while it is open, whether or not it may
post them; those it may not post it shows inside itself (WindowView: a banner in the page,
the Dock's bounce and badge), so osascript, whose click opens Script Editor, stays quiet while
a window is open. What the window process says (`say`) goes to stderr and to the home's
`logs/app.log` (APP_LOG, emptied at start when above APP_LOG_LIMIT), where it can be read
when the window was started from the Dock or Finder.

Own server: on the port of the last one (`run/app-port`) when it is free, so the page keeps
its origin (preferences, notification permission); otherwise on a free one, then remembered.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.parse
import webbrowser
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import IO, Protocol

from .. import app_open, autostart, config, desktop
from ..web import serve
from . import launcher, locate, notifier

HOST = "127.0.0.1"
STARTUP_TIMEOUT = 15.0  # seconds for the own server to listen
STOP_TIMEOUT = 10.0  # seconds for the own server, or the browser, to finish
WINDOW_SIZE = (1280, 860)
INSTALL_HINT = "install the extra orchestrator[app]"
LAUNCHER_HINT = "then run `orch app install`"
DOCS_HINT = "see docs/desktop.md"
ALREADY_OPEN = "the dashboard window of this home is already open"
ANSWER_TIMEOUT = 5.0  # seconds a second `orch app` waits for the open window's answer
WATCH_EVERY = 0.3  # seconds between two looks at the request file
ACTIVE_WAIT = 1.0  # seconds for the native window to become the active one (macOS)
PORT_FILE = "app-port"  # in config.runtime_dir(): the port of the last own server
CONNECT_TIMEOUT = 0.5  # seconds for the look whether something listens on that port
CHROMIUM_NAMES = "Chrome, Chromium, Edge or Brave"
MAC_BROWSERS = ("Google Chrome", "Chromium", "Microsoft Edge", "Brave Browser")  # app bundles
# Starts pywebview's GUI backend (argv[1]: the one to force, or "" for its own choice) and
# leaves: exit 0 when it can start.
BACKEND_PROBE = "import sys; from webview.guilib import initialize; initialize(sys.argv[1] or None)"
PROBE_TIMEOUT = 60.0  # seconds; the first import of Qt WebEngine can be slow
ERROR_LINE = re.compile(r"[\w.]*(Error|Exception): ")
POLL = 1.0  # seconds between two looks at the browser's open pages
EARLY_EXIT = 3.0  # seconds: a browser that ends this soon without a window did not start
STOP_SIGNALS = (signal.SIGTERM, signal.SIGHUP)
APP_LOG = "app.log"  # in config.logs_dir(): what the window process says
APP_LOG_LIMIT = 1024 * 1024  # bytes; emptied at the next start when above
ASK_WAIT = 20.0  # seconds for the native window to be shown before permission is asked anyway
LINUX_BROWSERS = (
    "google-chrome",
    "google-chrome-stable",
    "chromium",
    "chromium-browser",
    "microsoft-edge",
    "brave-browser",
)


class AppError(Exception):
    """A clear one-line reason `orch app` cannot run."""


def say(line: str) -> None:
    """One line about this window: on stderr and in the app log."""
    print(line, file=sys.stderr, flush=True)
    log_line(line)


def log_line(line: str) -> None:
    """One line in the app log only (what happened, for a look afterwards)."""
    with contextlib.suppress(OSError), app_log().open("a", encoding="utf-8") as out:
        stamp = datetime.now().astimezone().isoformat(timespec="seconds")
        out.write(f"{stamp} [{os.getpid()}] {line}\n")


def app_log() -> Path:
    return config.logs_dir() / APP_LOG


def cut_app_log() -> None:
    with contextlib.suppress(OSError):
        autostart.cut_log(app_log(), APP_LOG_LIMIT)


def this_platform() -> str:
    return sys.platform


def mac_app_dirs() -> list[Path]:
    return [Path("/Applications"), Path.home() / "Applications"]


def find_chromium() -> Path | None:
    """The first Chromium-family browser installed, by MAC_BROWSERS or LINUX_BROWSERS order."""
    if this_platform() == "darwin":
        for name in MAC_BROWSERS:
            for root in mac_app_dirs():
                path = root / f"{name}.app" / "Contents" / "MacOS" / name
                if path.is_file() and os.access(path, os.X_OK):
                    return path
        return None
    for name in LINUX_BROWSERS:
        if found := shutil.which(name):
            return Path(found)
    return None


def no_display() -> bool:
    return this_platform().startswith("linux") and not (
        os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")
    )


class OwnServer:
    """The dashboard server of this `orch app`: 127.0.0.1 only, in a thread."""

    def __init__(self, port: int = 0) -> None:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)  # as uvicorn binds
        try:
            sock.bind((HOST, port))
        except OSError as exc:
            sock.close()
            raise AppError(f"cannot listen on {HOST}:{port}: {exc.strerror or exc}") from None
        self.sock = sock
        self.port: int = sock.getsockname()[1]
        self.url = locate.url_of(HOST, self.port)
        self.server = serve.make(HOST, self.port)
        self.thread = threading.Thread(
            target=serve.serve, args=(self.server, sock), name="orch-app-server", daemon=True
        )

    def start(self) -> None:
        self.thread.start()
        deadline = time.monotonic() + STARTUP_TIMEOUT
        while not self.server.ready:
            if not self.thread.is_alive() or time.monotonic() > deadline:
                self.stop()
                raise AppError("the dashboard server did not start")
            time.sleep(0.02)

    def stop(self) -> None:
        self.server.should_exit = True
        if self.thread.is_alive():
            self.thread.join(STOP_TIMEOUT)
        self.sock.close()


@dataclass
class Session:
    """One `orch app`: its lock, its server and how it ends."""

    lock: IO[str]
    close_after: float | None = None
    url: str = ""
    own: OwnServer | None = None
    target: str = ""  # "decision:<n>", an agent id or "": where the window opens
    baseline: int = 0  # requests up to this id were there before the window
    launcher_installed: bool = False  # this home's launcher (launcher.py) is there
    bundled: bool = False  # this process counts as the launcher's app (macOS)
    own_bundle: Path | None = None  # ... through the bundle's interpreter copy: this bundle
    echo_location: bool = False  # say where the native window's page is (for checks)
    done: threading.Event = field(default_factory=threading.Event)  # end now
    signal: int | None = None  # the SIGTERM or SIGHUP that ended it

    def window_url(self) -> str:
        """The page in the window: marked as the window's (`?app=<baseline>`, see app_open.py),
        at its target."""
        return f"{self.url}?app={self.baseline}{fragment(self.target)}"

    def elapsed(self, since: float) -> bool:
        return self.close_after is not None and time.monotonic() - since >= self.close_after

    def hold(self) -> None:
        """Serve in the foreground until Ctrl-C, a stop signal or `close_after`. Only serving,
        it gives up the lock: another `orch app` reuses this server."""
        self.lock.close()
        since = time.monotonic()
        with contextlib.suppress(KeyboardInterrupt):
            while self.own and self.own.thread.is_alive() and not self.elapsed(since):
                if self.done.wait(0.2):
                    break


def fragment(target: str) -> str:
    return f"#{urllib.parse.quote(target, safe=':')}" if target else ""


@contextlib.contextmanager
def watching_requests(session: Session, bring_forward: Callable[[], tuple[bool, str]]):
    """While the window is open (this block), answer each new request of a second `orch app`
    (app_open.py) by bringing the window forward; `bring_forward` returns whether that worked
    and what was done, or why not."""
    stop = threading.Event()

    def loop() -> None:
        seen = session.baseline
        while not stop.wait(WATCH_EVERY) and not session.done.is_set():
            request = app_open.latest()
            if request is None or request["id"] <= seen:
                continue
            seen = request["id"]
            try:
                raised, detail = bring_forward()
            except Exception as exc:
                raised, detail = False, first_line(exc)
            app_open.answer(seen, raised, detail)

    thread = threading.Thread(target=loop, name="orch-app-requests", daemon=True)
    thread.start()
    try:
        yield
    finally:
        stop.set()
        thread.join(STOP_TIMEOUT)


class Stoppable(Protocol):
    done: threading.Event
    signal: int | None


@contextlib.contextmanager
def stopped_by_signals(session: Stoppable, signals: tuple[int, ...] = STOP_SIGNALS):
    """`signals` (SIGTERM and SIGHUP) set `session.done` and `session.signal`. Their Python
    handler does nothing: a thread reads the signal numbers from the wakeup fd, which the
    C-level handler writes even while a GUI loop (Cocoa) keeps the main thread from running
    Python handlers."""
    read_fd, write_fd = os.pipe()
    os.set_blocking(write_fd, False)
    previous = {sig: signal.signal(sig, lambda *_: None) for sig in signals}
    previous_fd = signal.set_wakeup_fd(write_fd, warn_on_full_buffer=False)

    def watch() -> None:
        while data := os.read(read_fd, 64):
            for number in data:
                if number in signals:
                    session.signal = number
                    session.done.set()

    watcher = threading.Thread(target=watch, name="orch-app-signals", daemon=True)
    watcher.start()
    try:
        yield
    finally:
        signal.set_wakeup_fd(previous_fd)
        for sig, handler in previous.items():
            signal.signal(sig, handler)
        os.close(write_fd)
        watcher.join(1.0)
        os.close(read_fd)


def first_line(exc: BaseException) -> str:
    text = str(exc).strip().splitlines()
    return (text[0] if text else type(exc).__name__)[:200]


def linux_gui() -> str | None:
    """The backend pywebview is told to use on Linux: Qt, which the extra installs (it would
    try GTK first and log its import error), unless PYWEBVIEW_GUI chooses."""
    if not this_platform().startswith("linux") or os.environ.get("PYWEBVIEW_GUI"):
        return None
    return "qt"


def backend_problem(gui: str | None) -> str | None:
    """Why pywebview's GUI backend cannot start here, found by starting it in a child
    process; None when it can."""
    argv = [sys.executable, "-c", BACKEND_PROBE, gui or ""]
    try:
        done = subprocess.run(
            argv, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=PROBE_TIMEOUT
        )
    except subprocess.TimeoutExpired:
        return f"its GUI backend did not start within {PROBE_TIMEOUT:.0f} s"
    except OSError as exc:
        return str(exc.strerror or exc)
    if done.returncode == 0:
        return None
    lines = [line.strip() for line in done.stderr.splitlines() if line.strip()]
    errors = [line for line in lines if ERROR_LINE.match(line)]
    plain = [line for line in lines if not line.startswith(("[pywebview]", "Traceback", "File "))]
    found = errors or plain
    return found[0][:200] if found else f"its GUI backend exited with code {done.returncode}"


def native_window(session: Session) -> bool:
    """a. Show `url` in a pywebview window until it is closed; False when that cannot be."""
    try:
        import webview  # the optional extra: imported here and nowhere else
    except ImportError:
        then = "" if session.launcher_installed else f", {LAUNCHER_HINT}"
        say(
            f"no native window: pywebview is not installed ({INSTALL_HINT}{then}; {DOCS_HINT});"
            " trying a Chromium-family browser in app mode"
        )
        return False
    gui = linux_gui()
    if this_platform().startswith("linux") and (problem := backend_problem(gui)):
        say(
            f"no native window: pywebview cannot start its GUI backend ({problem});"
            " trying a Chromium-family browser in app mode"
        )
        return False
    closed, timed_out = threading.Event(), threading.Event()
    icon = native_identity(session, gui)
    try:
        window = webview.create_window(
            config.PROJECT_NAME,
            session.window_url(),
            width=WINDOW_SIZE[0],
            height=WINDOW_SIZE[1],
            text_select=True,
            zoomable=True,
        )

        # The centre's delegate is set before the GUI loop runs: a click that started this
        # app reaches it. The notices are taken from now on; permission is asked once the
        # window is shown (`ask_when_shown`).
        notices = start_notifier(session, window)

        def on_closed() -> None:  # one handler: pywebview runs each in a thread of its own
            if notices is not None:
                notices.closing()  # the page is gone: nothing more is claimed for it
            closed.set()
            session.done.set()

        window.events.closed += on_closed

        def close_later() -> None:  # runs in pywebview's own thread, once the GUI runs
            if this_platform() == "darwin" and not session.bundled:
                say(
                    "the Dock shows this window as Python: `orch app install` gives it"
                    f" {config.PROJECT_NAME}'s name and icon"
                )
            if session.echo_location:
                echo_locations(session, window)
            if notices is not None:
                threading.Thread(
                    target=ask_when_shown, args=(window, notices), name="orch-app-ask", daemon=True
                ).start()
            with watching_requests(session, lambda: bring_native_forward(window)):
                if not session.done.wait(session.close_after):  # closed, a stop signal
                    timed_out.set()  # or time is up
            if notices is not None:
                notices.closing()
            if not closed.is_set():
                window.destroy()

        try:
            # The GUI loop runs on this (the main) thread until the window is gone. Not
            # private, so the page keeps its preferences (theme) per origin: in storage_path
            # with Qt/GTK; macOS WebKit ignores it and keeps the app's data in ~/Library.
            webview.start(
                close_later,
                gui=gui,
                private_mode=False,
                storage_path=str(config.runtime_dir() / "app-webview"),
                **({"icon": str(icon)} if icon else {}),
            )
        finally:
            if notices is not None:
                notices.close()
            log_line(f"window ended: {end_reason(session, closed, timed_out)}")
    except Exception as exc:  # no GUI toolkit (Linux without Qt or GTK), and the like
        say(
            f"no native window: pywebview failed ({first_line(exc)});"
            " trying a Chromium-family browser in app mode"
        )
        return False
    return True


def end_reason(session: Session, closed: threading.Event, timed_out: threading.Event) -> str:
    if session.signal:
        return f"stop signal {signal.Signals(session.signal).name}"
    if timed_out.is_set():
        return "--close-after elapsed"
    if closed.is_set():
        return "the window was closed (its close button, Cmd-Q or Quit)"
    return "the GUI loop ended"


def ask_when_shown(window, notices: notifier.WindowNotifier) -> None:
    """Ask for permission once the window is shown (the app launched and in front), not during
    the launch: a request made then was seen to get no answer."""
    shown = getattr(getattr(window, "events", None), "shown", None)
    if shown is not None:
        shown.wait(ASK_WAIT)
    notices.ask()


def not_posting(session: Session) -> str | None:
    """Why this window process cannot post notifications as the launcher's app (macOS);
    None when it can try."""
    if session.own_bundle is not None:
        if notifier.available():
            return None
        return f"UserNotifications is not installed ({INSTALL_HINT})"
    if session.bundled:
        return "started without the launcher's copy of Python: run `orch app install` again"
    if session.launcher_installed:
        return "not started as the launcher's app"
    return "no launcher: run `orch app install`"


def start_notifier(session: Session, window=None) -> notifier.WindowNotifier | None:
    """macOS: the window process takes the notices (notifier.py): posted as the launcher's app
    when it may (run through the bundle's interpreter copy, with the framework, allowed), else
    shown inside `window`. None elsewhere."""
    if this_platform() != "darwin":
        return None
    from ..service import Orchestrator

    why = not_posting(session)
    center: notifier.Center = notifier.Unavailable()
    if why is None:
        try:
            center = notifier.MacCenter(notifier.opener(clicked_open))
        except Exception as exc:
            why = first_line(exc)
    bundle = session.own_bundle
    try:
        notices = notifier.WindowNotifier(
            Orchestrator(),
            center,
            launcher.bundle_identifier(bundle) if bundle is not None else "",
            bundle,
            say,
            WindowView(window, MacDock.make()) if window is not None else None,
            why,
        )
        if window is not None:
            with contextlib.suppress(Exception):
                window.events.loaded += notices.repush  # a (re)loaded page shows nothing yet
        notices.start()
    except Exception as exc:
        say(f"notifications from {config.PROJECT_NAME}: not available ({first_line(exc)})")
        return None
    return notices


def clicked_open(target: str) -> None:
    """A click on one of this app's notifications reached this process: open its target."""
    from ..service import Orchestrator

    log_line(f"a click on a notification: opening {target or 'the window'}")
    Orchestrator.request_app_open(target)


class Dock(Protocol):
    def attention(self) -> None: ...
    def hold(self, count: int) -> None: ...


class MacDock:
    """The Dock's part for notices shown inside the window (AppKit, on the main thread): one
    bounce and a badge counting them while the app is not the active one; the badge goes when
    it becomes active (the window focused, a banner used), except for the notices the page has
    not taken (`hold`), which it keeps counting."""

    def __init__(self) -> None:
        import AppKit  # pyobjc, which pywebview's Cocoa backend brings
        import Foundation

        self.AppKit, self.Foundation = AppKit, Foundation
        self.unseen = 0
        self.held = 0
        self.observer = Foundation.NSNotificationCenter.defaultCenter().addObserverForName_object_queue_usingBlock_(
            AppKit.NSApplicationDidBecomeActiveNotification,
            None,
            Foundation.NSOperationQueue.mainQueue(),
            lambda note: self.clear(),
        )

    @classmethod
    def make(cls) -> Dock | None:
        try:
            return cls()
        except Exception:
            return None

    def on_main(self, work: Callable[[], None]) -> None:
        self.Foundation.NSOperationQueue.mainQueue().addOperationWithBlock_(work)

    def attention(self) -> None:
        def draw() -> None:
            app = self.AppKit.NSApplication.sharedApplication()
            if app.isActive():
                return
            self.unseen += 1
            self.badge()
            app.requestUserAttention_(self.AppKit.NSInformationalRequest)

        self.on_main(draw)

    def hold(self, count: int) -> None:
        def keep() -> None:
            self.held = count
            self.badge()

        self.on_main(keep)

    def clear(self) -> None:
        self.unseen = 0
        self.badge()

    def badge(self) -> None:
        count = max(self.unseen, self.held)
        tile = self.AppKit.NSApplication.sharedApplication().dockTile()
        tile.setBadgeLabel_(str(count) if count else None)


# The page's hook for the window process (static/app.js: spartitoApp.sync): the view's version
# it shows now, -1 before the page has the hook.
SYNC_JS = "window.spartitoApp ? window.spartitoApp.sync({}) : -1"


class WindowView:
    """notifier.InWindow for the native window: the notices and the notification state in the
    page (handed over with evaluate_js once the page is ready), the Dock's attention."""

    def __init__(self, window, dock: Dock | None) -> None:
        self.window, self.dock = window, dock

    def ready(self) -> bool:
        events = self.window.events
        for name in ("loaded", "_pywebviewready"):
            event = getattr(events, name, None)
            if event is not None and not event.is_set():
                return False
        return True

    def show(self, view: dict) -> bool:
        if not self.ready():
            return False  # evaluate_js would wait for the page; the next poll tries again
        return self.window.evaluate_js(SYNC_JS.format(json.dumps(view))) == view["version"]

    def attention(self) -> None:
        if self.dock is not None:
            self.dock.attention()

    def hold(self, count: int) -> None:
        if self.dock is not None:
            self.dock.hold(count)


def notify_only() -> str | None:
    """`orch app --notify`: post the pending notices as the launcher's app and leave; the
    target of a click that came meanwhile (the caller then opens the window there)."""
    if this_platform() != "darwin":
        return None
    from ..service import Orchestrator

    _, bundle = take_bundle_identity()
    target = notifier.notify(Orchestrator(), bundle, say)
    if target and bundle is not None:
        os.environ[launcher.BUNDLE_VAR] = str(bundle)  # the window that follows is this app
    return target


def native_identity(session: Session, gui: str | None) -> Path | None:
    """Name and icon of the native window; returns the icon file pywebview should set (macOS:
    the Dock icon, without the launcher; Linux: the window icon)."""
    if this_platform() == "darwin":
        return None if session.bundled else launcher.ICNS  # else the bundle gives both
    if gui == "qt":
        name_qt_app()
    return launcher.LINUX_ICONS["512x512"]


def name_qt_app() -> None:
    """The Qt application's name (the class of its X11 WM_CLASS, matched by the launcher's
    StartupWMClass) and desktop file name (its Wayland app id), set before pywebview makes the
    QApplication."""
    try:
        from qtpy.QtGui import QGuiApplication  # with the extra on Linux; here and nowhere else
    except Exception:
        return
    QGuiApplication.setApplicationName(launcher.WM_CLASS)
    QGuiApplication.setDesktopFileName(launcher.slug(config.PROJECT_NAME))


def bring_native_forward(window) -> tuple[bool, str]:
    """Un-minimize the pywebview window and bring it to the front (both backends do this on
    their GUI thread); on macOS, checked: the application must become the active one."""
    window.restore()
    window.show()
    deadline = time.monotonic() + ACTIVE_WAIT
    while this_platform() == "darwin" and native_active() is False:
        if time.monotonic() > deadline:
            return False, "macOS kept another application in front"
        time.sleep(0.05)
    return True, "brought it to the front"


def native_active() -> bool | None:
    """macOS: whether this application is the active one; None when that cannot be known."""
    try:
        import AppKit  # pyobjc, which pywebview's Cocoa backend brings
    except ImportError:
        return None
    return bool(AppKit.NSApplication.sharedApplication().isActive())


# `--echo-location`: the page's address, the decision its decisions window shows, and how many
# notice banners it shows (the notices the window shows itself).
WHERE_JS = (
    "location.href + ' decision: ' +"
    " (document.querySelector('#decisionsDialog[open] [data-key].marked')?.dataset.key || '-')"
    " + ' banners: ' + document.querySelectorAll('#appNotices .app-notice').length"
)


def echo_locations(session: Session, window) -> None:
    """`--echo-location` (for checks by hand): say as which app this process runs (macOS),
    then each new place of the native window's page."""
    if this_platform() == "darwin":
        say(f"identity: {mac_identity()}")

    def loop() -> None:
        last = None
        while not session.done.wait(0.5):
            try:
                where = window.evaluate_js(WHERE_JS)
            except Exception:
                continue
            if where != last:
                last = where
                say(f"location: {where}")

    threading.Thread(target=loop, name="orch-app-location", daemon=True).start()


def mac_identity() -> str:
    """This process's main bundle and name as macOS sees them from inside."""
    try:
        import AppKit  # pyobjc, which pywebview's Cocoa backend brings
        import Foundation
    except ImportError:
        return "unknown (no pyobjc)"
    bundle = Foundation.NSBundle.mainBundle()
    running = AppKit.NSRunningApplication.currentApplication()
    left = "CFProcessPath" in os.environ
    return (
        f"main bundle {bundle.bundleIdentifier()} at {bundle.bundlePath()},"
        f" running application {running.localizedName()!r},"
        f" CFProcessPath {'still set' if left else 'not handed on'}"
    )


def devtools_port(profile: Path) -> int:
    return int((profile / "DevToolsActivePort").read_text().split()[0])


def bring_chromium_forward(profile: Path) -> tuple[bool, str]:
    """Ask the browser of `orch app` (its DevTools endpoint) to activate its page, which
    brings its window forward as far as the browser does (not checked)."""
    try:
        port = devtools_port(profile)
        body = autostart.get_local(f"http://127.0.0.1:{port}/json/list", 2.0, limit=1 << 22)
        page = next(t for t in json.loads(body) if t.get("type") == "page")
        target = urllib.parse.quote(str(page["id"]), safe="")
        autostart.get_local(f"http://127.0.0.1:{port}/json/activate/{target}", 2.0)
    except StopIteration:
        return False, "the browser shows no page"
    except (OSError, ValueError, IndexError, KeyError, TypeError, AttributeError) as exc:
        return False, f"its DevTools endpoint did not do it: {first_line(exc)}"
    return True, "asked its browser to bring it to the front"


def open_pages(profile: Path) -> int | None:
    """How many pages the browser using `profile` shows, asked at its DevTools endpoint (the
    port it wrote to DevToolsActivePort); None when that cannot be known (yet)."""
    try:
        port = devtools_port(profile)
        body = autostart.get_local(f"http://127.0.0.1:{port}/json/list", 2.0, limit=1 << 22)
        return sum(1 for target in json.loads(body) if target.get("type") == "page")
    except OSError, ValueError, IndexError, TypeError, AttributeError:
        return None


def app_mode(session: Session) -> bool:
    """b. Show the page in a Chromium-family browser in app mode until its window is closed;
    False when there is none or it did not start."""
    browser = find_chromium()
    then = default_browser_note(session)
    if browser is None:
        say(f"no Chromium-family browser found ({CHROMIUM_NAMES}); {then}")
        return False
    profile = config.runtime_dir() / "app-profile"  # its own: the process is this window's
    (profile / "DevToolsActivePort").unlink(missing_ok=True)
    argv = [
        str(browser),
        f"--app={session.window_url()}",
        f"--user-data-dir={profile}",
        "--remote-debugging-port=0",  # loopback; honoured only with an own --user-data-dir
        "--no-first-run",
        "--no-default-browser-check",
    ]
    try:
        proc = subprocess.Popen(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,  # Ctrl-C reaches orch app, which ends the browser
        )
    except OSError as exc:
        say(f"{browser.name} did not start ({exc.strerror or exc}); {then}")
        return False
    started, seen = time.monotonic(), False
    try:
        with watching_requests(session, lambda: bring_chromium_forward(profile)):
            while (code := proc.poll()) is None:
                pages = open_pages(profile)  # None: no endpoint, the process alone tells
                if pages == 0 and seen or session.elapsed(started):
                    break  # the window was closed (the browser may stay without one)
                seen = seen or bool(pages)
                if session.done.wait(POLL if seen else 0.2):
                    break
            else:
                if not seen and time.monotonic() - started < EARLY_EXIT:
                    say(f"{browser.name} did not start (it exited at once, code {code}); {then}")
                    return False
    except KeyboardInterrupt:
        pass
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(STOP_TIMEOUT)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
    return True


def default_browser_note(session: Session) -> str:
    serving = f"; serving {session.url} until Ctrl-C" if session.own else ""
    return f"opening the default browser{serving}"


def default_browser(session: Session) -> None:
    """c. Open the page in the default browser; keep the own server until Ctrl-C."""
    try:
        opened = webbrowser.open(session.url + fragment(session.target))
    except webbrowser.Error:
        opened = False
    if not opened:
        serving = " (serving until Ctrl-C)" if session.own else ""
        say(f"no browser could be opened: open {session.url}{fragment(session.target)}{serving}")
    session.hold()


def show(session: Session) -> None:
    if no_display():
        serving = ", serving it until Ctrl-C" if session.own else ""
        say(
            "no display (DISPLAY and WAYLAND_DISPLAY are unset):"
            f" open {session.url}{fragment(session.target)} in a browser{serving}"
        )
        session.hold()
        return
    if native_window(session) or app_mode(session):
        return
    default_browser(session)


def single_instance(name: str = "app.lock") -> IO[str] | None:
    """The lock `name` of this home (`orch app`'s by default), held while the file stays open;
    None when another process holds it."""
    handle = (config.runtime_dir() / name).open("a")
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        handle.close()
        return None
    return handle


def forward(target: str) -> int:
    """A window of this home is open: ask it to come to the front (and show `target`), say in
    one line how that went; exit code 0."""
    from ..service import Orchestrator

    number = Orchestrator.request_app_open(target)
    deadline = time.monotonic() + ANSWER_TIMEOUT
    while (reply := app_open.answered(number)) is None and time.monotonic() < deadline:
        time.sleep(0.05)
    if reply is None:
        reply = {"raised": False, "detail": "the window did not answer"}
    if reply["raised"]:
        showing = f", showing {target}" if target else ""
        print(f"{ALREADY_OPEN}: {reply['detail'] or 'brought it to the front'}{showing}")
    else:
        asked = f"; asked it to show {target}" if target else ""
        print(
            f"{ALREADY_OPEN}, but it could not be brought to the front ({reply['detail']}){asked}"
        )
    return 0


def take_bundle_identity() -> tuple[bool, Path | None]:
    """Whether this process was started as the launcher's app (macOS: CFProcessPath, or the
    bundle's interpreter copy, whose script sets launcher.BUNDLE_VAR), and the bundle in the
    second case. Makes CoreFoundation read the main bundle now, then drops both variables, so
    the processes this one starts (agents, browsers) do not take this app's identity."""
    own = os.environ.get(launcher.BUNDLE_VAR)
    if this_platform() != "darwin" or not ("CFProcessPath" in os.environ or own):
        return False, None
    read_main_bundle()
    os.environ.pop("CFProcessPath", None)
    os.environ.pop(launcher.BUNDLE_VAR, None)
    return True, Path(own) if own else None


def read_main_bundle() -> None:
    """Let CoreFoundation find the main bundle now (it reads CFProcessPath once and keeps
    it)."""
    with contextlib.suppress(ImportError):
        import Foundation  # pyobjc, with the extra; here and nowhere else

        Foundation.NSBundle.mainBundle()


def stale_copy(item: launcher.Launcher, failed: bool) -> None:
    """Running as the launcher's app the earlier way (CFProcessPath) although it has, or had,
    an interpreter copy: say why, once, on stderr."""
    if not item.record.exists():
        return
    problem = desktop.interpreter_problem(item.path) or ("it does not start" if failed else None)
    if problem:
        say(
            f"the launcher's copy of Python is stale ({problem}): run `orch app install`"
            f" again; until then {config.PROJECT_NAME} posts no notifications itself"
        )


def execute(executable: str, argv: list[str], env: dict[str, str]) -> None:
    os.execve(executable, argv, env)


def app_argv(port: int, close_after: float | None, target: str, echo_location: bool) -> list[str]:
    """`<python> -m orchestrator app` with these options again."""
    argv = [sys.executable, "-m", "orchestrator", "app"]
    if port:
        argv += ["--port", str(port)]
    if close_after is not None:
        argv += ["--close-after", repr(close_after)]
    if target.startswith("decision:"):
        argv += ["--decision", target.removeprefix("decision:")]
    elif target:
        argv += ["--agent", target]
    if echo_location:
        argv.append("--echo-location")
    return argv


def native_possible() -> bool:
    """pywebview is installed (it is not imported for this)."""
    import importlib.util

    try:
        return importlib.util.find_spec("webview") is not None
    except ImportError, ValueError:
        return False


def remembered_port() -> int:
    try:
        port = int((config.runtime_dir() / PORT_FILE).read_text().strip())
    except OSError, ValueError:
        return 0
    return port if 0 < port < 65536 else 0


def listened_on(port: int) -> bool:
    """Whether a program accepts connections on `port` of the loopback addresses (127.0.0.1
    or ::1), also through a wildcard address. OwnServer's bind (SO_REUSEADDR, as uvicorn's)
    would succeed beside a program listening on 0.0.0.0 or :: on macOS, and take its
    loopback traffic."""
    for family, host in ((socket.AF_INET, "127.0.0.1"), (socket.AF_INET6, "::1")):
        try:
            with socket.socket(family, socket.SOCK_STREAM) as probe:
                probe.settimeout(CONNECT_TIMEOUT)
                if probe.connect_ex((host, port)) == 0:
                    return True
        except OSError:  # no IPv6 here, and the like
            continue
    return False


def own_server(port: int) -> OwnServer:
    """The own server: on `port` when given; else on the remembered one when nothing
    listens there, or a free one, which is then remembered."""
    if port:
        return OwnServer(port)
    server = None
    if (remembered := remembered_port()) and not listened_on(remembered):
        with contextlib.suppress(AppError):
            server = OwnServer(remembered)
    server = server or OwnServer(0)
    path = config.runtime_dir() / PORT_FILE
    temp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    temp.write_text(f"{server.port}\n")
    temp.replace(path)
    return server


def run(
    port: int = 0,
    close_after: float | None = None,
    target: str = "",
    item: launcher.Launcher | None = None,
    echo_location: bool = False,
) -> int:
    """`orch app`: open the dashboard window (at `target`) and return once it is gone, with
    the exit code (128 + the signal when SIGTERM or SIGHUP ended it); with a window of this
    home open already, bring that forward instead. `item`: the launcher to look for (default:
    this home's). AppError when no server can be had."""
    app_open.check_target(target)
    bundled, own_bundle = take_bundle_identity()
    lock = single_instance()
    if lock is None:
        return forward(target)
    installed = False
    if item is None:
        with contextlib.suppress(launcher.LauncherError):
            item = launcher.launcher()
    if item is not None:
        installed = launcher.installed(item)
    darwin = this_platform() == "darwin" and item is not None and item.kind == "mac"
    if darwin and installed and not bundled and native_possible():
        # Run on as the launcher's app: same process, terminal and exit code.
        lock.close()
        argv = app_argv(port, close_after, target, echo_location)
        copy = launcher.interpreter(item)
        if copy is not None:
            env = {
                **os.environ,
                launcher.BUNDLE_VAR: str(item.path),
                "__PYVENV_LAUNCHER__": sys.executable,
            }
            execute(str(copy), [str(copy), *argv[1:]], env)
        env = {**os.environ, "CFProcessPath": str(item.executable)}
        execute(sys.executable, argv, env)  # says why there is no copy (stale_copy)
    failed = os.environ.pop(launcher.COPY_FAILED_VAR, None) is not None
    cut_app_log()  # this process holds the window lock: the log is its own now
    if darwin and installed and bundled and own_bundle is None:
        stale_copy(item, failed)
    session = Session(lock, close_after, target=target, echo_location=echo_location)
    session.launcher_installed, session.bundled = installed, bundled
    session.own_bundle = own_bundle
    session.baseline = app_open.last_id()
    try:
        with stopped_by_signals(session):
            session.url = locate.running_server() or ""
            if not session.url:
                session.own = own_server(port)
                session.own.start()
                session.url = session.own.url
            note = "" if session.own else " (the running server)"
            print(f"web UI: {session.url}{note}", flush=True)
            try:
                show(session)
            finally:
                if session.own:
                    session.own.stop()
    finally:
        lock.close()
    return 128 + session.signal if session.signal else 0
