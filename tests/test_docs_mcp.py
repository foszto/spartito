"""docs/mcp.md and the `spartito mcp` reference in docs/usage.md: every `spartito mcp` command
line they show as working names a subcommand and options that exist; what later phases plan
(`mcp tools`, `mcp measure`, `mcp login`, `--refresh`, `--also-claude-code`, the HTTP
endpoints) exists nowhere in the command, stands only in mcp.md's design part, and is marked
planned where its command lines are listed. No home, no subprocess."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
import typer.main
from test_docs_links import anchors

from orchestrator import capabilities, cli, mcp

ROOT = Path(__file__).resolve().parent.parent
DOC = ROOT / "docs" / "mcp.md"
USAGE = ROOT / "docs" / "usage.md"
DESIGN = "## Design of the later phases"
GROUP = typer.main.get_command(cli.app).commands["mcp"]
PLANNED = {
    "mcp tools": re.compile(r"\bmcp tools\b"),
    "mcp measure": re.compile(r"\bmcp measure\b"),
    "mcp login": re.compile(r"(?<!claude )\bmcp login\b"),  # claude mcp login is Claude Code's
    "--refresh": re.compile(r"--refresh\b"),
    "--also-claude-code": re.compile(r"--also-claude-code\b"),
    "/api/mcp": re.compile(r"/api/mcp\b"),
}


def built_part() -> str:
    text = DOC.read_text(encoding="utf-8")
    assert DESIGN in text.splitlines(), f"docs/mcp.md has no heading {DESIGN!r}"
    return text[: text.index(DESIGN)]


def usage_part() -> str:
    text = USAGE.read_text(encoding="utf-8")
    start = text.index("\n### MCP servers\n")
    return text[start : text.index("\n### ", start + 1)]


def command_lines(text: str) -> list[str]:
    """The `spartito mcp …` lines of the code blocks (continued lines joined)."""
    found, fenced, current = [], False, ""
    for line in text.splitlines():
        if line.strip().startswith("```"):
            fenced = not fenced
            continue
        if not fenced:
            continue
        current += line.strip().removeprefix("$ ")
        if current.endswith("\\"):
            current = current[:-1] + " "
            continue
        if current.startswith("spartito mcp"):
            found.append(current.split("#")[0].strip())
        current = ""
    return found


def check(line: str) -> list[str]:
    """What the line names that the command does not have."""
    words = [word.strip("[]…") for word in line.split()[2:]]
    commands: list[Any] = [GROUP]
    if words and not words[0].startswith("-"):
        names = words.pop(0).split("|")
        missing = [name for name in names if name not in GROUP.commands]
        if missing:
            return missing
        commands = [GROUP.commands[name] for name in names]
    problems = []
    for command in commands:
        options = {
            opt: param
            for param in command.params
            if param.param_type_name == "option"
            for opt in [*param.opts, *param.secondary_opts]
        }
        skip = False
        for word in words:
            if skip:
                skip = False
                continue
            if not word.startswith("-"):
                continue
            for name in word.split("=")[0].split("|"):
                if name not in options:
                    problems.append(f"{command.name}: {name}")
            param = options.get(word.split("=")[0].split("|")[0])
            skip = "=" not in word and param is not None and not param.is_flag
    return problems


@pytest.mark.parametrize("where", ["mcp.md", "usage.md"])
def test_every_command_line_shown_as_working_exists(where: str) -> None:
    lines = command_lines(built_part() if where == "mcp.md" else usage_part())
    shown = {name for line in lines for name in (line.split() + ["", "", ""])[2].split("|")}
    assert {"show", "add", "remove", "on", "off", "inherit"} <= shown
    for line in lines:
        assert check(line) == [], line


def test_the_checker_sees_what_does_not_exist() -> None:
    assert check("spartito mcp tools docs-db") == ["tools"]
    assert check("spartito mcp --refresh") == ["mcp: --refresh"]
    assert check("spartito mcp add x --url U --also-claude-code") == ["add: --also-claude-code"]
    assert check("spartito mcp add x --command c --arg --read-only --arg=-v") == []


@pytest.mark.parametrize("planned", sorted(PLANNED))
def test_what_later_phases_plan_is_not_in_the_command(planned: str) -> None:
    names = set(GROUP.commands) | {
        opt for param in GROUP.params for opt in getattr(param, "opts", [])
    }
    for command in GROUP.commands.values():
        names |= {opt for param in command.params for opt in getattr(param, "opts", [])}
    assert planned.removeprefix("mcp ") not in names


@pytest.mark.parametrize("planned", sorted(PLANNED))
def test_what_later_phases_plan_stands_only_in_the_design(planned: str) -> None:
    pattern = PLANNED[planned]
    assert not pattern.search(built_part()), f"{planned} in mcp.md's built part"
    for path in [ROOT / "README.md", *sorted((ROOT / "docs").glob("*.md"))]:
        if path != DOC:
            assert not pattern.search(path.read_text(encoding="utf-8")), f"{planned} in {path}"
    design = DOC.read_text(encoding="utf-8").split(DESIGN, 1)[1]
    listed = [line for line in design.splitlines() if line.startswith("    spartito mcp")]
    for line in listed:
        if pattern.search(line):
            assert "# planned" in line, line


def test_the_phases_table_marks_phase_1_built() -> None:
    row = next(line for line in DOC.read_text().splitlines() if line.startswith("| 1 |"))
    assert "(built)" in row


def test_an_mcp_capabilitys_docs_link_names_a_heading() -> None:
    server = mcp.server("docs-db", command="docs-db-mcp")
    path, _, anchor = capabilities.mcp_capability(server).docs.partition("#")
    assert path == "docs/mcp.md" and anchor in anchors(DOC)


def test_the_other_docs_lead_to_it() -> None:
    for name in ("capabilities.md", "usage.md", "agents.md"):
        assert "(mcp.md" in (ROOT / "docs" / name).read_text(encoding="utf-8"), name
    assert "spartito mcp add" in usage_part()
