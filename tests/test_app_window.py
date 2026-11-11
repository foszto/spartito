"""`orch app` and the server it shares with `orch web`. Everything that would show something is
faked: a fake `webview` module, a fake Chromium-family browser, a fake `webbrowser.open`. No
test uses port 8787 or opens a real window or browser."""

from __future__ import annotations

import ast
import json
import os
import signal
import socket
import subprocess
import sys
import threading
import time
import types
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from typer.testing import CliRunner

from orchestrator import app_open, autostart, cli, config
from orchestrator.gui import launcher, locate, window
from orchestrator.service import Orchestrator
from orchestrator.web import serve
from orchestrator.web.app import LOOPBACK_HOSTS
from orchestrator.web.app import app as web_app

PACKAGE = Path(cli.__file__).parent
NATIVE_MISSING = (
    "no native window: pywebview is not installed (install the extra orchestrator[app],"
    " then run `orch app install`; see docs/desktop.md); trying a Chromium-family browser in"
    " app mode"
)
DOCK_HINT = (
    "the Dock shows this window as Python: `orch app install` gives it Spartito's name and icon"
)
NO_CHROMIUM = "no Chromium-family browser found (Chrome, Chromium, Edge or Brave); "


@pytest.fixture(autouse=True)
def nothing_real(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """No pywebview, no browser found, and a default browser that only notes the URL: a
    test sees a real one only through a fake it installs itself. Returns the opened URLs."""
    monkeypatch.setitem(sys.modules, "webview", None)  # `import webview` raises ImportError
    monkeypatch.setattr(window, "mac_app_dirs", lambda: [])
    monkeypatch.setattr(window, "LINUX_BROWSERS", ())
    monkeypatch.setattr(window, "this_platform", lambda: "darwin")
    monkeypatch.setattr(window, "BACKEND_PROBE", "raise SystemExit('the probe is not faked')")
    opened: list[str] = []
    monkeypatch.setattr(window.webbrowser, "open", lambda url: opened.append(url) or True)
    monkeypatch.setattr(window, "native_active", lambda: True)
    monkeypatch.setattr(window, "native_possible", lambda: False)
    monkeypatch.setattr(window, "WATCH_EVERY", 0.05)
    monkeypatch.delenv("CFProcessPath", raising=False)
    return opened


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def listening(port: int) -> bool:
    with socket.socket() as sock:
        return sock.connect_ex(("127.0.0.1", port)) == 0


def get_json(url: str) -> dict:
    with urllib.request.urlopen(url, timeout=5) as resp:
        return json.loads(resp.read())


def port_of(url: str) -> int:
    return int(url.rstrip("/").rsplit(":", 1)[1])


# -- the shared server ---------------------------------------------------------------------


def started(server: serve.Server, sock: socket.socket | None = None) -> threading.Thread:
    thread = threading.Thread(target=serve.serve, args=(server, sock), daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.ready and thread.is_alive() and time.monotonic() < deadline:
        time.sleep(0.02)
    assert server.ready
    return thread


def stopped(server: serve.Server, thread: threading.Thread) -> None:
    server.should_exit = True
    thread.join(10)
    assert not thread.is_alive()


def test_server_records_itself_while_it_listens():
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    server = serve.make("127.0.0.1", port)
    assert web_app.state.allowed_hosts == LOOPBACK_HOSTS
    thread = started(server, sock)
    record = json.loads(serve.record_path().read_text())
    assert set(record) == {"pid", "host", "port", "started"}
    assert (record["pid"], record["host"], record["port"]) == (os.getpid(), "127.0.0.1", port)
    assert get_json(f"http://127.0.0.1:{port}/api/ping")["tool"] == "orchestrator"
    stopped(server, thread)
    sock.close()
    assert not serve.record_path().exists()
    assert not listening(port)


def test_server_leaves_a_record_that_is_no_longer_its_own():
    server = serve.make("127.0.0.1", 0)
    thread = started(server)
    other = {"pid": 1, "host": "127.0.0.1", "port": 1, "started": "later"}
    serve.record_path().write_text(json.dumps(other))
    stopped(server, thread)
    assert json.loads(serve.record_path().read_text()) == other


def test_server_that_cannot_listen_exits_as_uvicorn_did_and_keeps_the_record():
    with socket.socket() as taken:
        taken.bind(("127.0.0.1", 0))
        taken.listen()
        port = taken.getsockname()[1]
        serve.record_path().write_text('{"port": 1}')
        with pytest.raises(SystemExit) as exc:
            serve.serve(serve.make("127.0.0.1", port))
    assert exc.value.code == serve.STARTUP_FAILURE
    assert serve.record_path().read_text() == '{"port": 1}'


# -- orch web ------------------------------------------------------------------------------


def test_orch_web_goes_through_serve(monkeypatch: pytest.MonkeyPatch):
    served = []
    monkeypatch.setattr(serve, "serve", lambda server, sock=None: served.append((server, sock)))
    result = CliRunner().invoke(cli.app, ["web", "--port", "18793"])
    assert result.exit_code == 0, result.output
    assert result.stdout == "web UI: http://127.0.0.1:18793\n"
    assert "warning" not in result.stderr
    ((server, sock),) = served
    assert (server.config.host, server.config.port, server.config.log_level, sock) == (
        "127.0.0.1",
        18793,
        "warning",
        None,
    )
    assert web_app.state.allowed_hosts == LOOPBACK_HOSTS


def test_orch_web_on_another_interface_still_warns(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(serve, "serve", lambda server, sock=None: None)
    result = CliRunner().invoke(cli.app, ["web", "--host", "0.0.0.0", "--port", "18793"])
    assert result.exit_code == 0
    assert result.stderr.startswith("warning: the web UI has no authentication")
    assert result.stdout == "web UI: http://0.0.0.0:18793\n"
    assert web_app.state.allowed_hosts is None
    web_app.state.allowed_hosts = LOOPBACK_HOSTS


@pytest.mark.parametrize(
    ("sig", "code"), [(signal.SIGINT, 0), (signal.SIGTERM, -signal.SIGTERM)]
)  # uvicorn raises SIGTERM again once it has shut down, as `orch service` stops it
def test_orch_web_process_records_and_forgets_itself(sig, code):
    port = free_port()
    proc = subprocess.Popen(
        [sys.executable, "-m", "orchestrator", "web", "--port", str(port)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        deadline = time.monotonic() + 20
        while locate.running_server() is None:
            assert proc.poll() is None and time.monotonic() < deadline, proc.stderr.read()
            time.sleep(0.1)
        assert locate.running_server() == f"http://127.0.0.1:{port}/"
        assert json.loads(serve.record_path().read_text())["pid"] == proc.pid
    finally:
        proc.send_signal(sig)
        out, _ = proc.communicate(timeout=20)
    assert proc.returncode == code
    assert out == f"web UI: http://127.0.0.1:{port}\n"
    assert not serve.record_path().exists()


# -- locate --------------------------------------------------------------------------------


class Pinged(BaseHTTPRequestHandler):
    answer: bytes = b""
    redirect: str = ""

    def do_GET(self) -> None:
        if self.redirect:
            self.send_response(302)
            self.send_header("Location", self.redirect)
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(self.answer)

    def log_message(self, *args) -> None:
        pass


@pytest.fixture
def pinged():
    """A stand-in server on a free loopback port answering /api/ping with what it is told;
    returns a function that sets the answer and records it in run/web.json."""
    server = ThreadingHTTPServer(("127.0.0.1", 0), Pinged)
    threading.Thread(target=server.serve_forever, daemon=True).start()

    def answer(body: dict, host: str = "127.0.0.1", redirect: str = "") -> int:
        Pinged.answer, Pinged.redirect = json.dumps(body).encode(), redirect
        port = server.server_address[1]
        serve.record_path().write_text(json.dumps({"pid": 1, "host": host, "port": port}))
        return port

    yield answer
    server.shutdown()
    server.server_close()


def this_home() -> str:
    return autostart.home_hash(autostart.home())[:16]


def test_locate_accepts_a_server_of_this_home(pinged):
    port = pinged({"tool": "orchestrator", "version": "x", "home": this_home()})
    assert locate.running_server() == f"http://127.0.0.1:{port}/"


def test_locate_asks_a_wildcard_host_on_loopback(pinged):
    port = pinged({"tool": "orchestrator", "home": this_home()}, host="0.0.0.0")
    assert locate.running_server() == f"http://127.0.0.1:{port}/"


@pytest.mark.parametrize(
    "body",
    [
        {"tool": "orchestrator", "home": "0123456789abcdef"},  # another home
        {"tool": "something-else", "home": "any"},  # another tool
        ["not", "an", "object"],
    ],
)
def test_locate_refuses_another_home_or_tool(pinged, body):
    if isinstance(body, dict) and body["home"] == "any":
        body["home"] = this_home()
    pinged(body)
    assert locate.running_server() is None


@pytest.mark.parametrize(
    "record",
    [None, "not json", '{"host": "127.0.0.1"}', '{"host": "127.0.0.1", "port": 0}', "[1, 2]"],
)
def test_locate_refuses_a_missing_or_broken_record(record):
    if record is not None:
        serve.record_path().write_text(record)
    assert locate.running_server() is None


@pytest.mark.parametrize("host", ["example.invalid", "192.0.2.1", "localhost"])
def test_locate_contacts_only_loopback(pinged, host):
    port = pinged({"tool": "orchestrator", "home": this_home()}, host=host)
    assert locate.running_server() == f"http://127.0.0.1:{port}/"


def test_locate_uses_ipv6_loopback_for_an_ipv6_host():
    assert [locate.loopback(h) for h in ("::", "::1", "fe80::1", "0.0.0.0")] == [
        "::1",
        "::1",
        "::1",
        "127.0.0.1",
    ]
    assert locate.url_of("::1", 5) == "http://[::1]:5/"


def test_ping_ignores_proxies(pinged, monkeypatch: pytest.MonkeyPatch):
    for name in ("no_proxy", "NO_PROXY"):
        monkeypatch.delenv(name, raising=False)
    for name in ("http_proxy", "HTTP_PROXY", "all_proxy", "ALL_PROXY"):
        monkeypatch.setenv(name, f"http://127.0.0.1:{free_port()}")  # nothing listens there
    port = pinged({"tool": "orchestrator", "home": this_home()})
    assert locate.running_server() == f"http://127.0.0.1:{port}/"
    assert autostart.ping("127.0.0.1", port, autostart.home()) == ("ours", "")


def test_ping_does_not_follow_redirects(pinged):
    server = serve.make("127.0.0.1", 0)
    thread = started(server)
    try:
        ours = serve.record_path().read_text()
        target = f"http://127.0.0.1:{json.loads(ours)['port']}/api/ping"
        port = pinged({}, redirect=target)
        assert autostart.ping("127.0.0.1", port, autostart.home()) == (
            "foreign",
            "/api/ping answered HTTP 302",
        )
        assert locate.running_server() is None
    finally:
        stopped(server, thread)


def test_locate_refuses_a_dead_port():
    serve.record_path().write_text(json.dumps({"pid": 1, "host": "127.0.0.1", "port": free_port()}))
    assert locate.running_server() is None


# -- the window ----------------------------------------------------------------------------


class Event:
    def __init__(self) -> None:
        self.handlers: list = []

    def __iadd__(self, handler):
        self.handlers.append(handler)
        return self

    def fire(self) -> None:
        for handler in self.handlers:
            handler()


class FakeWindow:
    def __init__(self, title: str, url: str, **options) -> None:
        self.title, self.url, self.options = title, url, options
        self.base = url.split("?")[0]  # the server's URL
        self.events = types.SimpleNamespace(closed=Event())
        self.destroyed = threading.Event()
        self.raised: list[str] = []  # restore and show, as called
        self.location = url  # what evaluate_js("location.href") answers

    def destroy(self) -> None:
        self.destroyed.set()

    def restore(self) -> None:
        self.raised.append("restore")

    def show(self) -> None:
        self.raised.append("show")

    def evaluate_js(self, script: str):
        assert script == "location.href"
        return self.location


class FakeWebview(types.ModuleType):
    """pywebview: `start` asks the page's server for /api/ping while the window is open, then
    returns when the window is destroyed (by `func`) or, without `close_after`, at once (the
    user closed it)."""

    def __init__(self, fail: Exception | None = None) -> None:
        super().__init__("webview")
        self.fail = fail
        self.windows: list[FakeWindow] = []
        self.options: dict = {}
        self.ping: dict | None = None
        self.main_thread = False
        self.open_for: float | None = 0.5  # seconds until the user closes it; None: never
        self.on_start = lambda: None  # what happens once the window is up

    def create_window(self, title: str, url: str, **options) -> FakeWindow:
        self.windows.append(FakeWindow(title, url, **options))
        return self.windows[-1]

    def start(self, func=None, *args, **options) -> None:
        if self.fail:
            raise self.fail
        self.options = options
        self.main_thread = threading.current_thread() is threading.main_thread()
        win = self.windows[-1]
        self.ping = get_json(win.base + "api/ping")
        if func:
            threading.Thread(target=func, daemon=True).start()
        self.on_start()
        win.destroyed.wait(self.open_for)
        win.events.closed.fire()


@pytest.fixture
def webview(monkeypatch: pytest.MonkeyPatch) -> FakeWebview:
    fake = FakeWebview()
    monkeypatch.setitem(sys.modules, "webview", fake)
    return fake


# A Chromium-family browser. FAKE_CHROMIUM_MODE: "window" (the default) shows the page until
# FAKE_CHROMIUM_OPEN_FOR seconds after its DevTools endpoint first listed it (so orch app has
# seen it, however slow the machine) and stays without it, as macOS browsers do, until SIGTERM;
# "quit" leaves as its window closes, as on Linux, before ever listing no page; "silent" has no
# DevTools endpoint and stays; "exit<N>" leaves at once with code N. The endpoint (with
# --remote-debugging-port=0) lists a background page always and the page while it is shown.
# Notes its argv, then "TERM" when terminated, in $FAKE_CHROMIUM_LOG, and what /api/ping
# answered it in <log>.ping.
CHROMIUM = """#!PYTHON
import json, os, signal, sys, threading, time, urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

log = os.environ["FAKE_CHROMIUM_LOG"]
mode = os.environ.get("FAKE_CHROMIUM_MODE", "window")
open_for = float(os.environ.get("FAKE_CHROMIUM_OPEN_FOR", "1e9"))
args = sys.argv[1:]
with open(log, "w") as out:
    out.write("\\n".join(args) + "\\n")


def ended(*_):
    with open(log, "a") as out:
        out.write("TERM\\n")
    os._exit(0)


signal.signal(signal.SIGTERM, ended)
if mode.startswith("exit"):
    sys.exit(int(mode[4:]))
url = next(a for a in args if a.startswith("--app=")).removeprefix("--app=")
with urllib.request.urlopen(url.split("?")[0] + "api/ping", timeout=5) as resp, open(log + ".ping", "wb") as out:
    out.write(resp.read())
listed = []  # when the page was first listed


def window_open():
    return not listed or time.monotonic() - listed[0] < open_for


class DevTools(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path.startswith("/json/activate/"):
            with open(log, "a") as out:
                out.write("ACTIVATE " + self.path.rsplit("/", 1)[1] + "\\n")
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"Target activated")
            return
        targets = [{"type": "background_page", "id": "b"}]
        if window_open():
            targets.append({"type": "page", "id": "p", "url": url})
            listed[:] = listed or [time.monotonic()]
        elif mode == "quit":
            os._exit(0)
        self.send_response(200 if self.path == "/json/list" else 404)
        self.end_headers()
        self.wfile.write(json.dumps(targets).encode())

    def log_message(self, *args):
        pass


if mode != "silent" and "--remote-debugging-port=0" in args:
    server = ThreadingHTTPServer(("127.0.0.1", 0), DevTools)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    profile = next(a for a in args if a.startswith("--user-data-dir=")).split("=", 1)[1]
    os.makedirs(profile, exist_ok=True)
    with open(os.path.join(profile, "DevToolsActivePort"), "w") as out:
        out.write(str(server.server_address[1]) + "\\n/devtools/browser/x\\n")
while mode != "quit" or window_open():
    time.sleep(0.05)
"""


def install_chromium(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(CHROMIUM.replace("PYTHON", sys.executable, 1))
    path.chmod(0o755)


@pytest.fixture
def chromium(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A fake Google Chrome bundle in a fake ~/Applications, whose window is closed after a
    second; returns its log."""
    apps = tmp_path / "Applications"
    install_chromium(apps / "Google Chrome.app" / "Contents" / "MacOS" / "Google Chrome")
    monkeypatch.setattr(window, "mac_app_dirs", lambda: [tmp_path / "missing", apps])
    monkeypatch.setattr(window, "POLL", 0.1)
    log = tmp_path / "chromium.log"
    monkeypatch.setenv("FAKE_CHROMIUM_LOG", str(log))
    monkeypatch.setenv("FAKE_CHROMIUM_OPEN_FOR", "1")
    return log


@pytest.fixture
def launched(monkeypatch: pytest.MonkeyPatch) -> list[subprocess.Popen]:
    """The processes started while the test runs (the browser among them)."""
    procs: list[subprocess.Popen] = []

    class Recorded(subprocess.Popen):
        def __init__(self, *args, **kwargs) -> None:
            super().__init__(*args, **kwargs)
            procs.append(self)

    monkeypatch.setattr(window.subprocess, "Popen", Recorded)
    return procs


def opened_then_hangup(urls: list[str]):
    """A default browser that notes the URL, then SIGHUP: it ends orch app once it serves."""

    def opened(url: str) -> bool:
        urls.append(url)
        os.kill(os.getpid(), signal.SIGHUP)
        return True

    return opened


def kill_self(sig: int):
    return lambda: os.kill(os.getpid(), sig)


def lock_is_free() -> bool:
    lock = window.single_instance()
    if lock is None:
        return False
    lock.close()
    return True


def run_app(*args: str):
    return CliRunner().invoke(cli.app, ["app", *args])


def assert_own_server_gone(url: str) -> None:
    assert url.startswith("http://127.0.0.1:")
    assert not listening(port_of(url))
    assert not serve.record_path().exists()


def test_native_window_on_an_own_server(webview: FakeWebview):
    result = run_app()
    assert result.exit_code == 0, result.output
    assert result.stderr == DOCK_HINT + "\n"  # macOS without the launcher
    (win,) = webview.windows
    assert result.stdout == f"web UI: {win.base}\n"
    assert win.url == f"{win.base}?app=0"  # the window's page, before any request
    assert webview.options["icon"] == str(launcher.ICNS)  # the Dock icon
    assert win.title == config.PROJECT_NAME
    assert win.options["text_select"] is True
    assert webview.main_thread
    assert webview.ping["home"] == this_home()
    assert webview.options["private_mode"] is False
    assert webview.options["gui"] is None  # pywebview's choice: Cocoa
    assert not win.destroyed.is_set()  # the user closed it
    assert_own_server_gone(win.base)


def test_native_window_closes_itself_after_the_given_time(webview: FakeWebview):
    webview.open_for = None  # until destroyed
    result = run_app("--close-after", "0.1")
    assert result.exit_code == 0, result.output
    (win,) = webview.windows
    assert win.destroyed.is_set()
    assert_own_server_gone(win.base)


def test_own_server_on_a_given_port(webview: FakeWebview):
    port = free_port()
    result = run_app("--port", str(port))
    assert result.exit_code == 0, result.output
    assert webview.windows[0].base == f"http://127.0.0.1:{port}/"
    assert not listening(port)


def test_own_server_port_taken():
    with socket.socket() as taken:
        taken.bind(("127.0.0.1", 0))
        taken.listen()
        port = taken.getsockname()[1]
        result = run_app("--port", str(port))
    assert result.exit_code == 1
    assert result.stderr.startswith(f"error: cannot listen on 127.0.0.1:{port}: ")


def test_running_server_of_this_home_is_reused_and_kept(webview: FakeWebview):
    server = serve.make("127.0.0.1", 0)
    thread = started(server)
    try:
        url = locate.running_server()
        result = run_app("--close-after", "0.1")
        assert result.exit_code == 0, result.output
        assert result.stdout == f"web UI: {url} (the running server)\n"
        assert webview.windows[0].base == url
        assert listening(port_of(url))
        assert locate.running_server() == url
    finally:
        stopped(server, thread)


def test_server_of_another_home_is_not_reused(webview: FakeWebview, pinged):
    foreign = pinged({"tool": "orchestrator", "home": "0123456789abcdef"})
    result = run_app()
    assert result.exit_code == 0, result.output
    url = webview.windows[0].base
    assert port_of(url) != foreign
    assert webview.ping["home"] == this_home()
    assert_own_server_gone(url)


def test_chromium_app_mode_ends_when_its_window_is_closed(chromium: Path):
    result = run_app()
    assert result.exit_code == 0, result.output
    assert result.stderr == NATIVE_MISSING + "\n"
    url = result.stdout.removeprefix("web UI: ").strip()
    assert chromium.read_text().splitlines() == [
        f"--app={url}?app=0",
        f"--user-data-dir={config.runtime_dir() / 'app-profile'}",
        "--remote-debugging-port=0",
        "--no-first-run",
        "--no-default-browser-check",
        "TERM",  # it stayed without its window, as on macOS: ended by orch app
    ]
    assert json.loads(Path(f"{chromium}.ping").read_text())["home"] == this_home()
    assert_own_server_gone(url)
    assert lock_is_free()


def test_chromium_that_quits_with_its_window(chromium: Path, monkeypatch):
    monkeypatch.setenv("FAKE_CHROMIUM_MODE", "quit")
    result = run_app()
    assert result.exit_code == 0, result.output
    assert "TERM" not in chromium.read_text()
    assert_own_server_gone(result.stdout.removeprefix("web UI: ").strip())


@pytest.mark.parametrize("code", [0, 1])
def test_chromium_that_exits_at_once_did_not_start(chromium: Path, nothing_real, monkeypatch, code):
    monkeypatch.setenv("FAKE_CHROMIUM_MODE", f"exit{code}")
    monkeypatch.setattr(window, "EARLY_EXIT", 600.0)  # "at once" however slow the machine
    monkeypatch.setattr(window.webbrowser, "open", opened_then_hangup(nothing_real))
    result = run_app()  # no time limit: that would also end the wait for the browser
    assert result.exit_code == 128 + signal.SIGHUP, result.output
    (url,) = nothing_real
    assert result.stderr == (
        f"{NATIVE_MISSING}\nGoogle Chrome did not start (it exited at once, code {code});"
        f" opening the default browser; serving {url} until Ctrl-C\n"
    )
    assert_own_server_gone(url)


def test_chromium_app_mode_when_pywebview_fails(chromium: Path, monkeypatch: pytest.MonkeyPatch):
    fail = RuntimeError("You must have either QT or GTK\nwith Python extensions")
    monkeypatch.setitem(sys.modules, "webview", FakeWebview(fail=fail))
    result = run_app()
    assert result.exit_code == 0, result.output
    assert result.stderr == (
        "no native window: pywebview failed (You must have either QT or GTK);"
        " trying a Chromium-family browser in app mode\n"
    )
    assert chromium.exists()


def test_chromium_without_devtools_is_ended_after_the_given_time(
    chromium: Path, launched: list[subprocess.Popen], monkeypatch
):
    monkeypatch.setenv("FAKE_CHROMIUM_MODE", "silent")
    result = run_app("--close-after", "1")
    assert result.exit_code == 0, result.output
    (browser,) = launched
    # Ended by orch app: by its SIGTERM handler, or before the fake had one
    assert browser.returncode in (0, -signal.SIGTERM)
    assert_own_server_gone(result.stdout.removeprefix("web UI: ").strip())


def test_chromium_found_on_linux_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name in ("chromium-browser", "brave-browser"):
        (bin_dir / name).write_text("#!/bin/sh\n")
        (bin_dir / name).chmod(0o755)
    monkeypatch.setenv("PATH", str(bin_dir))
    monkeypatch.setattr(window, "this_platform", lambda: "linux")
    monkeypatch.setattr(
        window,
        "LINUX_BROWSERS",
        ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser", "brave-browser"),
    )
    assert window.find_chromium() == bin_dir / "chromium-browser"


def test_chromium_bundles_on_macos(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    for root, name in ((tmp_path / "a", "Brave Browser"), (tmp_path / "b", "Microsoft Edge")):
        binary = root / f"{name}.app" / "Contents" / "MacOS" / name
        binary.parent.mkdir(parents=True)
        binary.write_text("#!/bin/sh\n")
        binary.chmod(0o755)
    monkeypatch.setattr(window, "mac_app_dirs", lambda: [tmp_path / "a", tmp_path / "b"])
    assert window.find_chromium().name == "Microsoft Edge"  # Edge comes before Brave


def test_default_browser_with_an_own_server(nothing_real: list[str]):
    result = run_app("--close-after", "0.2")
    assert result.exit_code == 0, result.output
    (url,) = nothing_real
    assert result.stderr == (
        f"{NATIVE_MISSING}\n{NO_CHROMIUM}opening the default browser; serving {url} until Ctrl-C\n"
    )
    assert_own_server_gone(url)


def test_default_browser_with_the_running_server(nothing_real: list[str]):
    server = serve.make("127.0.0.1", 0)
    thread = started(server)
    try:
        result = run_app()  # no --close-after: returns at once, nothing of its own to serve
        assert result.exit_code == 0, result.output
        assert result.stderr == f"{NATIVE_MISSING}\n{NO_CHROMIUM}opening the default browser\n"
        assert nothing_real == [locate.running_server()]
    finally:
        stopped(server, thread)


def test_no_browser_at_all(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(window.webbrowser, "open", lambda url: False)
    result = run_app("--close-after", "0.2")
    assert result.exit_code == 0, result.output
    url = result.stdout.removeprefix("web UI: ").strip()
    assert (
        result.stderr.splitlines()[-1]
        == f"no browser could be opened: open {url} (serving until Ctrl-C)"
    )
    assert_own_server_gone(url)


def test_linux_without_a_display(webview: FakeWebview, chromium: Path, nothing_real, monkeypatch):
    monkeypatch.setattr(window, "this_platform", lambda: "linux")
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    result = run_app("--close-after", "0.2")
    assert result.exit_code == 0, result.output
    url = result.stdout.removeprefix("web UI: ").strip()
    assert result.stderr == (
        f"no display (DISPLAY and WAYLAND_DISPLAY are unset): open {url} in a browser,"
        " serving it until Ctrl-C\n"
    )
    assert (webview.windows, nothing_real, chromium.exists()) == ([], [], False)
    assert_own_server_gone(url)


def test_linux_with_wayland_opens_a_window(webview: FakeWebview, monkeypatch):
    monkeypatch.setattr(window, "this_platform", lambda: "linux")
    monkeypatch.setattr(window, "BACKEND_PROBE", "pass")
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-0")
    assert run_app().exit_code == 0
    assert len(webview.windows) == 1


# Stand-ins for pywebview's backend start in the child process (window.BACKEND_PROBE); each
# notes the backend it was told to force (its argument) in $FAKE_PROBE_LOG.
NOTE_GUI = "import os, sys; open(os.environ['FAKE_PROBE_LOG'], 'w').write(sys.argv[1])\n"
QT_PLUGIN = (
    "qt.qpa.plugin: From 6.5.0, xcb-cursor0 or libxcb-cursor0 is needed to load the Qt xcb"
    " platform plugin."
)
QT_ABORTS = (
    NOTE_GUI + f"print({QT_PLUGIN!r}, file=sys.stderr)\n"
    "print('This application failed to start because no Qt platform plugin could be"
    " initialized.', file=sys.stderr, flush=True)\n"
    "os._exit(134)\n"  # as an abort ends it, without a core dump or crash report
)
QT_IMPORT_FAILS = (
    NOTE_GUI + "print('[pywebview] QT cannot be loaded', file=sys.stderr)\n"
    "print('Traceback (most recent call last):', file=sys.stderr)\n"
    "print('  File \"qt.py\", line 1', file=sys.stderr)\n"
    "print('ImportError: libEGL.so.1: cannot open shared object file', file=sys.stderr)\n"
    "raise RuntimeError('You must have either QT or GTK with Python extensions installed')\n"
)


@pytest.fixture
def linux(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Linux with an X display and a fake `chromium` on PATH; returns the probe's log."""
    monkeypatch.setattr(window, "this_platform", lambda: "linux")
    monkeypatch.setenv("DISPLAY", ":0")
    monkeypatch.delenv("PYWEBVIEW_GUI", raising=False)
    bin_dir = tmp_path / "linux-bin"
    bin_dir.mkdir()
    install_chromium(bin_dir / "chromium")
    monkeypatch.setenv("FAKE_CHROMIUM_OPEN_FOR", "1")
    monkeypatch.setattr(window, "POLL", 0.1)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setattr(window, "LINUX_BROWSERS", ("chromium",))
    monkeypatch.setenv("FAKE_CHROMIUM_LOG", str(tmp_path / "chromium.log"))
    monkeypatch.setattr(window, "name_qt_app", lambda: None)
    log = tmp_path / "probe.log"
    monkeypatch.setenv("FAKE_PROBE_LOG", str(log))
    return log


def test_linux_native_window_with_qt(webview: FakeWebview, linux: Path, monkeypatch):
    monkeypatch.setattr(window, "BACKEND_PROBE", NOTE_GUI)
    result = run_app()
    assert result.exit_code == 0, result.output
    assert result.stderr == ""
    assert linux.read_text() == "qt"
    assert webview.options["gui"] == "qt"
    assert_own_server_gone(webview.windows[0].base)


def test_linux_backend_chosen_by_pywebview_gui(webview: FakeWebview, linux: Path, monkeypatch):
    monkeypatch.setenv("PYWEBVIEW_GUI", "gtk")
    monkeypatch.setattr(window, "BACKEND_PROBE", NOTE_GUI)
    assert run_app().exit_code == 0
    assert linux.read_text() == ""
    assert webview.options["gui"] is None


@pytest.mark.parametrize(
    ("probe", "reason"),
    [
        (QT_ABORTS, QT_PLUGIN),
        (QT_IMPORT_FAILS, "ImportError: libEGL.so.1: cannot open shared object file"),
    ],
)
def test_linux_backend_that_cannot_start(
    webview: FakeWebview, linux: Path, monkeypatch, probe, reason
):
    monkeypatch.setattr(window, "BACKEND_PROBE", probe)
    result = run_app()
    assert result.exit_code == 0, result.output
    assert result.stderr == (
        f"no native window: pywebview cannot start its GUI backend ({reason});"
        " trying a Chromium-family browser in app mode\n"
    )
    assert linux.read_text() == "qt"
    assert webview.windows == []
    url = result.stdout.removeprefix("web UI: ").strip()
    assert (linux.parent / "chromium.log").read_text().splitlines()[0] == f"--app={url}?app=0"
    assert_own_server_gone(url)


# -- signals, the lock while serving --------------------------------------------------------


def test_sigterm_ends_chromium_app_mode(chromium: Path, monkeypatch):
    monkeypatch.setenv("FAKE_CHROMIUM_OPEN_FOR", "1e9")
    shown = window.open_pages

    def open_pages(profile: Path) -> int | None:
        pages = shown(profile)
        if pages:  # the window is up: now the signal
            os.kill(os.getpid(), signal.SIGTERM)
        return pages

    monkeypatch.setattr(window, "open_pages", open_pages)
    result = run_app()
    assert result.exit_code == 128 + signal.SIGTERM, result.output
    assert chromium.read_text().splitlines()[-1] == "TERM"
    assert_own_server_gone(result.stdout.removeprefix("web UI: ").strip())
    assert lock_is_free()
    assert signal.getsignal(signal.SIGTERM) is signal.SIG_DFL  # restored


def test_sighup_ends_serving(nothing_real: list[str], monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(window.webbrowser, "open", opened_then_hangup(nothing_real))
    result = run_app()  # serves until a signal
    assert result.exit_code == 128 + signal.SIGHUP, result.output
    assert_own_server_gone(nothing_real[0])


def test_sigterm_closes_the_native_window(webview: FakeWebview):
    webview.open_for = None  # until destroyed
    webview.on_start = kill_self(signal.SIGTERM)
    result = run_app()
    assert result.exit_code == 128 + signal.SIGTERM, result.output
    (win,) = webview.windows
    assert win.destroyed.is_set()
    assert_own_server_gone(win.base)


def test_serving_only_gives_up_the_lock_for_a_second_app(monkeypatch: pytest.MonkeyPatch):
    seen = []

    def second_app(url: str) -> None:  # once the first one only serves
        while not lock_is_free():
            time.sleep(0.05)
        seen.append((True, locate.running_server(), url))
        os.kill(os.getpid(), signal.SIGHUP)

    def opened(url: str) -> bool:
        threading.Thread(target=second_app, args=(url,), daemon=True).start()
        return True

    monkeypatch.setattr(window.webbrowser, "open", opened)
    result = run_app()
    assert result.exit_code == 128 + signal.SIGHUP, result.output
    ((free, found, url),) = seen
    assert free
    assert found == url  # a second `orch app` reuses this server
    assert_own_server_gone(url)


def webview_imports(path: Path) -> list[str]:
    """Where `path` imports pywebview: the name of the enclosing function, or "<module>"."""
    found = []

    def visit(node: ast.AST, where: str) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef):
                visit(child, child.name)
                continue
            if isinstance(child, ast.Import | ast.ImportFrom):
                names = [a.name for a in child.names]
                if isinstance(child, ast.ImportFrom):
                    names = [child.module or ""]
                if any(name.split(".")[0] == "webview" for name in names):
                    found.append(where)
            visit(child, where)

    visit(ast.parse(path.read_text()), "<module>")
    return found


def test_pywebview_is_imported_only_to_open_the_native_window():
    found = {
        path.relative_to(PACKAGE).as_posix(): where
        for path in PACKAGE.rglob("*.py")
        if (where := webview_imports(path))
    }
    assert found == {"gui/window.py": ["native_window"]}


# -- a second `orch app`, deep links ---------------------------------------------------------


def second_app(target: str = "") -> dict:
    """What a second `orch app` does, from a thread: write the request and wait for the
    window's answer."""
    number = Orchestrator.request_app_open(target)
    deadline = time.monotonic() + 10
    while (reply := app_open.answered(number)) is None and time.monotonic() < deadline:
        time.sleep(0.02)
    assert reply is not None
    return {"id": number, **reply}


def test_second_app_brings_the_native_window_forward_at_its_target(webview: FakeWebview):
    webview.open_for = None  # until destroyed
    seen = []

    def meanwhile() -> None:
        win = webview.windows[-1]
        reply = second_app("decision:5")
        state = get_json(win.base + "api/state")["app_open"]  # what the page gets
        seen.append((reply, list(win.raised), state))
        win.destroy()

    webview.on_start = lambda: threading.Thread(target=meanwhile, daemon=True).start()
    result = run_app()
    assert result.exit_code == 0, result.output
    ((reply, raised, state),) = seen
    assert reply == {"id": reply["id"], "raised": True, "detail": ""}
    assert raised == ["restore", "show"]
    assert state == {"id": reply["id"], "target": "decision:5"}


def test_native_window_that_macos_keeps_behind(webview: FakeWebview, monkeypatch):
    monkeypatch.setattr(window, "native_active", lambda: False)
    monkeypatch.setattr(window, "ACTIVE_WAIT", 0.1)
    webview.open_for = None
    seen = []

    def meanwhile() -> None:
        seen.append(second_app())
        webview.windows[-1].destroy()

    webview.on_start = lambda: threading.Thread(target=meanwhile, daemon=True).start()
    assert run_app().exit_code == 0
    assert seen[0]["raised"] is False
    assert seen[0]["detail"] == "macOS kept another application in front"


def test_a_request_from_before_the_window_is_not_applied(webview: FakeWebview):
    earlier = Orchestrator.request_app_open("decision:1")
    webview.open_for = 0.5
    assert run_app().exit_code == 0
    (win,) = webview.windows
    assert win.url == f"{win.base}?app={earlier}"  # the page applies only later ones
    assert win.raised == []
    assert app_open.answered(earlier) is None


@pytest.mark.parametrize(
    ("args", "fragment"),
    [
        (["--decision", "7"], "#decision:7"),
        (["--agent", "worker-3f9a"], "#worker-3f9a"),
        (["--agent", "orchestrator"], "#orchestrator"),
    ],
)
def test_the_window_opens_at_its_target(webview: FakeWebview, args, fragment):
    assert run_app(*args).exit_code == 0
    (win,) = webview.windows
    assert win.url == f"{win.base}?app=0{fragment}"


def test_the_default_browser_opens_at_the_target(nothing_real: list[str], monkeypatch):
    monkeypatch.setattr(window.webbrowser, "open", opened_then_hangup(nothing_real))
    result = run_app("--decision", "7")
    assert result.exit_code == 128 + signal.SIGHUP, result.output
    (url,) = nothing_real
    assert url.endswith("/#decision:7") and "?app" not in url


@pytest.mark.parametrize(
    ("args", "error"),
    [
        (["--agent", "a", "--decision", "1"], "use either --agent or --decision"),
        (["--agent", "Not An Id"], "not an agent id or decision:<number>: 'Not An Id'"),
        (["--agent", "x#y"], "not an agent id or decision:<number>: 'x#y'"),
        (["--decision", "-1"], "--decision takes a decision number"),
    ],
)
def test_targets_are_checked(webview: FakeWebview, args, error):
    result = run_app(*args)
    assert result.exit_code == 1
    assert result.stderr == f"error: {error}\n"
    assert webview.windows == []
    assert not (config.runtime_dir() / app_open.REQUEST).exists()


def answering(raised: bool, detail: str = ""):
    """The open window's `orch app`, from a thread: answers the next request."""

    def answer() -> None:
        before = app_open.last_id()
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if (request := app_open.latest()) and request["id"] > before:
                app_open.answer(request["id"], raised, detail)
                return
            time.sleep(0.02)

    thread = threading.Thread(target=answer, daemon=True)
    thread.start()
    return thread


@pytest.mark.parametrize(
    ("args", "raised", "detail", "line"),
    [
        ([], True, "", ": brought it to the front"),
        (["--decision", "5"], True, "", ": brought it to the front, showing decision:5"),
        (
            ["--agent", "worker-3f9a"],
            False,
            "the browser shows no page",
            ", but it could not be brought to the front (the browser shows no page);"
            " asked it to show worker-3f9a",
        ),
    ],
)
def test_second_app_says_in_one_line_how_it_went(webview: FakeWebview, args, raised, detail, line):
    lock = window.single_instance()
    assert lock is not None
    try:
        thread = answering(raised, detail)
        result = run_app(*args)
        thread.join()
    finally:
        lock.close()
    assert result.exit_code == 0, result.output
    assert result.stdout == f"the dashboard window of this home is already open{line}\n"
    assert result.stderr == ""
    assert webview.windows == []
    assert not serve.record_path().exists()


def test_one_app_per_home(webview: FakeWebview, monkeypatch):
    monkeypatch.setattr(window, "ANSWER_TIMEOUT", 0.2)
    lock = window.single_instance()
    assert lock is not None
    try:
        result = run_app()
    finally:
        lock.close()
    assert result.exit_code == 0
    assert result.stdout == (
        "the dashboard window of this home is already open, but it could not be brought to"
        " the front (the window did not answer)\n"
    )
    assert app_open.latest()["target"] == ""
    assert webview.windows == []
    assert not serve.record_path().exists()
    assert run_app().exit_code == 0  # released
    assert len(webview.windows) == 1


def test_second_app_brings_chromium_app_mode_forward(chromium: Path, monkeypatch):
    monkeypatch.setenv("FAKE_CHROMIUM_OPEN_FOR", "3")
    seen = []

    def opened() -> None:  # once the browser shows the page
        while "--app=" not in (chromium.read_text() if chromium.exists() else ""):
            time.sleep(0.02)
        time.sleep(0.5)
        seen.append(second_app("decision:2"))

    threading.Thread(target=opened, daemon=True).start()
    result = run_app()
    assert result.exit_code == 0, result.output
    assert seen[0]["raised"] is True, seen
    assert "ACTIVATE p" in chromium.read_text().splitlines()


def test_echo_location_says_where_the_page_is(webview: FakeWebview):
    webview.open_for = 1.5
    webview.on_start = lambda: setattr(webview.windows[-1], "location", "http://x/#decision:3")
    result = run_app("--echo-location")
    assert result.exit_code == 0, result.output
    assert "location: http://x/#decision:3\n" in result.stderr


# -- the remembered port --------------------------------------------------------------------


def test_own_server_uses_the_remembered_port_again(webview: FakeWebview):
    assert run_app().exit_code == 0
    first = port_of(webview.windows[0].base)
    remembered = config.runtime_dir() / window.PORT_FILE
    assert remembered.read_text() == f"{first}\n"
    assert run_app().exit_code == 0
    assert port_of(webview.windows[1].base) == first


def test_a_taken_remembered_port_is_replaced(webview: FakeWebview):
    with socket.socket() as taken:
        taken.bind(("127.0.0.1", 0))
        taken.listen()
        busy = taken.getsockname()[1]
        (config.runtime_dir() / window.PORT_FILE).write_text(f"{busy}\n")
        assert run_app().exit_code == 0
    port = port_of(webview.windows[0].base)
    assert port != busy
    assert (config.runtime_dir() / window.PORT_FILE).read_text() == f"{port}\n"


@pytest.mark.parametrize("content", ["", "nonsense", "0", "70000"])
def test_a_broken_remembered_port_is_ignored(webview: FakeWebview, content):
    (config.runtime_dir() / window.PORT_FILE).write_text(content)
    assert run_app().exit_code == 0
    port = port_of(webview.windows[0].base)
    assert (config.runtime_dir() / window.PORT_FILE).read_text() == f"{port}\n"


def test_a_given_port_is_not_remembered(webview: FakeWebview):
    port = free_port()
    assert run_app("--port", str(port)).exit_code == 0
    assert not (config.runtime_dir() / window.PORT_FILE).exists()


# -- identity: the launcher's app ----------------------------------------------------------


@pytest.fixture
def execed(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, list[str], dict]]:
    """os.execve, noted; like the real one, it does not return."""
    calls: list[tuple[str, list[str], dict]] = []

    def execute(executable: str, argv: list[str], env: dict) -> None:
        calls.append((executable, argv, env))
        raise SystemExit(0)

    monkeypatch.setattr(window, "execute", execute)
    monkeypatch.setattr(window, "native_possible", lambda: True)
    monkeypatch.setattr(window, "read_main_bundle", lambda: None)
    monkeypatch.setattr(launcher, "this_platform", lambda: "darwin")
    return calls


def test_from_a_terminal_it_runs_on_as_the_installed_launcher(webview: FakeWebview, execed):
    item = launcher.launcher()
    launcher.install(item)
    result = run_app("--decision", "3", "--close-after", "2", "--port", "18811")
    assert result.exit_code == 0, result.output
    ((executable, argv, env),) = execed
    assert executable == sys.executable
    assert argv == [sys.executable, "-m", "orchestrator", "app"] + [
        "--port", "18811", "--close-after", "2.0", "--decision", "3"
    ]  # fmt: skip
    assert env["CFProcessPath"] == str(item.executable)
    assert env["ORCH_HOME"] == os.environ["ORCH_HOME"]
    assert webview.windows == []
    assert lock_is_free()


@pytest.mark.parametrize("why", ["not installed", "another home", "no pywebview", "linux"])
def test_no_relaunch_without_this_homes_launcher_and_pywebview(
    webview: FakeWebview, execed, monkeypatch, why
):
    if why == "another home":
        item = launcher.launcher()
        other = launcher.Launcher(**{**item.__dict__, "identifier": "local.spartito-0badf00d.app"})
        launcher.install(other)
    elif why == "no pywebview":
        launcher.install(launcher.launcher())
        monkeypatch.setattr(window, "native_possible", lambda: False)
    elif why == "linux":
        launcher.install(launcher.launcher())
        monkeypatch.setattr(window, "this_platform", lambda: "linux")
        monkeypatch.setattr(launcher, "this_platform", lambda: "linux")
        monkeypatch.setenv("DISPLAY", ":0")
        monkeypatch.setattr(window, "backend_problem", lambda gui: None)
        monkeypatch.setattr(window, "name_qt_app", lambda: None)
    assert run_app().exit_code == 0
    assert execed == []
    assert len(webview.windows) == 1


def test_started_as_the_launchers_app(webview: FakeWebview, execed, monkeypatch):
    """With CFProcessPath (the launcher's script, or the relaunch): no relaunch, no Dock hint,
    no icon set at run time, and the variable is not handed on."""
    launcher.install(launcher.launcher())
    monkeypatch.setenv("CFProcessPath", "/Applications/X.app/Contents/MacOS/X")
    result = run_app()
    assert result.exit_code == 0, result.output
    assert execed == []
    assert result.stderr == ""
    assert "icon" not in webview.options
    assert "CFProcessPath" not in os.environ


def test_a_launcher_named_by_the_hidden_options(webview: FakeWebview, execed, tmp_path: Path):
    apps = tmp_path / "apps"
    item = launcher.launcher("Spartito Test", apps)
    launcher.install(item)
    result = run_app("--name", "Spartito Test", "--dir", str(apps))
    assert result.exit_code == 0, result.output
    assert execed[0][2]["CFProcessPath"] == str(
        apps / "Spartito Test.app/Contents/MacOS/Spartito Test"
    )


def test_linux_window_gets_icon_and_application_name(
    webview: FakeWebview, linux: Path, monkeypatch
):
    named = []
    monkeypatch.setattr(window, "BACKEND_PROBE", NOTE_GUI)
    monkeypatch.setattr(window, "name_qt_app", lambda: named.append(True))
    result = run_app()
    assert result.exit_code == 0, result.output
    assert named == [True]
    assert webview.options["icon"] == str(launcher.LINUX_ICONS["512x512"])
    assert "Dock" not in result.stderr
