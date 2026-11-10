from __future__ import annotations

from pathlib import Path

import pytest

from orchestrator import roles
from orchestrator.loadout import Loadout, LoadoutError, resolve
from orchestrator.service import Orchestrator

DECLARED = """---
description: custom
tools: Read, Grep
mcp: inherit
setting_sources: user,project
slash_commands: false
codex_user_config: true
---
Do things.
"""


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "home"
    monkeypatch.setenv("ORCH_HOME", str(path))
    return path


def write_role(directory: Path, name: str, text: str) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{name}.md").write_text(text, encoding="utf-8")


def test_a_role_declares_every_loadout_key(home: Path) -> None:
    write_role(home / "roles", "custom", DECLARED)

    role = roles.load("custom")

    assert role.prompt == "Do things."
    assert resolve(role) == Loadout(
        tools=("Read", "Grep"),
        strict_mcp=False,
        setting_sources=("user", "project"),
        slash_commands=False,
        codex_user_config=True,
    )


def test_a_role_that_declares_nothing_strips_the_user_level_but_keeps_the_tools(
    home: Path,
) -> None:
    write_role(home / "roles", "legacy", "---\ndescription: old\n---\nWork.\n")

    role = roles.load("legacy")

    assert role.loadout == {}
    assert resolve(role) == Loadout(
        tools=None, strict_mcp=True, setting_sources=("project", "local"), codex_user_config=False
    )


def test_packaged_roles_load_only_what_their_job_needs(home: Path) -> None:
    names = ("orchestrator", "lead", "worker", "tester", "reviewer", "planner")
    loadouts = {name: resolve(roles.load(name)) for name in names}

    for name, loadout in loadouts.items():
        assert loadout.strict_mcp, name
        assert loadout.setting_sources == ("project", "local"), name
        assert loadout.slash_commands and not loadout.codex_user_config, name
        assert {"Bash", "Read", "Glob", "Grep"} <= set(loadout.tools), name
    for name in ("worker", "tester"):
        assert {"Edit", "Write"} <= set(loadouts[name].tools)
    for name in ("reviewer", "planner", "lead", "orchestrator"):
        assert not {"Edit", "Write"} & set(loadouts[name].tools), name
    assert {"WebSearch", "WebFetch"} <= set(loadouts["planner"].tools)
    assert roles.load("lead").lead and roles.load("orchestrator").lead


def test_overrides_win_per_key_project_role_first(home: Path, tmp_path: Path) -> None:
    project = tmp_path / "repo"
    (project / ".orch").mkdir(parents=True)
    home.mkdir()
    (project / ".orch" / "loadout.toml").write_text(
        '[worker]\ntools = ["Read"]\n\n[default]\ntools = "Bash"\nmcp = "inherit"\n'
    )
    (home / "loadout.toml").write_text(
        '[worker]\nmcp = "strict"\nsetting_sources = "user,project,local"\n\n'
        '[default]\nsetting_sources = "project"\nslash_commands = false\n'
    )

    worker = resolve(roles.load("worker"), project)
    reviewer = resolve(roles.load("reviewer"), project)
    no_project = resolve(roles.load("worker"))

    # project [worker] > project [default] > home [worker] > home [default] > frontmatter
    assert worker == Loadout(
        tools=("Read",),
        strict_mcp=False,
        setting_sources=None,
        slash_commands=False,
        codex_user_config=False,
    )
    assert reviewer.tools == ("Bash",) and reviewer.setting_sources == ("project",)
    assert no_project.tools == roles_tools("worker") and no_project.strict_mcp
    assert no_project.setting_sources is None  # user,project,local = inherit everything


def roles_tools(name: str) -> tuple[str, ...]:
    return tuple(roles.load(name).loadout["tools"].split(","))


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("[worker\ntools = 1", "invalid TOML"),
        ('tools = "Read"', "must be a table"),
        ('[worker]\ncolour = "red"', "unknown loadout key `colour`"),
        ('[default]\nmcp = "some"', "`mcp` must be `strict` or `inherit`"),
        ('[default]\nsetting_sources = "user,global"', "`setting_sources` takes"),
        ('[default]\ntools = ""', "use `none`"),
        ("[default]\nslash_commands = 1", "must be true or false"),
    ],
)
def test_a_malformed_loadout_file_is_an_error_naming_the_file(
    home: Path, text: str, message: str
) -> None:
    home.mkdir()
    (home / "loadout.toml").write_text(text)

    with pytest.raises(LoadoutError, match=message) as error:
        resolve(roles.load("worker"))

    assert str(home / "loadout.toml") in str(error.value)


def test_none_means_no_built_in_tools(home: Path) -> None:
    home.mkdir()
    (home / "loadout.toml").write_text('[default]\ntools = "none"\n')

    assert resolve(roles.load("worker")).tools == ()


def test_a_run_with_a_malformed_loadout_fails_with_the_reason(
    orch: Orchestrator, project: str, repo: Path
) -> None:
    (repo / ".orch").mkdir()
    (repo / ".orch" / "loadout.toml").write_text("[worker\n")
    agent, _ = orch.spawn(project, "hello", worktree=False)

    done, (view,) = orch.wait([agent.id], timeout=30)

    assert done and view["status"] == "failed"
    assert "loadout.toml: invalid TOML" in view["last_run"]["error"]
