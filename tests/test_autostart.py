"""`orch service`: the dashboard as a launchd agent or systemd user unit.

Every system command is a fake (conftest.fake_system) and HOME is temporary; the platform
is passed in or patched, so both paths run on any host.
"""

from __future__ import annotations

import hashlib
import json
import os
import plistlib
import pwd
import shutil
import socket
import subprocess
import sys
import threading
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from conftest import SYSTEM_COMMANDS, FakeSystem
from typer.testing import CliRunner

from orchestrator import autostart, cli

UID = str(os.getuid())

GOLDEN_PLIST = """\
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
	<key>Label</key>
	<string>local.spartito.web</string>
	<key>ProgramArguments</key>
	<array>
		<string>/opt/py/bin/python3</string>
		<string>-m</string>
		<string>orchestrator</string>
		<string>web</string>
		<string>--host</string>
		<string>127.0.0.1</string>
		<string>--port</string>
		<string>8787</string>
	</array>
	<key>EnvironmentVariables</key>
	<dict>
		<key>LANG</key>
		<string>en_US.UTF-8</string>
		<key>ORCH_HOME</key>
		<string>/srv/orch</string>
		<key>PATH</key>
		<string>/opt/py/bin:/usr/bin:/bin</string>
	</dict>
	<key>WorkingDirectory</key>
	<string>/srv/orch</string>
	<key>RunAtLoad</key>
	<true/>
	<key>KeepAlive</key>
	<true/>
	<key>ProcessType</key>
	<string>Interactive</string>
	<key>StandardOutPath</key>
	<string>/srv/orch/logs/web.log</string>
	<key>StandardErrorPath</key>
	<string>/srv/orch/logs/web.log</string>
</dict>
</plist>
"""

GOLDEN_UNIT = """\
[Unit]
Description=Spartito dashboard (orch web)

[Service]
Type=exec
ExecStart=/opt/py/bin/python3 -m orchestrator web --host 127.0.0.1 --port 8787
WorkingDirectory=/srv/orch
Environment=LANG=en_US.UTF-8
Environment=ORCH_HOME=/srv/orch
Environment=PATH=/opt/py/bin:/usr/bin:/bin
Restart=on-failure
RestartSec=2

[Install]
WantedBy=default.target
"""


def neutral_service(**changes) -> autostart.Service:
    fields = {
        "label": "local.spartito.web",
        "argv": ["/opt/py/bin/python3", "-m", "orchestrator", "web"]
        + ["--host", "127.0.0.1", "--port", "8787"],
        "env": {
            "LANG": "en_US.UTF-8",
            "ORCH_HOME": "/srv/orch",
            "PATH": "/opt/py/bin:/usr/bin:/bin",
        },
        "home": Path("/srv/orch"),
        "log": Path("/srv/orch/logs/web.log"),
    }
    return autostart.Service(**(fields | changes))


@pytest.fixture
def on(monkeypatch: pytest.MonkeyPatch):
    """on("darwin") / on("linux"): the platform the code sees from now on."""
    return lambda platform: monkeypatch.setattr(autostart, "this_platform", lambda: platform)


def invoke(*args: str):
    return CliRunner().invoke(cli.app, ["service", *args])


def label() -> str:
    return autostart.default_label(autostart.home())


def plist_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{label()}.plist"


def unit_path() -> Path:
    return Path(os.environ["XDG_CONFIG_HOME"]) / "systemd" / "user" / f"{label()}.service"


# -- rendering ---------------------------------------------------------------------------


def test_the_plist_is_the_golden_one_and_a_valid_plist():
    rendered = autostart.render_plist(neutral_service())
    assert rendered.decode() == GOLDEN_PLIST
    parsed = plistlib.loads(rendered)
    assert parsed["KeepAlive"] is True and parsed["RunAtLoad"] is True
    assert parsed["ProgramArguments"][-4:] == ["--host", "127.0.0.1", "--port", "8787"]


def test_the_unit_is_the_golden_one():
    assert autostart.render_unit(neutral_service()) == GOLDEN_UNIT


def test_the_unit_quotes_spaces_specifiers_and_variables():
    svc = neutral_service(
        argv=["/opt/my py/python3", "-m", "orchestrator", "web", "--port", "1$2"],
        env={"ORCH_HOME": "/srv/my orch", "ORCH_PROVIDERS": 'x=50%$y"z'},
        home=Path("/srv/my orch"),
    )
    unit = autostart.render_unit(svc)
    assert 'ExecStart="/opt/my py/python3" -m orchestrator web --port 1$$2\n' in unit
    assert "WorkingDirectory=/srv/my orch\n" in unit
    assert 'Environment="ORCH_HOME=/srv/my orch"\n' in unit
    assert 'Environment="ORCH_PROVIDERS=x=50%%$y\\"z"\n' in unit


def test_the_executable_doubles_percent_but_not_dollar():
    """systemd expands variables in the arguments only: `$$` would stay in the path."""
    svc = neutral_service(argv=["/opt/a$b/50% py/python3", "-m", "orchestrator", "--x", "a$b%c"])
    unit = autostart.render_unit(svc)
    assert 'ExecStart="/opt/a$b/50%% py/python3" -m orchestrator --x a$$b%%c\n' in unit


def test_the_service_runs_this_interpreter_on_the_package():
    svc = autostart.service("127.0.0.1", 18787)
    assert svc.argv == [sys.executable, "-m", "orchestrator", "web"] + [
        "--host",
        "127.0.0.1",
        "--port",
        "18787",
    ]
    assert svc.log == autostart.home() / "logs" / "web.log"


def test_python_dash_m_runs_the_command_line():
    result = subprocess.run(
        [sys.executable, "-m", "orchestrator", "service", "--help"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "install" in result.stdout and "uninstall" in result.stdout


# -- label and environment -------------------------------------------------------------------


def test_the_default_home_has_the_plain_label(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ORCH_HOME", str(Path.home() / ".orchestrator"))
    assert label() == "local.spartito.web"
    monkeypatch.delenv("ORCH_HOME")
    assert label() == "local.spartito.web"


def test_another_home_has_a_label_of_its_own(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    first = label()
    digest = hashlib.sha256(str(autostart.home()).encode()).hexdigest()
    assert first == f"local.spartito-{digest[:8]}.web"
    monkeypatch.setenv("ORCH_HOME", str(tmp_path / "other"))
    assert label() not in (first, "local.spartito.web")


def test_only_whitelisted_variables_reach_the_unit():
    environ = {
        "PATH": "/opt/bin:/usr/bin",
        "LANG": "en_US.UTF-8",
        "LC_ALL": "en_US.UTF-8",
        "ORCH_PROVIDERS": "fake=mod:Cls",
        "ORCH_HOME": "/elsewhere",
        "CLAUDE_CONFIG_DIR": "/cfg/claude",
        "CODEX_HOME": "/cfg/codex",
        "ORCH_AGENT_ID": "worker-1",
        "ORCH_TURN": "7",
        "ANTHROPIC_API_KEY": "sk-secret",
        "GITHUB_TOKEN": "ghp-secret",
        "HOME": "/home/someone",
        "SSH_AUTH_SOCK": "/tmp/agent.sock",
    }
    env = autostart.captured_env(Path("/srv/orch"), environ)
    assert env == {
        "CLAUDE_CONFIG_DIR": "/cfg/claude",
        "CODEX_HOME": "/cfg/codex",
        "LANG": "en_US.UTF-8",
        "LC_ALL": "en_US.UTF-8",
        "ORCH_HOME": "/srv/orch",
        "ORCH_PROVIDERS": "fake=mod:Cls",
        "PATH": "/opt/bin:/usr/bin",
    }


@pytest.mark.parametrize("platform", ["darwin", "linux"])
def test_a_secret_in_the_environment_never_reaches_the_unit_file(
    platform: str, on, monkeypatch: pytest.MonkeyPatch
):
    on(platform)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-do-not-leak")
    monkeypatch.setenv("ORCH_EXTRA", "kept")
    assert invoke("install").exit_code == 0
    written = (plist_path() if platform == "darwin" else unit_path()).read_text()
    assert "sk-do-not-leak" not in written and "ANTHROPIC_API_KEY" not in written
    assert "ORCH_EXTRA" in written and os.environ["PATH"] in written


# -- launchd ---------------------------------------------------------------------------------


def test_install_on_macos_bootstraps_the_agent(on, fake_system: FakeSystem):
    on("darwin")
    result = invoke("install", "--port", "18787")
    assert result.exit_code == 0, result.output
    assert fake_system.calls() == [
        ["launchctl", "bootout", f"gui/{UID}/{label()}"],
        ["launchctl", "bootstrap", f"gui/{UID}", str(plist_path())],
    ]
    log = autostart.home() / "logs" / "web.log"
    assert f"unit: {plist_path()}" in result.output
    assert f"logs: {log}" in result.output
    assert plistlib.loads(plist_path().read_bytes())["StandardOutPath"] == str(log)


def test_install_twice_on_macos_leaves_one_agent_with_the_latest_options(
    on, fake_system: FakeSystem
):
    on("darwin")
    assert invoke("install", "--port", "18787").exit_code == 0
    fake_system.set("launchctl", "bootout", exit=0)  # loaded now; gone once booted out
    fake_system.clear()
    assert invoke("install", "--port", "18788").exit_code == 0
    assert fake_system.calls() == [
        ["launchctl", "bootout", f"gui/{UID}/{label()}"],
        ["launchctl", "print", f"gui/{UID}/{label()}"],
        ["launchctl", "bootstrap", f"gui/{UID}", str(plist_path())],
    ]
    assert os.listdir(plist_path().parent) == [plist_path().name]
    assert plistlib.loads(plist_path().read_bytes())["ProgramArguments"][-1] == "18788"


def test_a_failed_bootstrap_is_an_error(on, fake_system: FakeSystem):
    on("darwin")
    fake_system.set("launchctl", "bootstrap", exit=5, out="Bootstrap failed: 5: I/O error\n")
    result = invoke("install")
    assert result.exit_code == 1
    assert "launchctl bootstrap" in result.output and "Bootstrap failed" in result.output
    assert not plist_path().exists()  # nothing was installed before
    fake_system.set("launchctl", "bootstrap", exit=0)
    assert invoke("install", "--port", "18787").exit_code == 0
    fake_system.set("launchctl", "bootstrap", exit=5, out="Bootstrap failed: 5: I/O error\n")
    assert invoke("install", "--port", "18788").exit_code == 1
    assert plistlib.loads(plist_path().read_bytes())["ProgramArguments"][-1] == "18788"


def test_install_cuts_a_large_log_only(on):
    on("darwin")
    log = autostart.home() / "logs" / "web.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_bytes(b"x" * (autostart.LOG_LIMIT + 1))
    assert invoke("install").exit_code == 0
    assert log.stat().st_size == 0
    log.write_text("kept\n")
    assert invoke("install").exit_code == 0
    assert log.read_text() == "kept\n"


def test_uninstall_on_macos_boots_out_and_removes_the_plist(on, fake_system: FakeSystem):
    on("darwin")
    assert invoke("install").exit_code == 0
    fake_system.set("launchctl", "bootout", exit=0)
    fake_system.clear()
    result = invoke("uninstall")
    assert result.exit_code == 0
    assert fake_system.calls() == [["launchctl", "bootout", f"gui/{UID}/{label()}"]]
    assert not plist_path().exists()
    assert f"removed {label()}" in result.output


def test_uninstall_keeps_the_plist_of_an_agent_that_stays_loaded(on, fake_system: FakeSystem):
    on("darwin")
    assert invoke("install").exit_code == 0
    fake_system.set("launchctl", "bootout", exit=37, out="Boot-out failed: 37: in progress\n")
    fake_system.set("launchctl", "print", out="\tpid = 4242\n")  # still loaded
    fake_system.clear()
    result = invoke("uninstall")
    assert result.exit_code == 1
    assert result.output == f"error: could not unload {label()}: Boot-out failed: 37: in progress\n"
    assert fake_system.calls() == [
        ["launchctl", "bootout", f"gui/{UID}/{label()}"],
        ["launchctl", "print", f"gui/{UID}/{label()}"],
    ]
    assert plist_path().exists()


def test_uninstall_removes_the_plist_of_an_agent_that_was_not_loaded(on, fake_system: FakeSystem):
    on("darwin")
    assert invoke("install").exit_code == 0  # bootout and print fail: not loaded
    fake_system.clear()
    result = invoke("uninstall")
    assert result.exit_code == 0
    assert fake_system.calls() == [
        ["launchctl", "bootout", f"gui/{UID}/{label()}"],
        ["launchctl", "print", f"gui/{UID}/{label()}"],
    ]
    assert not plist_path().exists()
    assert result.output == f"removed {label()}: {plist_path()}\n"


@pytest.mark.parametrize("platform", ["darwin", "linux"])
def test_uninstall_without_a_service_says_so(platform: str, on):
    on(platform)
    result = invoke("uninstall")
    assert result.exit_code == 0
    assert result.output == f"{label()} is not installed\n"


def test_status_on_macos_reads_the_exit_code_and_the_pid(on, fake_system: FakeSystem):
    on("darwin")
    assert invoke("install", "--port", str(free_port())).exit_code == 0
    fake_system.set(
        "launchctl", "print", out=f"{label()} = {{\n\tstate = running\n\tpid = 4242\n}}\n"
    )
    fake_system.clear()
    result = invoke("status")
    assert result.exit_code == 0
    assert fake_system.calls() == [["launchctl", "print", f"gui/{UID}/{label()}"]]
    assert "state:   loaded, pid 4242" in result.output
    assert f"unit:    {plist_path()}\n" in result.output
    assert f"logs:    {autostart.home() / 'logs' / 'web.log'}" in result.output
    assert "nothing answers" in result.output


def test_status_without_a_service(on):
    on("darwin")
    result = invoke("status")
    assert result.exit_code == 0
    assert "(not installed)" in result.output and "state:   not loaded" in result.output
    assert "web UI" not in result.output


# -- systemd ---------------------------------------------------------------------------------


def test_install_on_linux_enables_the_unit(on, fake_system: FakeSystem):
    on("linux")
    result = invoke("install", "--port", "18787")
    assert result.exit_code == 0, result.output
    name = f"{label()}.service"
    assert fake_system.calls() == [
        ["systemctl", "--user", "is-active", name],
        ["systemctl", "--user", "daemon-reload"],
        ["systemctl", "--user", "enable", "--now", name],
    ]
    assert f"unit: {unit_path()}" in result.output
    assert f"logs: journalctl --user -u {name}" in result.output
    assert "--port 18787\n" in unit_path().read_text()


def test_install_twice_on_linux_restarts_with_the_latest_options(on, fake_system: FakeSystem):
    on("linux")
    assert invoke("install", "--port", "18787").exit_code == 0
    fake_system.set("systemctl", "is-active", out="active\n")
    fake_system.clear()
    assert invoke("install", "--port", "18788").exit_code == 0
    name = f"{label()}.service"
    assert fake_system.calls() == [
        ["systemctl", "--user", "is-active", name],
        ["systemctl", "--user", "daemon-reload"],
        ["systemctl", "--user", "enable", "--now", name],
        ["systemctl", "--user", "restart", name],
    ]
    assert os.listdir(unit_path().parent) == [unit_path().name]
    assert "--port 18788\n" in unit_path().read_text()


MANAGER_DOWN = "Failed to connect to bus: No medium found\n"


def test_an_install_without_a_user_manager_leaves_no_unit(on, fake_system: FakeSystem):
    on("linux")
    fake_system.set("systemctl", "daemon-reload", exit=1, out=MANAGER_DOWN)
    result = invoke("install")
    assert result.exit_code == 1
    assert result.output == (
        "error: `systemctl --user daemon-reload` failed (1): "
        "Failed to connect to bus: No medium found\n"
    )
    assert not unit_path().exists()
    assert "(not installed)" in invoke("status").output


def test_a_failed_reinstall_keeps_the_unit_it_wrote(on, fake_system: FakeSystem):
    on("linux")
    assert invoke("install", "--port", "18787").exit_code == 0
    fake_system.set("systemctl", "enable", exit=1, out="Failed to enable unit\n")
    assert invoke("install", "--port", "18788").exit_code == 1
    assert "--port 18788\n" in unit_path().read_text()


def test_uninstall_on_linux_disables_and_removes_the_unit(on, fake_system: FakeSystem):
    on("linux")
    assert invoke("install").exit_code == 0
    fake_system.clear()
    result = invoke("uninstall")
    assert result.exit_code == 0
    name = f"{label()}.service"
    assert fake_system.calls() == [
        ["systemctl", "--user", "disable", "--now", name],
        ["systemctl", "--user", "daemon-reload"],
    ]
    assert not unit_path().exists()


def test_status_on_linux_reads_active_and_the_main_pid(on, fake_system: FakeSystem):
    on("linux")
    assert invoke("install", "--port", str(free_port())).exit_code == 0
    fake_system.set("systemctl", "is-active", out="active\n")
    fake_system.set("systemctl", "show", out="4242\n")
    fake_system.clear()
    result = invoke("status")
    assert result.exit_code == 0
    name = f"{label()}.service"
    assert fake_system.calls() == [
        ["systemctl", "--user", "is-active", name],
        ["systemctl", "--user", "show", "--property=MainPID", "--value", name],
    ]
    assert "state:   active, pid 4242" in result.output
    assert f"logs:    journalctl --user -u {name}" in result.output


def test_logs_names_the_log(on):
    on("darwin")
    log = autostart.home() / "logs" / "web.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text("".join(f"line {n}\n" for n in range(30)))
    result = invoke("logs", "-n", "2")
    assert result.output == f"{log}\nline 28\nline 29\n"
    on("linux")
    assert invoke("logs").output == f"journalctl --user -u {label()}.service\n"


@pytest.mark.parametrize("command", ["install", "uninstall", "status", "logs"])
def test_other_platforms_fail_clearly(command: str, on, fake_system: FakeSystem):
    on("win32")
    result = invoke(command)
    assert result.exit_code == 1
    assert "macOS and Linux, not win32" in result.output
    assert fake_system.calls() == []


def test_a_missing_service_manager_is_an_error(on, monkeypatch: pytest.MonkeyPatch, tmp_path):
    on("linux")
    monkeypatch.setenv("PATH", str(tmp_path))
    result = invoke("install")
    assert result.exit_code == 1 and "`systemctl` is not on PATH" in result.output


BAD_LABELS = ["../../../tmp/x", "a/b", "", ".hidden", "-x", "a b", "x\ny", "x" * 101, "é"]


@pytest.mark.parametrize("platform", ["darwin", "linux"])
@pytest.mark.parametrize("command", ["install", "uninstall", "status", "logs"])
@pytest.mark.parametrize("bad", BAD_LABELS)
def test_a_bad_label_is_refused_before_anything_runs(
    bad: str, command: str, platform: str, on, fake_system: FakeSystem
):
    on(platform)
    result = invoke(command, "--label", bad)
    assert result.exit_code == 1
    assert result.output.startswith("error: invalid label ")
    assert result.output.count("\n") == 1
    assert fake_system.calls() == []
    assert list(Path.home().iterdir()) == []
    assert not Path(os.environ["ORCH_HOME"]).exists()


@pytest.mark.parametrize("good", ["x", "_x", "local.spartito-test_1.web", "x" * 100])
def test_a_good_label_is_taken_as_it_is(good: str):
    assert autostart.check_label(good) == good


def test_the_label_option_names_the_service(on, fake_system: FakeSystem):
    on("darwin")
    assert invoke("install", "--label", "local.spartito-test.web").exit_code == 0
    plist = Path.home() / "Library" / "LaunchAgents" / "local.spartito-test.web.plist"
    assert plistlib.loads(plist.read_bytes())["Label"] == "local.spartito-test.web"
    assert invoke("uninstall", "--label", "local.spartito-test.web").exit_code == 0
    assert not plist.exists()


def test_a_non_loopback_host_warns_as_orch_web_does(on):
    on("darwin")
    result = invoke("install", "--host", "0.0.0.0")
    assert result.exit_code == 0
    assert "warning: the web UI has no authentication" in result.output
    args = plistlib.loads(plist_path().read_bytes())["ProgramArguments"]
    assert args[args.index("--host") + 1] == "0.0.0.0"


# -- who answers -----------------------------------------------------------------------------


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture
def server():
    """serve(status, body) -> port of a local HTTP server that answers every GET so."""
    started = []

    def serve(code: int, body: bytes) -> int:
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=httpd.serve_forever, args=(0.05,), daemon=True).start()
        started.append(httpd)
        return httpd.server_address[1]

    yield serve
    for httpd in started:
        httpd.shutdown()
        httpd.server_close()


def ping_body(home: Path, tool: str = "orchestrator") -> bytes:
    digest = hashlib.sha256(str(home).encode()).hexdigest()[:16]
    return json.dumps({"tool": tool, "version": "0.1.0", "home": digest}).encode()


def test_ping_tells_ours_from_foreign_from_nothing(server, tmp_path: Path):
    home = autostart.home()
    ours = server(200, ping_body(home))
    assert autostart.ping("127.0.0.1", ours, home) == ("ours", "")
    assert autostart.ping("0.0.0.0", ours, home) == ("ours", "")  # asked on loopback
    other_home = server(200, ping_body(tmp_path / "elsewhere"))
    assert autostart.ping("127.0.0.1", other_home, home)[0] == "foreign"
    other_tool = server(200, ping_body(home, tool="something"))
    assert autostart.ping("127.0.0.1", other_tool, home) == ("foreign", "not this tool")
    not_found = server(404, b"{}")
    assert autostart.ping("127.0.0.1", not_found, home)[0] == "foreign"
    garbage = server(200, b"<html>")
    assert autostart.ping("127.0.0.1", garbage, home)[0] == "foreign"
    assert autostart.ping("127.0.0.1", free_port(), home) == ("none", "")


def test_status_reports_who_answers_on_the_units_port(on, server):
    on("darwin")
    port = server(200, ping_body(autostart.home()))
    assert invoke("install", "--port", str(port)).exit_code == 0
    assert f"web UI:  http://127.0.0.1:{port}: answers\n" in invoke("status").output
    foreign = server(200, b'{"tool": "else"}')
    assert invoke("install", "--port", str(foreign)).exit_code == 0
    assert "something else answers (not this tool)" in invoke("status").output


def test_status_agrees_with_the_web_apps_own_ping(on, monkeypatch, tmp_path: Path):
    """The dashboard's real /api/ping, run in-process: same home answers, another does not."""
    from starlette.testclient import TestClient

    from orchestrator.web.app import app

    client = TestClient(app, base_url="http://localhost")
    served = {"home": os.environ["ORCH_HOME"]}  # unresolved: both sides must resolve it

    def get_local(url: str, timeout: float, limit: int = 65536) -> bytes:
        assert url == "http://127.0.0.1:18787/api/ping"
        with monkeypatch.context() as server_env:
            server_env.setenv("ORCH_HOME", served["home"])
            resp = client.get("/api/ping")
        assert resp.status_code == 200
        return resp.content

    monkeypatch.setattr(autostart, "get_local", get_local)
    on("darwin")
    assert invoke("install", "--port", "18787").exit_code == 0
    assert "web UI:  http://127.0.0.1:18787: answers\n" in invoke("status").output
    served["home"] = str(tmp_path / "elsewhere")
    assert (
        "something else answers (this tool, but for another ORCH_HOME)" in invoke("status").output
    )


# -- the real systemd, where there is one ----------------------------------------------------


def real_systemd_analyze() -> str | None:
    fakes = os.environ["FAKE_SYSTEM_DIR"]
    path = os.pathsep.join(p for p in os.environ["PATH"].split(os.pathsep) if p != fakes)
    return shutil.which("systemd-analyze", path=path)


@pytest.mark.skipif(sys.platform != "linux", reason="systemd only on Linux")
@pytest.mark.parametrize("folder", ["bin", "my py%x$y"])
def test_systemd_accepts_the_unit(folder: str, tmp_path: Path):
    """Also outside a login session (its own XDG_RUNTIME_DIR). verify checks that the
    executable exists, so a link to this interpreter stands in a folder with ` `, `%`, `$`."""
    analyze = real_systemd_analyze()
    if not analyze:
        pytest.skip("no systemd-analyze")
    runtime = tmp_path / "runtime"
    runtime.mkdir(mode=0o700)
    interpreter = tmp_path / folder / "python3"
    interpreter.parent.mkdir()
    interpreter.symlink_to(sys.executable)
    svc = autostart.service("127.0.0.1", 18787)
    unit = tmp_path / "local.spartito-check.web.service"
    unit.write_text(autostart.render_unit(replace(svc, argv=[str(interpreter), *svc.argv[1:]])))
    result = subprocess.run(
        [analyze, "verify", "--user", "--man=no", "--recursive-errors=no", str(unit)],
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, "XDG_RUNTIME_DIR": str(runtime)},
    )
    assert result.returncode == 0, result.stderr


def test_the_fakes_stand_in_for_every_service_manager():
    for name in SYSTEM_COMMANDS:
        assert shutil.which(name).startswith(os.environ["FAKE_SYSTEM_DIR"])
    assert Path.home() != Path(pwd.getpwuid(os.getuid()).pw_dir)
