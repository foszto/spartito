"""The command's name (config.CLI_NAME).

Nothing the tool starts or writes to disk calls the command by name: every process and every
definition (service, launcher, MCP configuration, runner) runs `<python> -m orchestrator…`,
so an installation whose command is missing or renamed never holds a stale reference.

No text tells anyone to run the command under its old name `orch`: the help pages, and the
README, docs/, scripts/, the dashboard's files, the role prompts and the string literals and
docstrings of the package. The internal names that keep `orch` (the MCP server `orch` and its
`mcp__orch__` tools, the `[orch notification]` marker, ORCH_* variables, orch.db, the `orch/`
branches) never stand before a subcommand, so they need no exception; the one place that may
name the old command is ALLOWED."""

from __future__ import annotations

import ast
import json
import plistlib
import re
import shlex
import sys
import tomllib
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import typer.main
from typer.testing import CliRunner

from orchestrator import autostart, cli, config, desktop
from orchestrator.core import launcher as run_launcher
from orchestrator.gui import launcher, menubar, window
from orchestrator.models import Agent
from orchestrator.runner import mcp_server

NAMES = {config.CLI_NAME, cli.OLD_NAME}
ROOT = Path(__file__).resolve().parent.parent
PACKAGE = ROOT / "src" / "orchestrator"
# Where the old name may stand before a subcommand: the section that explains the rename.
ALLOWED = {ROOT / "docs" / "usage.md": "## Upgrading from `orch`"}


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


# -- the entry point -------------------------------------------------------------------------


def test_the_only_entry_point_is_the_command() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]
    assert project["scripts"] == {config.CLI_NAME: "orchestrator.cli:main"}


# -- no text names the old command -----------------------------------------------------------


def command_paths() -> list[list[str]]:
    """Every command and group of the CLI, as the words that call it."""
    found: list[list[str]] = []

    def walk(command: Any, path: list[str]) -> None:
        found.append(path)
        for name, sub in (getattr(command, "commands", None) or {}).items():
            walk(sub, [*path, name])

    walk(typer.main.get_command(cli.app), [])
    return found


SUBCOMMANDS = sorted({path[0] for path in command_paths() if path})
OLD_COMMAND = re.compile(rf"(?<![\w./-]){cli.OLD_NAME}\s+(?:{'|'.join(SUBCOMMANDS)})\b")


# In README.md and docs/: `orch` as a word of its own is the command, unless it names the MCP
# server (`orch` MCP server, `orch` server, `orch` tools) or the upgrade section.
BARE_NAME = re.compile(
    rf"(?<!Upgrading from )`{cli.OLD_NAME}`(?!\s+(?:MCP|server|tools)\b)", re.IGNORECASE
)
# In a Python string: the old name at the end of an f-string part that a value follows
# (`f"orch {sub}"`), so the subcommand cannot be seen.
NAME_BEFORE_VALUE = re.compile(rf"(?:^|[\s`(]){cli.OLD_NAME}\s+$")
KEPT = "the `orch` MCP tools, the `orch` server, branch orch/x-1, [orch notification], .orch/"


def test_the_patterns_know_the_commands_and_the_kept_names() -> None:
    assert {"app", "chat", "web", "service", "status", "board"} <= set(SUBCOMMANDS)
    assert OLD_COMMAND.search("run `orch app install`")
    assert BARE_NAME.search("puts `orch` on the PATH")
    assert not OLD_COMMAND.search(KEPT) and not BARE_NAME.search(KEPT)
    assert not BARE_NAME.search("[Upgrading from `orch`](docs/usage.md#upgrading-from-orch)")
    source = (
        'A = ["orch", "app"]\n'
        'B = ("x", "orch")\n'
        'C = f"run orch {sub} now"\n'
        'D = f"`orch {sub}`"\n'
        'E = FastMCP("orch")\n'
        'F = {"orch": 1}\n'
        'G = f"mcp__orch__{tool}"\n'
        'H = f"[orch notification] {text}"\n'
    )
    assert [line for line, _ in python_commands(source)] == [1, 2, 3, 4]


def python_texts(path: Path) -> list[tuple[int, str]]:
    """The string literals of a module, docstrings and f-string parts included."""
    tree = ast.parse(path.read_text(), str(path))
    return [
        (node.lineno, node.value)
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    ]


def python_commands(source: str) -> list[tuple[int, str]]:
    """The old name used as the command in code: a list or tuple element that is exactly it
    (an argv), or an f-string part ending in it before a value (`f"orch {sub}"`). The MCP
    server's name (a call argument, a dict key) and the kept names do not match."""
    found = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.List | ast.Tuple):
            found += [
                (item.lineno, item.value)
                for item in node.elts
                if isinstance(item, ast.Constant) and item.value == cli.OLD_NAME
            ]
        elif isinstance(node, ast.JoinedStr):
            parts = node.values
            found += [
                (part.lineno, part.value)
                for part, after in zip(parts, parts[1:], strict=False)
                if isinstance(part, ast.Constant)
                and isinstance(after, ast.FormattedValue)
                and NAME_BEFORE_VALUE.search(part.value)
            ]
    return sorted(found)


def file_texts(path: Path) -> list[tuple[int, str]]:
    """The whole file, its ALLOWED section blanked out (line numbers kept)."""
    text = path.read_text()
    if heading := ALLOWED.get(path):
        start = text.index(heading)
        end = text.find("\n## ", start)
        end = len(text) if end < 0 else end
        text = text[:start] + "\n" * text.count("\n", start, end) + text[end:]
    return [(1, text)]


def scanned() -> list[Path]:
    static = PACKAGE / "web" / "static"
    return [
        ROOT / "README.md",
        *sorted((ROOT / "docs").glob("*.md")),
        *sorted((ROOT / "scripts").glob("*.py")),
        PACKAGE / "web" / "index.html",
        *sorted(static.glob("*.js")),
        *sorted(static.glob("*.css")),
        *sorted((PACKAGE / "roles").glob("*.md")),
        *sorted(PACKAGE.rglob("*.py")),
    ]


def test_no_text_tells_to_run_the_old_command() -> None:
    found = []
    for path in scanned():
        texts = python_texts(path) if path.suffix == ".py" else file_texts(path)
        for line, text in texts:
            for match in OLD_COMMAND.finditer(text):
                at = line + text.count("\n", 0, match.start())
                found.append(f"{path.relative_to(ROOT)}:{at}: {match.group()!r}")
    assert found == []


def test_no_code_runs_the_old_command() -> None:
    found = [
        f"{path.relative_to(ROOT)}:{line}: {text!r}"
        for path in [*sorted((ROOT / "scripts").glob("*.py")), *sorted(PACKAGE.rglob("*.py"))]
        for line, text in python_commands(path.read_text())
    ]
    assert found == []


def test_no_doc_names_the_old_command() -> None:
    found = []
    for path in [ROOT / "README.md", *sorted((ROOT / "docs").glob("*.md"))]:
        for _, text in file_texts(path):
            for match in BARE_NAME.finditer(text):
                line = 1 + text.count("\n", 0, match.start())
                found.append(f"{path.relative_to(ROOT)}:{line}: {match.group()!r}")
    assert found == []


def test_the_allowed_section_exists() -> None:
    for path, heading in ALLOWED.items():
        assert heading in path.read_text().splitlines()


def test_no_help_page_names_the_old_command() -> None:
    runner = CliRunner()
    for path in command_paths():
        result = runner.invoke(cli.app, [*path, "--help"], prog_name=config.CLI_NAME)
        assert result.exit_code == 0, path
        assert not re.search(rf"\b{cli.OLD_NAME}\b|{{cli}}", result.output), path
        assert f"Usage: {config.CLI_NAME}" in result.output


# -- a script left by an older install -------------------------------------------------------


@pytest.mark.parametrize("script", [cli.OLD_NAME, config.CLI_NAME])
def test_only_the_old_script_warns(
    script: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """An editable install from before the rename keeps its `orch` script, which runs this
    code until that install is replaced: it works, and warns on every call."""
    monkeypatch.setattr(sys, "argv", [f"/tools/bin/{script}", "roles"])
    with pytest.raises(SystemExit) as done:
        cli.main()
    out, err = capsys.readouterr()
    assert done.value.code == 0 and "worker" in out
    if script == config.CLI_NAME:
        assert err == ""
        return
    assert err == (
        f"warning: `{cli.OLD_NAME}` is now `{config.CLI_NAME}`; this script is left from an older"
        " install and goes away when that install is replaced"
        " (docs/usage.md#upgrading-from-orch)\n"
    )
    assert "## Upgrading from `orch`" in (ROOT / "docs" / "usage.md").read_text().splitlines()
