"""The command's name (config.CLI_NAME). Nothing the tool starts or writes to disk calls the
command by name: every process and every definition (service, launcher, MCP configuration,
runner) runs `<python> -m orchestrator…`, so an installation whose command is missing or
renamed never holds a stale reference."""

from __future__ import annotations

import json
import plistlib
import shlex
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from orchestrator import autostart, cli, config, desktop
from orchestrator.core import launcher as run_launcher
from orchestrator.gui import launcher, menubar, window
from orchestrator.models import Agent
from orchestrator.runner import mcp_server

NAMES = {config.CLI_NAME, cli.OLD_NAME}


def by_interpreter(argv: list[str] | tuple[str, ...], module: str) -> None:
    """`argv` is `<this python> -m <module> …` and names no command."""
    assert list(argv[:3]) == [sys.executable, "-m", module]
    assert not NAMES & {Path(arg).name for arg in argv}


def test_the_service_runs_the_interpreter() -> None:
    svc = autostart.service(port=18891, label="local.spartito.test")
    by_interpreter(svc.argv, "orchestrator")
    by_interpreter(plistlib.loads(autostart.render_plist(svc))["ProgramArguments"], "orchestrator")
    exec_start = next(
        line for line in autostart.render_unit(svc).splitlines() if line.startswith("ExecStart=")
    )
    by_interpreter(shlex.split(exec_start.removeprefix("ExecStart=")), "orchestrator")


@pytest.mark.parametrize("own_python", [False, True])
def test_the_launcher_script_runs_the_interpreter(tmp_path: Path, own_python: bool) -> None:
    item = launcher.launcher("Spartito Test", tmp_path, platform="darwin")
    by_interpreter(item.argv, "orchestrator")
    script = launcher.render_script(item, own_python)
    execs = [shlex.split(line)[1:] for line in script.splitlines() if "exec " in line]
    assert len(execs) == (2 if own_python else 1)
    for command in execs:
        assert command[0] in (sys.executable, str(item.interpreter))
        assert command[1:3] == ["-m", "orchestrator"]
        assert not NAMES & {Path(arg).name for arg in command}


def test_the_desktop_entry_runs_the_interpreter(tmp_path: Path) -> None:
    item = launcher.launcher("Spartito Test", tmp_path, platform="linux")
    exec_line = next(
        line for line in launcher.render_desktop(item).splitlines() if line.startswith("Exec=")
    )
    words = shlex.split(exec_line.removeprefix("Exec="))
    by_interpreter(words[words.index(sys.executable) :], "orchestrator")


def test_the_processes_started_for_the_window_run_the_interpreter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    by_interpreter(menubar.APP_ARGV, "orchestrator")
    by_interpreter(window.app_argv(18892, None, "decision:3", False), "orchestrator")
    started: list[list[str]] = []
    monkeypatch.setattr(desktop.subprocess, "Popen", lambda argv, **_: started.append(argv))
    desktop.start_app(["--agent", "a-1"])
    by_interpreter(started[0], "orchestrator")


def test_mcp_servers_and_runners_run_the_interpreter(monkeypatch: pytest.MonkeyPatch) -> None:
    servers = json.loads(cli.mcp_config())["mcpServers"]
    by_interpreter(
        [servers["orch"]["command"], *servers["orch"]["args"]], "orchestrator.mcp_server"
    )
    agent = Agent(id="a-1", project="p", name="a", role="worker", provider="claude", cwd="/tmp")
    server = mcp_server(agent)
    by_interpreter([server.command, *server.args], "orchestrator.mcp_server")

    started: list[list[str]] = []

    def popen(argv: list[str], **_: Any) -> Any:
        started.append(argv)
        return SimpleNamespace(pid=0, wait=lambda: 0)

    monkeypatch.setattr(run_launcher.subprocess, "Popen", popen)
    store = SimpleNamespace(update_run=lambda *a, **k: None)
    run_launcher.launch(store, SimpleNamespace(id=7))  # type: ignore[arg-type]
    by_interpreter(started[0], "orchestrator.runner")
