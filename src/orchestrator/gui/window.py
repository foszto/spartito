"""`orch app`: the dashboard in its own window, with the server it needs.

The server: this home's running one when `locate` finds it (never stopped here), otherwise
one of its own on 127.0.0.1 in a thread, stopped when the window is gone.

The window, by the first way that works; each step that fails says so in one line on stderr:
  a. a native window with pywebview (the optional extra `orchestrator[app]`): Cocoa on macOS,
     Qt on Linux, where the backend is first started in a child process (Qt aborts the whole
     process, no exception, when it cannot load its platform plugin);
  b. a Chromium-family browser in app mode, with its own profile under `run/`, so its process
     lives exactly as long as the window;
  c. the default browser; the own server then runs in the foreground until Ctrl-C.
On Linux without a display none of them is tried: the URL is printed and served until Ctrl-C.

One `orch app` per home: a second one says so and leaves.
"""

from __future__ import annotations

import contextlib
import fcntl
import os
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
import webbrowser
from pathlib import Path
from typing import IO

from .. import config
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

    def hold(self, seconds: float | None) -> None:
        """Keep serving in the foreground until Ctrl-C (or for `seconds`)."""
        with contextlib.suppress(KeyboardInterrupt):
            self.thread.join(seconds)


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


def native_window(url: str, close_after: float | None) -> bool:
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
            url,
            width=WINDOW_SIZE[0],
            height=WINDOW_SIZE[1],
            text_select=True,
            zoomable=True,
        )
        window.events.closed += closed.set

        def close_later() -> None:  # runs in pywebview's own thread
            if close_after is not None and not closed.wait(close_after):
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


def app_mode(url: str, own: OwnServer | None, close_after: float | None) -> bool:
    """b. Show `url` in a Chromium-family browser in app mode until its process ends; False
    when there is none."""
    browser = find_chromium()
    then = default_browser_note(url, own)
    if browser is None:
        say(f"no Chromium-family browser found ({CHROMIUM_NAMES}); {then}")
        return False
    profile = config.runtime_dir() / "app-profile"  # its own: the process is this window's
    argv = [
        str(browser),
        f"--app={url}",
        f"--user-data-dir={profile}",
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
    try:
        proc.wait(timeout=close_after)
    except subprocess.TimeoutExpired, KeyboardInterrupt:
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


def default_browser_note(url: str, own: OwnServer | None) -> str:
    return "opening the default browser" + (f"; serving {url} until Ctrl-C" if own else "")


def default_browser(url: str, own: OwnServer | None, close_after: float | None) -> None:
    """c. Open `url` in the default browser; keep the own server until Ctrl-C."""
    try:
        opened = webbrowser.open(url)
    except webbrowser.Error:
        opened = False
    if not opened:
        say(f"no browser could be opened: open {url}" + (" (serving until Ctrl-C)" if own else ""))
    if own:
        own.hold(close_after)


def show(url: str, own: OwnServer | None, close_after: float | None) -> None:
    if no_display():
        say(
            f"no display (DISPLAY and WAYLAND_DISPLAY are unset): open {url} in a browser"
            + (", serving it until Ctrl-C" if own else "")
        )
        if own:
            own.hold(close_after)
        return
    if native_window(url, close_after) or app_mode(url, own, close_after):
        return
    default_browser(url, own, close_after)


def single_instance() -> IO[str] | None:
    """The lock of this home's `orch app`, held while the file stays open; None when another
    process holds it."""
    handle = (config.runtime_dir() / "app.lock").open("a")
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        handle.close()
        return None
    return handle


def run(port: int = 0, close_after: float | None = None) -> None:
    """`orch app`: open the dashboard window and return once it is gone. AppError when no
    server can be had."""
    lock = single_instance()
    if lock is None:
        print("the dashboard window of this home is already open")
        return
    try:
        url = locate.running_server()
        own = None
        if url is None:
            own = OwnServer(port)
            own.start()
            url = own.url
        print(f"web UI: {url}" + ("" if own else " (the running server)"), flush=True)
        try:
            show(url, own, close_after)
        finally:
            if own:
                own.stop()
    finally:
        lock.close()
