from __future__ import annotations

import os
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
        assert not loadout.slash_commands and not loadout.codex_user_config, name
        assert "Skill" not in loadout.tools, name  # skills off: they could not be invoked
        assert {"Bash", "Read", "Glob", "Grep"} <= set(loadout.tools), name
    for name in ("worker", "tester"):
        assert {"Edit", "Write"} <= set(loadouts[name].tools)
    for name in ("reviewer", "planner", "lead", "orchestrator"):
        assert not {"Edit", "Write"} & set(loadouts[name].tools), name
    # Write/Edit for their own memory directory only (see test_memory_guard.py)
    assert {name for name, loadout in loadouts.items() if loadout.memory} == {
        "orchestrator",
        "lead",
    }
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
        ("[default]\ntools = 1", "`tools` must be a comma-separated string or a list"),
        ("[default]\nmcp = true", "`mcp` must be `strict` or `inherit`, not True"),
        ('[worker]\nmemory = "yes"', "`memory` must be true or false, not 'yes'"),
        ("[worker]\nmemory = 1", "`memory` must be true or false, not 1"),
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


def test_an_unreadable_loadout_file_is_an_error(home: Path) -> None:
    home.mkdir()
    path = home / "loadout.toml"
    path.write_text("[default]\n")
    path.chmod(0)
    try:
        if path.stat().st_uid == 0 or os.access(path, os.R_OK):
            pytest.skip("running as a user who can read anything")
        with pytest.raises(LoadoutError, match="cannot read"):
            resolve(roles.load("worker"))
    finally:
        path.chmod(0o600)


@pytest.mark.parametrize(
    ("line", "message"),
    [
        ("tools:", "`tools` is empty"),
        ("mcp: maybe", "`mcp` must be"),
        ("slash_commands: yes", "true or false"),
        ("memory: on", "`memory` must be true or false"),
    ],
)
def test_an_invalid_frontmatter_value_names_the_role_file(
    home: Path, line: str, message: str
) -> None:
    write_role(home / "roles", "custom", f"---\ndescription: x\n{line}\n---\nWork.\n")

    with pytest.raises(LoadoutError, match=message) as error:
        resolve(roles.load("custom"))

    assert str(home / "roles" / "custom.md") in str(error.value)


def test_a_table_for_an_unknown_role_is_ignored_with_a_warning(home: Path) -> None:
    home.mkdir()
    (home / "loadout.toml").write_text('[wroker]\ntools = "Read"\n\n[reviewer]\nmcp = "inherit"\n')
    warnings: list[str] = []

    loadout = resolve(roles.load("worker"), warn=warnings.append)

    assert loadout.tools == roles_tools("worker")
    assert warnings == [f"{home / 'loadout.toml'}: ignoring [wroker]: no such role here"]


def test_a_run_reports_an_unknown_role_table_and_still_runs(
    orch: Orchestrator, project: str, repo: Path
) -> None:
    (repo / ".orch").mkdir()
    (repo / ".orch" / "loadout.toml").write_text('[Worker]\ntools = "Read"\n')
    agent, _ = orch.spawn(project, "hello", worktree=False)

    done, (view,) = orch.wait([agent.id], timeout=30)

    assert done and view["status"] == "done"
    info = [event.text for event in orch.store.events(agent.id) if event.kind == "info"]
    assert any("ignoring [Worker]: no such role here" in text for text in info)


def test_memory_is_set_in_frontmatter_or_loadout_toml(home: Path, tmp_path: Path) -> None:
    write_role(home / "roles", "keeper", "---\ntools: Read\nmemory: true\n---\nKeep.\n")
    project = tmp_path / "repo"
    (project / ".orch").mkdir(parents=True)
    (project / ".orch" / "loadout.toml").write_text("[lead]\nmemory = false\n")
    (home / "loadout.toml").write_text("[worker]\nmemory = true\n")

    assert resolve(roles.load("keeper")).memory
    assert resolve(roles.load("worker")).memory
    assert resolve(roles.load("lead")).memory and not resolve(roles.load("lead"), project).memory
    assert not resolve(roles.load("reviewer")).memory


def test_packaged_lead_and_orchestrator_carry_the_pr_review_rule(home: Path) -> None:
    lead = " ".join(roles.load("lead").prompt.split())
    orchestrator = " ".join(roles.load("orchestrator").prompt.split())

    assert "PR review feedback" in lead and "in which commit" in lead
    assert "once a fix is on the PR branch" in lead
    assert (
        "Every push needs the user's approval" in lead and "do not resolve review threads" in lead
    )
    assert "PR review feedback goes to the project lead" in orchestrator
    assert "push approval requests to the user; never grant them yourself" in orchestrator


def test_packaged_prompts_bound_review_rounds_and_scope(home: Path) -> None:
    def text(name: str) -> str:
        return " ".join(roles.load(name).prompt.split())

    lead, reviewer, worker = text("lead"), text("reviewer"), text("worker")

    assert "At most 2 review rounds per branch" in lead and "with their severity" in lead
    assert "until the result is right" not in lead
    assert "each with a severity; leave out style and nice-to-haves" in reviewer
    assert "conventions" not in reviewer
    assert "Choose the simplest change that meets the brief" in worker
    assert "stop and report instead of building it" in worker
