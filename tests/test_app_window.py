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

from orchestrator import autostart, cli, config
from orchestrator.gui import locate, window
from orchestrator.web import serve
from orchestrator.web.app import LOOPBACK_HOSTS
from orchestrator.web.app import app as web_app

PACKAGE = Path(cli.__file__).parent
NATIVE_MISSING = (
    "no native window: pywebview is not installed (install the extra orchestrator[app]);"
    " trying a Chromium-family browser in app mode"
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
    opened: list[str] = []
    monkeypatch.setattr(window.webbrowser, "open", lambda url: opened.append(url) or True)
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


def test_orch_web_process_records_and_forgets_itself():
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
        proc.send_signal(signal.SIGINT)
        out, _ = proc.communicate(timeout=20)
    assert proc.returncode == 0
    assert out == f"web UI: http://127.0.0.1:{port}\n"
    assert not serve.record_path().exists()


# -- locate --------------------------------------------------------------------------------


class Pinged(BaseHTTPRequestHandler):
    answer: bytes = b""

    def do_GET(self) -> None:
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

    def answer(body: dict, host: str = "127.0.0.1") -> int:
        Pinged.answer = json.dumps(body).encode()
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
        self.events = types.SimpleNamespace(closed=Event())
        self.destroyed = threading.Event()

    def destroy(self) -> None:
        self.destroyed.set()


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

    def create_window(self, title: str, url: str, **options) -> FakeWindow:
        self.windows.append(FakeWindow(title, url, **options))
        return self.windows[-1]

    def start(self, func=None, *args, **options) -> None:
        if self.fail:
            raise self.fail
        self.options = options
        self.main_thread = threading.current_thread() is threading.main_thread()
        win = self.windows[-1]
        self.ping = get_json(win.url + "api/ping")
        if func:
            threading.Thread(target=func, daemon=True).start()
        win.destroyed.wait(0.5)
        win.events.closed.fire()


@pytest.fixture
def webview(monkeypatch: pytest.MonkeyPatch) -> FakeWebview:
    fake = FakeWebview()
    monkeypatch.setitem(sys.modules, "webview", fake)
    return fake


CHROMIUM = """#!{python}
import os, signal, sys, time, urllib.request

log = os.environ["FAKE_CHROMIUM_LOG"]
with open(log, "w") as out:
    out.write("\\n".join(sys.argv[1:]) + "\\n")
url = next(a for a in sys.argv[1:] if a.startswith("--app=")).removeprefix("--app=")
with urllib.request.urlopen(url + "api/ping", timeout=5) as resp, open(log + ".ping", "wb") as out:
    out.write(resp.read())


def ended(*_):
    with open(log, "a") as out:
        out.write("TERM\\n")
    sys.exit(0)


signal.signal(signal.SIGTERM, ended)
while os.environ.get("FAKE_CHROMIUM_STAYS"):
    time.sleep(0.05)
"""


@pytest.fixture
def chromium(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A fake Google Chrome bundle in a fake ~/Applications; returns the log of its argv.
    It exits at once (the window closed), or with FAKE_CHROMIUM_STAYS when terminated."""
    apps = tmp_path / "Applications"
    binary = apps / "Google Chrome.app" / "Contents" / "MacOS" / "Google Chrome"
    binary.parent.mkdir(parents=True)
    binary.write_text(CHROMIUM.format(python=sys.executable))
    binary.chmod(0o755)
    monkeypatch.setattr(window, "mac_app_dirs", lambda: [tmp_path / "missing", apps])
    log = tmp_path / "chromium.log"
    monkeypatch.setenv("FAKE_CHROMIUM_LOG", str(log))
    return log


def run_app(*args: str):
    return CliRunner().invoke(cli.app, ["app", *args])


def assert_own_server_gone(url: str) -> None:
    assert url.startswith("http://127.0.0.1:")
    assert not listening(port_of(url))
    assert not serve.record_path().exists()


def test_native_window_on_an_own_server(webview: FakeWebview):
    result = run_app()
    assert result.exit_code == 0, result.output
    assert result.stderr == ""
    (win,) = webview.windows
    assert result.stdout == f"web UI: {win.url}\n"
    assert win.title == config.PROJECT_NAME
    assert win.options["text_select"] is True
    assert webview.main_thread
    assert webview.ping["home"] == this_home()
    assert webview.options["private_mode"] is False
    assert not win.destroyed.is_set()  # the user closed it
    assert_own_server_gone(win.url)


def test_native_window_closes_itself_after_the_given_time(webview: FakeWebview):
    result = run_app("--close-after", "0.1")
    assert result.exit_code == 0, result.output
    (win,) = webview.windows
    assert win.destroyed.is_set()
    assert_own_server_gone(win.url)


def test_own_server_on_a_given_port(webview: FakeWebview):
    port = free_port()
    result = run_app("--port", str(port))
    assert result.exit_code == 0, result.output
    assert webview.windows[0].url == f"http://127.0.0.1:{port}/"
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
        assert webview.windows[0].url == url
        assert listening(port_of(url))
        assert locate.running_server() == url
    finally:
        stopped(server, thread)


def test_server_of_another_home_is_not_reused(webview: FakeWebview, pinged):
    foreign = pinged({"tool": "orchestrator", "home": "0123456789abcdef"})
    result = run_app()
    assert result.exit_code == 0, result.output
    url = webview.windows[0].url
    assert port_of(url) != foreign
    assert webview.ping["home"] == this_home()
    assert_own_server_gone(url)


def test_chromium_app_mode_when_pywebview_is_missing(chromium: Path):
    result = run_app()
    assert result.exit_code == 0, result.output
    assert result.stderr == NATIVE_MISSING + "\n"
    url = result.stdout.removeprefix("web UI: ").strip()
    assert chromium.read_text().splitlines() == [
        f"--app={url}",
        f"--user-data-dir={config.runtime_dir() / 'app-profile'}",
        "--no-first-run",
        "--no-default-browser-check",
    ]
    assert json.loads(Path(f"{chromium}.ping").read_text())["home"] == this_home()
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


def test_chromium_is_ended_after_the_given_time(chromium: Path, monkeypatch):
    monkeypatch.setenv("FAKE_CHROMIUM_STAYS", "1")
    started_at = time.monotonic()
    result = run_app("--close-after", "1")
    assert result.exit_code == 0, result.output
    assert time.monotonic() - started_at < 10
    assert chromium.read_text().splitlines()[-1] == "TERM"
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
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-0")
    assert run_app().exit_code == 0
    assert len(webview.windows) == 1


def test_one_app_per_home(webview: FakeWebview):
    lock = window.single_instance()
    assert lock is not None
    try:
        result = run_app()
    finally:
        lock.close()
    assert result.exit_code == 0
    assert result.stdout == "the dashboard window of this home is already open\n"
    assert webview.windows == []
    assert not serve.record_path().exists()
    assert run_app().exit_code == 0  # released
    assert len(webview.windows) == 1


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
