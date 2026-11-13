"""`spartito --version`, `spartito version [--check]` and `spartito upgrade` (core/upgrade.py).

No test uses the real network: the check asks a fake index on 127.0.0.1, and an autouse
fixture records and refuses, at the socket level, every attempt to reach anything but this
machine; a test that leaves such a record fails, even when the code swallowed the refusal.
The install, the service, the launcher and the running window are faked as the other tests
fake them (conftest)."""

from __future__ import annotations

import contextlib
import fcntl
import inspect
import ipaddress
import json
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.request
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import metadata
from pathlib import Path
from typing import Any

import pytest
from starlette.testclient import TestClient
from test_code_view import FAKE_DOM, page_functions, run_node
from typer.testing import CliRunner

from orchestrator import __version__, autostart, cli, config
from orchestrator.core import upgrade
from orchestrator.gui import launcher, menubar, window
from orchestrator.models import Agent, Project, RunStatus
from orchestrator.service import Orchestrator
from orchestrator.web import serve
from orchestrator.web.app import app

CHECKOUT = "/home/dev/src/spartito"
REAL_OPENER = upgrade.OPENER  # the module's own, before a fixture replaces it
LOCAL_NAMES = {None, "", "localhost", b"localhost"}


def is_local(host: Any) -> bool:
    if host in LOCAL_NAMES:
        return True
    try:
        text = host.decode() if isinstance(host, bytes) else str(host)
        return ipaddress.ip_address(text.split("%", 1)[0]).is_loopback
    except ValueError:
        return False


@pytest.fixture(autouse=True)
def outside(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[str]]:
    """Every attempt of this test to reach anything but this machine, at the socket level (a
    name lookup, a connect: any opener, HTTP or HTTPS), recorded and refused. The record must
    be empty at the end, so a refusal the code swallowed still fails the test. No proxy is
    used, so a request cannot pass through one on this machine."""
    attempts: list[str] = []
    lookup, connect, connect_ex = (
        socket.getaddrinfo,
        socket.socket.connect,
        socket.socket.connect_ex,
    )

    def guarded_lookup(host: Any, *args: Any, **kwargs: Any) -> Any:
        if not is_local(host):
            attempts.append(f"lookup {host!r}")
            raise socket.gaierror(socket.EAI_NONAME, f"refused by the test: {host!r}")
        return lookup(host, *args, **kwargs)

    def guard(address: Any) -> None:
        if isinstance(address, tuple) and not is_local(address[0]):
            attempts.append(f"connect {address!r}")
            raise ConnectionRefusedError(f"refused by the test: {address!r}")

    def guarded_connect(self: socket.socket, address: Any) -> Any:
        guard(address)
        return connect(self, address)

    def guarded_connect_ex(self: socket.socket, address: Any) -> Any:
        guard(address)
        return connect_ex(self, address)

    monkeypatch.setattr(socket, "getaddrinfo", guarded_lookup)
    monkeypatch.setattr(socket.socket, "connect", guarded_connect)
    monkeypatch.setattr(socket.socket, "connect_ex", guarded_connect_ex)
    monkeypatch.setattr(urllib.request, "getproxies", lambda: {})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), autostart.NoRedirect)
    monkeypatch.setattr(upgrade, "OPENER", opener)  # the module's, without proxies
    yield attempts
    assert attempts == [], f"tried to reach another machine: {attempts}"


def invoke(*args: str):
    return CliRunner().invoke(cli.app, list(args), prog_name=config.CLI_NAME)


# -- the install ---------------------------------------------------------------------------------


class FakeDist:
    def __init__(self, direct_url: dict | None) -> None:
        self.direct_url = direct_url

    def read_text(self, name: str) -> str | None:
        assert name == "direct_url.json"
        return json.dumps(self.direct_url) if self.direct_url is not None else None


EDITABLE = {"url": f"file://{CHECKOUT}", "dir_info": {"editable": True}}


def editable(checkout: Path) -> dict:
    """The direct_url.json of an editable install of `checkout` (a space is %20 in it)."""
    return {"url": checkout.as_uri(), "dir_info": {"editable": True}}


@pytest.fixture
def env_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "env"
    path.mkdir()
    monkeypatch.setattr(upgrade, "environment", lambda: path)
    return path


def fake_install(
    monkeypatch: pytest.MonkeyPatch, env_dir: Path, direct_url: dict | None, extras: list | None
) -> None:
    """The distribution with `direct_url` (None: from an index); `extras` not None: a uv tool
    environment whose receipt names them."""
    monkeypatch.setattr(upgrade, "distribution", lambda: FakeDist(direct_url))
    receipt = env_dir / "uv-receipt.toml"
    receipt.unlink(missing_ok=True)
    if extras is not None:
        listed = ", ".join(f'"{e}"' for e in extras)
        receipt.write_text(
            f'[tool]\nrequirements = [{{ name = "spartito", extras = [{listed}] }}]\n'
            'entrypoints = [{ name = "spartito", install-path = "/x/bin/spartito" }]\n'
        )


def test_dash_dash_version_prints_the_version_and_exits() -> None:
    result = invoke("--version")
    assert result.exit_code == 0
    assert result.stdout == f"spartito {__version__}\n"


def test_version_names_an_editable_checkout_with_its_directory(
    monkeypatch: pytest.MonkeyPatch, env_dir: Path
) -> None:
    fake_install(monkeypatch, env_dir, EDITABLE, ["app", "menubar"])
    result = invoke("version")
    assert result.exit_code == 0
    assert result.stdout == (f"spartito {__version__}\ninstall: editable checkout ({CHECKOUT})\n")


@pytest.mark.parametrize(
    ("extras", "shown"), [(["app"], " (uv tool)"), (None, "")], ids=["uv-tool", "pip"]
)
def test_version_names_an_installed_package(
    monkeypatch: pytest.MonkeyPatch, env_dir: Path, extras: list | None, shown: str
) -> None:
    fake_install(monkeypatch, env_dir, None, extras)
    result = invoke("version")
    assert result.exit_code == 0
    assert result.stdout == f"spartito {__version__}\ninstall: installed package{shown}\n"


def test_version_opens_no_database() -> None:
    assert invoke("version").exit_code == 0
    assert not config.db_path().exists()


def test_the_install_of_this_checkout_is_editable() -> None:
    """The suite runs from a `uv sync` of the checkout: an editable install, no uv tool."""
    found = upgrade.install()
    assert found.kind == "editable" and not found.tool
    assert Path(found.checkout or "").resolve() == Path(__file__).resolve().parent.parent
    assert upgrade.commands(found)[0][-1] == "uv sync"  # its own .venv


def test_without_the_distribution_the_install_is_unknown(monkeypatch: pytest.MonkeyPatch) -> None:
    def missing():
        raise metadata.PackageNotFoundError("spartito")

    monkeypatch.setattr(upgrade, "distribution", missing)
    assert upgrade.install().label == "not installed as the distribution spartito"
    lines, notes = upgrade.commands(upgrade.install())
    assert lines == [] and "upgrading-to-the-distribution-name-spartito" in notes[0]


# -- the dashboard -------------------------------------------------------------------------------


def test_the_state_holds_the_version_and_ping_nothing_new(orch: Orchestrator) -> None:
    with TestClient(app, base_url="http://localhost") as client:  # with the app's startup
        state = client.get("/api/state").json()
        ping = client.get("/api/ping").json()
    assert state["version"] == __version__
    assert set(ping) == {"tool", "version", "home"}
    assert str(Path(__file__).resolve().parent.parent) not in json.dumps(state)


def test_the_settings_show_the_version_as_text() -> None:
    source = f"const CLI = {json.dumps(config.CLI_NAME)};\n" + page_functions("el", "versionBox")
    code = (
        FAKE_DOM
        + source
        + (
            "\nprocess.stdout.write(JSON.stringify(["
            "dump(versionBox({project_name: 'Spartito', version: '0.2.0<b>'})),"
            " dump(versionBox({}))]));"
        )
    )
    shown, unknown = run_node(code)
    assert shown["children"][0]["text"] == "Version"
    assert shown["children"][1]["text"] == (
        f"Spartito 0.2.0<b>. `{config.CLI_NAME} version --check` asks PyPI for a newer release;"
        f" `{config.CLI_NAME} upgrade` prepares an upgrade. Nothing checks on its own."
    )
    assert unknown["children"][1]["text"].startswith("Spartito unknown.")


# -- the check -----------------------------------------------------------------------------------


class FakeIndex:
    """A tiny index on 127.0.0.1: answers each path from `answers` (status, body, headers,
    delay) and notes every request (path, headers)."""

    def __init__(self) -> None:
        self.answers: dict[str, tuple[int, bytes, dict[str, str], float]] = {}
        self.requests: list[tuple[str, dict[str, str]]] = []
        index = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                index.requests.append((self.path, dict(self.headers.items())))
                status, body, headers, delay = index.answers.get(
                    self.path, (404, b'{"message": "Not Found"}', {}, 0.0)
                )
                time.sleep(delay)
                try:
                    self.send_response(status)
                    for name, value in headers.items():
                        self.send_header(name, value)
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                except OSError:
                    pass  # the client gave up (the timeout case)

            def log_message(self, *args: Any) -> None:
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.server.block_on_close = False
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def release(self, version: str, status: int = 200, delay: float = 0.0) -> None:
        body = json.dumps({"info": {"name": "spartito", "version": version}, "releases": {}})
        self.answer(status, body.encode(), delay=delay)

    def answer(
        self, status: int, body: bytes, headers: dict | None = None, delay: float = 0.0
    ) -> None:
        self.answers["/pypi/spartito/json"] = (status, body, headers or {}, delay)

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def index(monkeypatch: pytest.MonkeyPatch) -> Iterator[FakeIndex]:
    """The fake index in place of PyPI. The opener is the module's own, without the proxies of
    the environment or the system, so the request reaches 127.0.0.1."""
    fake = FakeIndex()
    monkeypatch.setattr(upgrade, "INDEX", f"{fake.url}/pypi")
    monkeypatch.setattr(
        upgrade,
        "OPENER",
        urllib.request.build_opener(urllib.request.ProxyHandler({}), autostart.NoRedirect),
    )
    yield fake
    fake.close()


def test_the_opener_follows_no_redirect() -> None:
    handlers = [type(h) for h in REAL_OPENER.handlers]
    assert autostart.NoRedirect in handlers
    assert urllib.request.HTTPRedirectHandler not in handlers
    assert urllib.request.HTTPCookieProcessor not in handlers


@pytest.mark.parametrize(
    ("latest", "status", "line"),
    [
        ("0.2.0", "newer", "a newer release is on PyPI: 0.2.0 (installed: 0.1.0);"
         " `spartito upgrade` prepares the upgrade"),
        ("0.1.0", "current", "up to date: 0.1.0 is the latest release on PyPI"),
        ("0.0.9", "ahead", "0.1.0 is newer than the latest release on PyPI (0.0.9)"),
        ("0.1.0.post1", "newer", "a newer release is on PyPI: 0.1.0.post1 (installed: 0.1.0);"
         " `spartito upgrade` prepares the upgrade"),
        ("0.1.0+local.7", "current", "up to date: 0.1.0 is the latest release on PyPI"),
    ],
)  # fmt: skip
def test_the_check_compares_with_the_latest_release(
    index: FakeIndex, monkeypatch: pytest.MonkeyPatch, latest: str, status: str, line: str
) -> None:
    index.release(latest)
    assert upgrade.check("0.1.0")["status"] == status
    monkeypatch.setattr(upgrade, "__version__", "0.1.0")
    result = invoke("version", "--check")
    assert result.exit_code == 0
    assert result.stdout.splitlines()[-1] == line


def test_not_published_is_an_answer_not_an_error(index: FakeIndex) -> None:
    result = invoke("version", "--check")  # nothing published: the index answers 404
    assert result.exit_code == 0
    assert result.stdout.splitlines()[-1] == (
        f"spartito is not published on PyPI (installed: {__version__})"
    )
    assert upgrade.check("0.1.0") == {
        "status": "unpublished",
        "installed": "0.1.0",
        "latest": None,
        "name": "spartito",
    }


def test_the_request_carries_only_the_name_and_a_fixed_user_agent(index: FakeIndex) -> None:
    index.release("0.1.0")
    upgrade.check("0.1.0")
    ((path, headers),) = index.requests
    assert path == "/pypi/spartito/json"
    assert headers["User-Agent"] == "spartito"
    port = index.url.rsplit(":", 1)[1]
    assert headers == {
        "Accept-Encoding": "identity",
        "Host": f"127.0.0.1:{port}",
        "User-Agent": "spartito",
        "Connection": "close",
    }


@pytest.mark.parametrize(
    ("answer", "message"),
    [
        ((500, b"oops", {}), "error: PyPI answered HTTP 500"),
        ((200, b"<html>not json</html>", {}), "error: PyPI's answer was not understood"),
        ((200, b'{"info": {}}', {}), "error: PyPI's answer was not understood"),
        ((200, b'{"info": {"version": "latest"}}', {}), "error: PyPI's answer was not understood"),
        ((302, b"", {"Location": "http://127.0.0.2:9/elsewhere"}), "error: PyPI answered with a redirect (HTTP 302)"),
    ],
    ids=["500", "not-json", "no-version", "no-numbers", "redirect"],
)  # fmt: skip
def test_a_broken_answer_is_a_one_line_error(index: FakeIndex, answer: tuple, message: str) -> None:
    index.answer(*answer)
    result = invoke("version", "--check")
    assert result.exit_code == 1
    assert isinstance(result.exception, SystemExit)  # no traceback
    (line,) = result.stderr.splitlines()
    assert line.startswith(message)
    assert len(index.requests) == 1  # a redirect is not followed


def test_a_too_large_answer_is_cut_off(index: FakeIndex, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(upgrade, "READ_LIMIT", 64)
    index.answer(200, b'{"info": {"version": "0.1.0"}, "pad": "' + b"x" * 100 + b'"}')
    result = invoke("version", "--check")
    assert result.exit_code == 1
    assert result.stderr == (
        "error: PyPI's answer was not understood: it is larger than 64 bytes\n"
    )


def test_a_timeout_is_a_one_line_error(index: FakeIndex, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(upgrade, "TIMEOUT", 0.2)
    index.release("0.2.0", delay=1.0)
    result = invoke("version", "--check")
    assert result.exit_code == 1 and isinstance(result.exception, SystemExit)
    assert result.stderr == "error: could not reach PyPI: timed out\n"


def test_an_unreachable_index_is_a_one_line_error(
    index: FakeIndex, monkeypatch: pytest.MonkeyPatch
) -> None:
    index.close()  # nothing listens on its port any more
    result = invoke("version", "--check")
    assert result.exit_code == 1 and isinstance(result.exception, SystemExit)
    (line,) = result.stderr.splitlines()
    assert line.startswith("error: could not reach PyPI: ")


@pytest.mark.parametrize(
    ("a", "b", "order"),
    [
        ("0.10.0", "0.9.1", 1),
        ("1.0", "1.0.0", 0),
        ("0.2.0", "0.2.0rc1", 1),
        ("0.2.0.dev3", "0.2.0", -1),
        ("0.2.0.post1", "0.2.0", 1),
        ("0.2.0.post2", "0.2.0.post1", 1),
        ("0.2.0-1", "0.2.0.post1", 0),
        ("0.2.0rc2", "0.2.0rc1", 1),
        ("0.2.0rc1", "0.2.0b3", 1),
        ("0.2.0b1", "0.2.0a9", 1),
        ("0.2.0a1", "0.2.0.dev9", 1),
        ("0.2.0rc1", "0.2.0rc1.dev2", 1),
        ("0.2.0.post1", "0.2.0.post1.dev1", 1),
        ("0.2.0.post1.dev1", "0.2.0", 1),
        ("0.2.0.RC1", "0.2.0rc1", 0),
        ("0.2.0-alpha.1", "0.2.0a1", 0),
        ("0.2.0+abc.1", "0.2.0", 0),
        ("v0.2.0", "0.2.0", 0),
        ("0.1.1rc1", "0.1.0", 1),
    ],
)
def test_versions_compare_by_their_release_numbers(a: str, b: str, order: int) -> None:
    ka, kb = upgrade.release_key(a), upgrade.release_key(b)
    assert ka is not None and kb is not None
    assert (ka > kb) - (ka < kb) == order


@pytest.mark.parametrize("version", ["latest", "0.2.0\x1b[31m", "0.2.0 rc1", "0.2.0+"])
def test_what_is_not_a_plain_version_has_no_key(version: str) -> None:
    assert upgrade.release_key(version) is None


def test_a_later_release_candidate_is_newer(index: FakeIndex) -> None:
    index.release("0.2.0rc2")
    assert upgrade.check("0.2.0rc1")["status"] == "newer"


def test_an_unknown_installed_version_is_said(index: FakeIndex) -> None:
    index.release("0.2.0")
    assert upgrade.check("0+unknown")["status"] == "unknown"


# -- no request otherwise ------------------------------------------------------------------------


def test_the_guard_records_a_request_whose_error_is_swallowed(outside: list[str]) -> None:
    """The shape of a "best effort" check: the refusal is swallowed, the record stays."""
    with contextlib.suppress(Exception):
        upgrade.check()
    with contextlib.suppress(Exception):  # another opener, plain HTTP
        urllib.request.urlopen("http://example.org/", timeout=1)
    assert outside == ["lookup 'pypi.org'", "lookup 'example.org'"]
    outside.clear()  # seen: the fixture's own check at the end stays meaningful


def test_nothing_else_makes_a_request(
    orch: Orchestrator, monkeypatch: pytest.MonkeyPatch, env_dir: Path, outside: list[str]
) -> None:
    fake_install(monkeypatch, env_dir, None, ["app"])
    for args in (["--version"], ["version"], ["upgrade", "--dry-run"], ["upgrade"]):
        assert invoke(*args).exit_code == 0, args
    with TestClient(app, base_url="http://localhost") as client:
        assert client.get("/api/state").status_code == 200
        assert client.get("/api/ping").status_code == 200
    server = serve.make("127.0.0.1", 0)  # a real server start, asked over the loopback
    thread = threading.Thread(target=serve.serve, args=(server,), daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.ready and thread.is_alive() and time.monotonic() < deadline:
        time.sleep(0.02)
    try:
        assert server.ready
        base = f"http://127.0.0.1:{server.record['port']}"
        local = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        for path in ("/api/state", "/api/ping"):
            with local.open(base + path, timeout=5) as answer:
                assert answer.status == 200
    finally:
        server.should_exit = True
        thread.join(10)
    assert outside == []


# -- upgrade -------------------------------------------------------------------------------------


@pytest.fixture
def o() -> Orchestrator:
    orch = Orchestrator()
    orch.store.add_project(Project("my-app", "/src/my-app", "main"))
    orch.store.add_agent(
        Agent(id="fix-login-3f2a", project="my-app", name="fix-login-3f2a", role="worker",
              provider="claude", cwd="/w/fix-login-3f2a")
    )  # fmt: skip
    return orch


def prompts(orch: Orchestrator) -> list[str]:
    return [run.prompt for run in orch.store.runs_of("fix-login-3f2a")]


@pytest.mark.parametrize("status", [RunStatus.RUNNING, RunStatus.WAITING])
def test_upgrade_refuses_while_a_turn_is_running_or_queued(
    o: Orchestrator, status: RunStatus
) -> None:
    run = o.store.add_run("fix-login-3f2a", "busy")
    o.store.update_run(run.id, status=status, pid=os.getpid() if status == "running" else None)

    result = invoke("upgrade")
    assert result.exit_code == 1
    assert result.stderr == (
        f"error: not prepared: turns are running or queued (fix-login-3f2a run {run.id}"
        f" {status.value}); wait for them, or `spartito stop` the agents, then run"
        " `spartito upgrade` again\n"
    )
    assert result.stdout == ""
    assert not config.backups_dir().exists()

    dry = invoke("upgrade", "--dry-run")  # lists them instead, and makes no backup either
    assert dry.exit_code == 0
    assert f"active: fix-login-3f2a run {run.id} {status.value}" in dry.stdout
    assert not config.backups_dir().exists()


def test_a_turn_whose_runner_is_gone_does_not_block_an_upgrade(o: Orchestrator) -> None:
    run = o.store.add_run("fix-login-3f2a", "orphan")
    gone = subprocess.Popen([sys.executable, "-c", "pass"])
    gone.wait()
    o.store.update_run(run.id, status=RunStatus.RUNNING, pid=gone.pid)
    assert invoke("upgrade").exit_code == 0


def test_upgrade_makes_a_listed_restorable_backup(o: Orchestrator) -> None:
    done = o.store.add_run("fix-login-3f2a", "before the upgrade")
    o.store.finish_run(done.id, RunStatus.DONE, result="ok")

    result = invoke("upgrade")
    assert result.exit_code == 0
    (name,) = [item["name"] for item in o.list_backups()]
    path = config.backups_dir() / name
    assert f"backup: {path}\n  to go back: `spartito state restore {name} --yes`\n" in (
        result.stdout
    )
    assert "warning: a backup holds prompts" in result.stderr

    later = o.store.add_run("fix-login-3f2a", "after the upgrade")
    o.store.finish_run(later.id, RunStatus.DONE, result="ok")
    o.restore_state(name, confirm=True)
    assert prompts(o) == ["before the upgrade"]


def test_a_dry_run_makes_no_backup(o: Orchestrator) -> None:
    result = invoke("upgrade", "--dry-run")
    assert result.exit_code == 0
    assert f"backup: none (dry run); without --dry-run one goes into {config.backups_dir()}\n" in (
        result.stdout
    )
    assert o.list_backups() == [] and not config.backups_dir().exists()


# -- the plan ------------------------------------------------------------------------------------


@pytest.fixture
def platform(monkeypatch: pytest.MonkeyPatch) -> str:
    """The platform the service is looked for on: this one when supported, as in the CI."""
    kind = "darwin" if sys.platform == "darwin" else "linux"
    monkeypatch.setattr(autostart, "this_platform", lambda: kind)
    return kind


def install_service(platform: str, port: int) -> None:
    """This home's service unit, as `spartito service install` writes it (nothing loaded)."""
    svc = autostart.service(port=port)
    unit = autostart.unit_path(svc.label, autostart.platform_kind(platform))
    unit.parent.mkdir(parents=True, exist_ok=True)
    if platform == "darwin":
        unit.write_bytes(autostart.render_plist(svc))
    else:
        unit.write_text(autostart.render_unit(svc))


def hold(name: str):
    """Hold the lock `name` of this home's run/, as `spartito app` and the menu bar do."""
    handle = (config.runtime_dir() / name).open("a")
    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    return handle


def record_server(port: int, pid: int | None = None) -> None:
    record = {"pid": pid or os.getpid(), "host": "127.0.0.1", "port": port, "started": "x"}
    serve.record_path().write_text(json.dumps(record))


UNSEEN = (
    "not visible from here, if you run them:\n"
    "  - an open `spartito chat`: leave it, and continue with `spartito chat --continue`"
    " afterwards\n"
    "  - a `spartito web` started by hand that is not listed here (only the last dashboard"
    " server started is recorded): stop it and start it again\n"
    "check the new version with `spartito version`.\n"
)


def test_the_plan_of_a_uv_tool_package_with_nothing_running(
    o: Orchestrator, monkeypatch: pytest.MonkeyPatch, env_dir: Path, platform: str
) -> None:
    fake_install(monkeypatch, env_dir, None, ["app", "menubar"])
    result = invoke("upgrade", "--dry-run")
    assert result.exit_code == 0
    assert result.stdout == (
        f"backup: none (dry run); without --dry-run one goes into {config.backups_dir()}\n"
        f"install: spartito {__version__}, installed package\n"
        "upgrade it with:\n"
        "  uv tool upgrade spartito\n"
        "  note: `uv tool upgrade` keeps the install's extras and Python (with the extras app,"
        " menubar)\n"
        "then restart: nothing of this home runs that needs it\n" + UNSEEN
    )


def test_the_plan_of_a_pip_package(
    o: Orchestrator, monkeypatch: pytest.MonkeyPatch, env_dir: Path, platform: str
) -> None:
    fake_install(monkeypatch, env_dir, None, None)
    monkeypatch.setattr(upgrade, "python", lambda: "/opt/my python/bin/python3")
    lines = invoke("upgrade", "--dry-run").stdout.splitlines()
    assert "  '/opt/my python/bin/python3' -m pip install --upgrade spartito" in lines
    assert "  note: with the extras you installed, for example 'spartito[app,menubar]'" in lines


def test_the_plan_of_a_package_from_a_url(
    o: Orchestrator, monkeypatch: pytest.MonkeyPatch, env_dir: Path, platform: str
) -> None:
    url = "file:///home/dev/dist/spartito-0.1.0-py3-none-any.whl"
    fake_install(monkeypatch, env_dir, {"url": url, "archive_info": {}}, ["app"])
    lines = invoke("upgrade", "--dry-run").stdout.splitlines()
    assert "upgrade it with:" not in lines
    assert f"  note: installed from {url}, not from PyPI: install it again from there," in (
        "\n".join(lines)
    )


def test_the_plan_of_an_editable_uv_tool_with_everything_running(
    o: Orchestrator, monkeypatch: pytest.MonkeyPatch, env_dir: Path, platform: str
) -> None:
    fake_install(monkeypatch, env_dir, EDITABLE, ["app", "menubar"])
    monkeypatch.setattr(launcher, "installed", lambda item: True)
    install_service(platform, 18891)
    (config.runtime_dir() / window.PORT_FILE).write_text("18892\n")
    record_server(18892)  # the window's own server
    with hold("app.lock"), hold("menubar.lock"):
        result = invoke("upgrade")
    assert result.exit_code == 0
    label = autostart.default_label(autostart.home())
    after = result.stdout.split("install: ", 1)[1]
    assert after == (
        f"spartito {__version__}, editable checkout ({CHECKOUT})\n"
        "upgrade it with:\n"
        f"  cd {CHECKOUT}\n"
        "  git pull    # or update the checkout's branch as you do\n"
        "  uv tool install --editable '.[app,menubar]'\n"
        "  note: the `uv tool install` line reinstalls the checkout with the extras it has now\n"
        "then restart:\n"
        f"  - the background service ({label}): `spartito service install --host 127.0.0.1"
        " --port 18891` restarts it with the new code\n"
        "  - the `spartito app` window: quit it and open it again\n"
        "  - the menu-bar item: quit it and start `spartito menubar` again\n"
        "  - the launcher: run `spartito app install` again (it records the version and the"
        " Python of the install)\n" + UNSEEN
    )


def test_the_plan_of_a_synced_checkout_with_a_hand_started_server(
    o: Orchestrator, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, platform: str
) -> None:
    checkout = tmp_path / "my app"  # `uv run` in it: the environment is its own .venv
    (checkout / ".venv").mkdir(parents=True)
    fake_install(monkeypatch, checkout / ".venv", editable(checkout), None)
    monkeypatch.setattr(upgrade, "environment", lambda: checkout / ".venv")
    record_server(18893)
    lines = invoke("upgrade", "--dry-run").stdout.splitlines()
    assert lines[lines.index("upgrade it with:") + 1 :][:4] == [
        f"  cd '{checkout}'",
        "  git pull    # or update the checkout's branch as you do",
        "  uv sync",
        "  note: add the extras you synced with, for example `uv sync --extra app --extra"
        " menubar` (CONTRIBUTING.md)",
    ]
    assert lines[lines.index("then restart:") + 1] == (
        f"  - the dashboard server on port 18893 (pid {os.getpid()}), a `spartito web` started"
        " by hand: stop it (Ctrl-C) and start it again"
    )


@pytest.mark.parametrize(
    ("pip", "install"),
    [
        (True, "  '/opt/my python/bin/python3' -m pip install -e ."),
        (False, "  uv pip install --python '/opt/my python/bin/python3' -e ."),
    ],
    ids=["pip", "no-pip"],
)
def test_the_plan_of_an_editable_install_into_another_environment(
    o: Orchestrator,
    monkeypatch: pytest.MonkeyPatch,
    env_dir: Path,
    platform: str,
    pip: bool,
    install: str,
) -> None:
    """Neither a uv tool nor the checkout's own .venv: `uv sync` would sync the wrong
    environment, so the checkout is installed again into the Python that runs this."""
    fake_install(monkeypatch, env_dir, editable(Path("/home/dev/src/my app")), None)
    monkeypatch.setattr(upgrade, "python", lambda: "/opt/my python/bin/python3")
    monkeypatch.setattr(upgrade, "has_pip", lambda: pip)
    lines = invoke("upgrade", "--dry-run").stdout.splitlines()
    assert lines[lines.index("upgrade it with:") + 1 :][:4] == [
        "  cd '/home/dev/src/my app'",
        "  git pull    # or update the checkout's branch as you do",
        install,
        "  note: with the extras you installed, for example `-e '.[app,menubar]'`",
    ]
    assert "  uv sync" not in lines


def test_the_service_server_and_dead_records_are_not_listed_twice(
    o: Orchestrator, monkeypatch: pytest.MonkeyPatch, env_dir: Path, platform: str
) -> None:
    fake_install(monkeypatch, env_dir, None, [])
    install_service(platform, 18891)
    record_server(18891)  # the service's own server: restarted with it
    items, _ = upgrade.restarts(launcher=False)
    assert len(items) == 1 and items[0].startswith("the background service")

    gone = subprocess.Popen([sys.executable, "-c", "pass"])
    gone.wait()
    record_server(18893, gone.pid)  # a server that is gone left its record
    assert upgrade.restarts(launcher=False)[0] == items


def test_a_released_lock_is_not_running(o: Orchestrator) -> None:
    assert not upgrade.lock_held("app.lock")  # never created
    assert not (config.home() / "run" / "app.lock").exists()
    hold("app.lock").close()
    assert not upgrade.lock_held("app.lock")
    with hold("app.lock"):
        assert upgrade.lock_held("app.lock")


def test_the_file_names_are_those_of_the_processes() -> None:
    assert serve.record_path().name == upgrade.WEB_RECORD
    assert window.PORT_FILE == upgrade.APP_PORT
    assert inspect.signature(window.single_instance).parameters["name"].default == (
        upgrade.APP_LOCK
    )
    assert f'single_instance("{upgrade.MENUBAR_LOCK}")' in inspect.getsource(menubar.run)


def test_the_help_names_the_new_commands() -> None:
    assert "--version" in invoke("--help").stdout
    assert "--check" in invoke("version", "--help").stdout
    assert "--dry-run" in invoke("upgrade", "--help").stdout
    assert "spartito state backup" in invoke("upgrade", "--help").stdout
