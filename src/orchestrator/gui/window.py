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

One `orch app` per home: a second one says so and leaves; one that only serves (c, or no
display) gives that up, so the next one reuses its server. SIGTERM and SIGHUP end it as
closing the window does.
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
import webbrowser
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO, Protocol

from .. import autostart, config
from ..web import serve
from . import locate

HOST = "127.0.0.1"
STARTUP_TIMEOUT = 15.0  # seconds for the own server to listen
STOP_TIMEOUT = 10.0  # seconds for the own server, or the browser, to finish
WINDOW_SIZE = (1280, 860)
INSTALL_HINT = "install the extra orchestrator[app]"
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
    print(line, file=sys.stderr, flush=True)


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
    done: threading.Event = field(default_factory=threading.Event)  # end now
    signal: int | None = None  # the SIGTERM or SIGHUP that ended it

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
        say(
            f"no native window: pywebview is not installed ({INSTALL_HINT});"
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
    closed = threading.Event()
    try:
        window = webview.create_window(
            config.PROJECT_NAME,
            session.url,
            width=WINDOW_SIZE[0],
            height=WINDOW_SIZE[1],
            text_select=True,
            zoomable=True,
        )

        def on_closed() -> None:  # one handler: pywebview runs each in a thread of its own
            closed.set()
            session.done.set()

        window.events.closed += on_closed

        def close_later() -> None:  # runs in pywebview's own thread
            session.done.wait(session.close_after)  # closed, a stop signal or the time is up
            if not closed.is_set():
                window.destroy()

        # The GUI loop runs on this (the main) thread until the window is gone. Not private,
        # so the page keeps its preferences (theme) per origin: in storage_path with Qt/GTK;
        # macOS WebKit ignores it and keeps the data of the Python app in ~/Library.
        webview.start(
            close_later,
            gui=gui,
            private_mode=False,
            storage_path=str(config.runtime_dir() / "app-webview"),
        )
    except Exception as exc:  # no GUI toolkit (Linux without Qt or GTK), and the like
        say(
            f"no native window: pywebview failed ({first_line(exc)});"
            " trying a Chromium-family browser in app mode"
        )
        return False
    return True


def open_pages(profile: Path) -> int | None:
    """How many pages the browser using `profile` shows, asked at its DevTools endpoint (the
    port it wrote to DevToolsActivePort); None when that cannot be known (yet)."""
    try:
        port = int((profile / "DevToolsActivePort").read_text().split()[0])
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
        f"--app={session.url}",
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
        opened = webbrowser.open(session.url)
    except webbrowser.Error:
        opened = False
    if not opened:
        serving = " (serving until Ctrl-C)" if session.own else ""
        say(f"no browser could be opened: open {session.url}{serving}")
    session.hold()


def show(session: Session) -> None:
    if no_display():
        serving = ", serving it until Ctrl-C" if session.own else ""
        say(
            "no display (DISPLAY and WAYLAND_DISPLAY are unset):"
            f" open {session.url} in a browser{serving}"
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


def run(port: int = 0, close_after: float | None = None) -> int:
    """`orch app`: open the dashboard window and return once it is gone, with the exit code
    (128 + the signal when SIGTERM or SIGHUP ended it). AppError when no server can be had."""
    lock = single_instance()
    if lock is None:
        print("the dashboard window of this home is already open")
        return 0
    session = Session(lock, close_after)
    try:
        with stopped_by_signals(session):
            session.url = locate.running_server() or ""
            if not session.url:
                session.own = OwnServer(port)
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
