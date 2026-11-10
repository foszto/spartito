from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path

import pytest

from orchestrator import memory_guard
from orchestrator.memory_guard import memory_dir, project_key, refusal
from orchestrator.providers.claude import memory_settings


@pytest.fixture
def env(tmp_path: Path) -> dict[str, str]:
    project = tmp_path / "proj"
    project.mkdir()
    return {"CLAUDE_PROJECT_DIR": str(project), "CLAUDE_CONFIG_DIR": str(tmp_path / "cfg")}


def memory(env: dict[str, str]) -> Path:
    path = Path(memory_dir(env))
    path.mkdir(parents=True, exist_ok=True)
    return path


def call(path: object, tool: str = "Write", cwd: str = "/") -> dict:
    field = memory_guard.PATH_FIELDS.get(tool, "file_path")
    return {
        "hook_event_name": "PreToolUse",
        "tool_name": tool,
        "tool_input": {field: path, "content": "x"},
        "cwd": cwd,
    }


def test_the_memory_directory_is_keyed_like_the_cli_does(env: dict[str, str]) -> None:
    key = re.sub(r"[^A-Za-z0-9]", "-", env["CLAUDE_PROJECT_DIR"])

    assert memory_dir(env) == os.path.join(env["CLAUDE_CONFIG_DIR"], "projects", key, "memory")
    assert project_key("/Users/a.b/Árvíz 😀") == "-Users-a-b--rv-z---"
    # Values computed with the CLI's own JavaScript (2.1.322) for a path over 200 characters.
    assert project_key("/a" * 120) == "-a" * 100 + "-vbgiog"


def test_writes_inside_the_memory_directory_pass(env: dict[str, str]) -> None:
    inside = memory(env)
    (inside / "MEMORY.md").write_text("index")

    for tool in ("Write", "Edit", "MultiEdit", "NotebookEdit"):
        assert refusal(call(str(inside / "MEMORY.md"), tool), env) is None
    assert refusal(call(str(inside / "new" / "fact.md")), env) is None


@pytest.mark.parametrize(
    "target",
    [
        "{project}/code.py",  # the working directory
        "/etc/hosts",
        "{memory}/../escape.md",
        "{memory}/sub/../../escape.md",
        "{memory}",  # the directory itself
        "{config}/projects/-other-project/memory/fact.md",
        "{config}/settings.json",
        "{memory}-evil/x.md",  # a sibling sharing the prefix
        "{memory}X",
        "{memory}X/x.md",
    ],
)
def test_writes_anywhere_else_are_refused(env: dict[str, str], target: str) -> None:
    inside = memory(env)
    path = target.format(
        project=env["CLAUDE_PROJECT_DIR"], memory=inside, config=env["CLAUDE_CONFIG_DIR"]
    )

    reason = refusal(call(path), env)

    assert reason and str(inside) in reason
    assert refusal(call(path, "Edit"), env)


@pytest.mark.parametrize(
    "target",
    [
        " {memory}/x.md",  # the CLI trims: it would write elsewhere than a join with the cwd
        "{memory}/x.md ",
        "\t{memory}/x.md",
        "fact.md",  # relative: refused even with the cwd inside the memory directory
        "./fact.md",
        "~/x.md",
        "~",
        "~root/x.md",
    ],
)
def test_paths_the_cli_may_map_differently_are_refused(env: dict[str, str], target: str) -> None:
    inside = memory(env)

    reason = refusal(call(target.format(memory=inside), cwd=str(inside)), env)

    assert reason and "plain absolute path" in reason


def test_a_leading_space_does_not_let_a_path_outside_through(
    env: dict[str, str], tmp_path: Path
) -> None:
    inside = memory(env)

    assert refusal(call(f" {tmp_path}/out/x", cwd=str(inside)), env)


def test_links_out_of_the_memory_directory_are_refused(env: dict[str, str], tmp_path: Path) -> None:
    inside = memory(env)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "target.md").write_text("x")
    (inside / "dir-link").symlink_to(outside)
    (inside / "file-link.md").symlink_to(outside / "target.md")
    os.link(outside / "target.md", inside / "hard.md")

    assert refusal(call(str(inside / "dir-link" / "new.md")), env)
    assert refusal(call(str(inside / "file-link.md")), env)
    assert refusal(call(str(inside / "hard.md")), env)


def test_a_worktree_keys_its_memory_by_the_main_checkout(tmp_path: Path) -> None:
    main = tmp_path / "main"
    worktree = tmp_path / "wt"
    git = ["git", "-c", "user.email=t@t", "-c", "user.name=t"]
    subprocess.run([*git, "init", "-q", str(main)], check=True)
    subprocess.run([*git, "-C", str(main), "commit", "-q", "--allow-empty", "-m", "i"], check=True)
    subprocess.run([*git, "-C", str(main), "worktree", "add", "-q", str(worktree)], check=True)
    (worktree / "sub").mkdir()
    config = str(tmp_path / "cfg")

    for start in (main, worktree, worktree / "sub"):
        found = memory_dir({"CLAUDE_PROJECT_DIR": str(start), "CLAUDE_CONFIG_DIR": config})
        assert found == memory_dir({"CLAUDE_PROJECT_DIR": str(main), "CLAUDE_CONFIG_DIR": config})


@pytest.mark.parametrize(
    "payload",
    [
        None,
        [],
        {"tool_name": "Write", "tool_input": {"file_path": "/x"}},  # no event name
        {"hook_event_name": "PostToolUse", "tool_name": "Write", "tool_input": {"file_path": "/x"}},
        call("/x", tool="Bash"),
        call(None),
        call(""),
        call(["/x"]),
        call("/x\0y"),
        {"hook_event_name": "PreToolUse", "tool_name": "Write", "tool_input": "/x"},
        call("relative.md", cwd=None),  # type: ignore[arg-type]
    ],
)
def test_malformed_calls_are_refused(env: dict[str, str], payload: object) -> None:
    memory(env)

    assert refusal(payload, env)


def test_without_a_project_directory_nothing_is_allowed(env: dict[str, str]) -> None:
    inside = memory(env)

    with pytest.raises(ValueError):
        refusal(call(str(inside / "f.md")), {"CLAUDE_CONFIG_DIR": env["CLAUDE_CONFIG_DIR"]})


@pytest.mark.parametrize("name", memory_guard.OVERRIDES)
def test_an_override_of_the_memory_location_refuses_everything(
    env: dict[str, str], name: str
) -> None:
    inside = memory(env)

    with pytest.raises(ValueError, match=name):
        refusal(call(str(inside / "f.md")), {**env, name: "/elsewhere"})


def test_a_symlinked_git_marker_is_not_guessed(tmp_path: Path) -> None:
    outer = tmp_path / "outer"
    inner = outer / "inner"
    (outer / ".git").mkdir(parents=True)
    (tmp_path / "real-git").mkdir()
    inner.mkdir()
    (inner / ".git").symlink_to(tmp_path / "real-git")
    config = str(tmp_path / "cfg")

    with pytest.raises(ValueError, match="symbolic link"):
        memory_dir({"CLAUDE_PROJECT_DIR": str(inner), "CLAUDE_CONFIG_DIR": config})
    outer_memory = memory_dir({"CLAUDE_PROJECT_DIR": str(outer), "CLAUDE_CONFIG_DIR": config})
    with pytest.raises(ValueError):  # not OUTER's memory
        refusal(
            call(f"{outer_memory}/x.md"),
            {"CLAUDE_PROJECT_DIR": str(inner), "CLAUDE_CONFIG_DIR": config},
        )


def test_a_worktree_of_a_bare_repository(tmp_path: Path) -> None:
    git = ["git", "-c", "user.email=t@t", "-c", "user.name=t"]
    seed = tmp_path / "seed"
    subprocess.run([*git, "init", "-q", str(seed)], check=True)
    subprocess.run([*git, "-C", str(seed), "commit", "-q", "--allow-empty", "-m", "i"], check=True)
    bare = tmp_path / "repo.git"
    subprocess.run([*git, "clone", "-q", "--bare", str(seed), str(bare)], check=True)
    worktree = tmp_path / "wt"
    subprocess.run([*git, "-C", str(bare), "worktree", "add", "-q", str(worktree)], check=True)
    env = {"CLAUDE_PROJECT_DIR": str(worktree), "CLAUDE_CONFIG_DIR": str(tmp_path / "cfg")}

    assert memory_dir(env).endswith(project_key(str(bare)) + "/memory")
    (bare / ".git").mkdir()  # the CLI would then key by the worktree itself: refuse instead
    with pytest.raises(ValueError, match="may key the memory by another directory"):
        memory_dir(env)


def run_hook(
    stdin: str, env: dict[str, str], python: str | None = None
) -> subprocess.CompletedProcess:
    """The hook command exactly as the CLI gets it, run through /bin/sh as the CLI does."""
    command = memory_settings(Path("/c"))["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
    if python:
        command = command.replace(command.split(" ")[0], python, 1)
    return subprocess.run(
        ["/bin/sh", "-c", command],
        input=stdin,
        env={**os.environ, **env},
        capture_output=True,
        text=True,
    )


def test_the_hook_command_exits_0_inside_and_2_otherwise(env: dict[str, str]) -> None:
    inside = memory(env)

    allowed = run_hook(json.dumps(call(str(inside / "f.md"))), env)
    refused = run_hook(json.dumps(call(env["CLAUDE_PROJECT_DIR"] + "/f.md")), env)
    garbage = run_hook("not json", env)
    no_project = run_hook(json.dumps(call(str(inside / "f.md"))), {"CLAUDE_PROJECT_DIR": ""})
    no_python = run_hook(json.dumps(call(str(inside / "f.md"))), env, python="/nonexistent/py")

    assert allowed.returncode == 0 and not allowed.stderr
    assert refused.returncode == 2 and "outside it" in refused.stderr
    assert garbage.returncode == 2 and "failed" in garbage.stderr
    assert no_project.returncode == 2
    assert no_python.returncode == 2  # fails closed when the guard cannot even start
