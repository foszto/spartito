"""The code view of an agent's worktree (core/code_view.py, web/code_api.py, the page's part
in app.js): the changed files against the base branch with their patches, the file tree and
a file's content; read-only, confined to the worktree and to the files git lists, without
running anything the repository's configuration names."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from orchestrator import git
from orchestrator.core import code_view
from orchestrator.service import Orchestrator, OrchestratorError
from orchestrator.web.app import app

STATIC = Path(__file__).resolve().parent.parent / "src" / "orchestrator" / "web" / "static"
ID = {"name": "t", "email": "t@example.com"}


def run_git(where: Path, *args: str) -> str:
    command = ["git", "-c", f"user.name={ID['name']}", "-c", f"user.email={ID['email']}"]
    done = subprocess.run([*command, "-C", str(where), *args], capture_output=True, text=True)
    assert done.returncode == 0, done.stderr
    return done.stdout


def commit_all(where: Path, message: str = "work") -> None:
    run_git(where, "add", "-A")
    run_git(where, "commit", "-q", "-m", message)


@pytest.fixture
def agent_id(orch: Orchestrator, project: str, repo: Path) -> str:
    """A worker with its own worktree on its branch from `main` (no turn run)."""
    agent = orch.create_agent(project, name="fix-login")
    git.add_worktree(repo, agent.cwd, agent.branch, "main")
    return agent.id


@pytest.fixture
def tree(orch: Orchestrator, agent_id: str) -> Path:
    return Path(orch.agent(agent_id).cwd)


@pytest.fixture
def client() -> TestClient:
    return TestClient(app, base_url="http://localhost")


def by_path(answer: dict) -> dict[str, dict]:
    return {row["path"]: row for row in answer["files"]}


# -- the changed files and their patches --------------------------------------------------


def test_committed_uncommitted_and_untracked_changes_are_listed_with_counts(
    orch: Orchestrator, agent_id: str, tree: Path, repo: Path
) -> None:
    (repo / "kept.txt").write_text("one\ntwo\nthree\n")
    (repo / "gone.txt").write_text("a\nb\n")
    commit_all(repo, "base files")
    run_git(tree, "merge", "-q", "--ff-only", "main")
    (tree / "committed.py").write_text("print(1)\nprint(2)\n")
    commit_all(tree)
    (tree / "kept.txt").write_text("one\nTWO\nthree\nfour\n")  # unstaged
    (tree / "gone.txt").unlink()
    (tree / "staged.txt").write_text("staged\n")
    run_git(tree, "add", "staged.txt")
    (tree / "new.md").write_text("first\nsecond\nno newline")  # untracked

    answer = orch.code_changes(agent_id)
    rows = by_path(answer)

    assert answer["base"] == "main" and answer["truncated"] is False and answer["total"] == 5
    assert list(rows) == ["committed.py", "gone.txt", "kept.txt", "new.md", "staged.txt"]
    assert rows["committed.py"] == {
        "path": "committed.py",
        "status": "A",
        "added": 2,
        "removed": 0,
        "binary": False,
        "too_large": False,
        "untracked": False,
        "submodule": False,
    }
    assert (rows["kept.txt"]["status"], rows["kept.txt"]["added"], rows["kept.txt"]["removed"]) == (
        "M",
        2,
        1,
    )
    assert (rows["gone.txt"]["status"], rows["gone.txt"]["removed"]) == ("D", 2)
    assert (rows["staged.txt"]["status"], rows["staged.txt"]["added"]) == ("A", 1)
    assert rows["new.md"]["untracked"] is True
    assert (rows["new.md"]["status"], rows["new.md"]["added"], rows["new.md"]["removed"]) == (
        "A",
        3,
        0,
    )


def test_changes_are_counted_from_where_the_branch_left_the_base(
    orch: Orchestrator, agent_id: str, tree: Path, repo: Path
) -> None:
    """A commit on the base after the branch left it is not the agent's change."""
    (repo / "later.txt").write_text("on main\n")
    commit_all(repo, "later on main")
    (tree / "mine.txt").write_text("mine\n")
    commit_all(tree)

    assert list(by_path(orch.code_changes(agent_id))) == ["mine.txt"]


def test_a_patch_is_given_per_file_for_tracked_and_untracked_files(
    orch: Orchestrator, agent_id: str, tree: Path
) -> None:
    (tree / "app.py").write_text("x = 1\n")
    commit_all(tree)
    (tree / "app.py").write_text("x = 2\n")
    (tree / "notes.txt").write_text("hello\nworld")

    tracked = orch.code_patch(agent_id, "app.py")
    untracked = orch.code_patch(agent_id, "notes.txt")

    assert tracked["truncated"] is False and tracked["binary"] is False
    assert "+x = 2\n" in tracked["patch"] and "diff --git a/app.py b/app.py" in tracked["patch"]
    assert "-x" not in tracked["patch"]  # against the base, the file is new
    assert untracked["patch"].splitlines() == [
        "diff --git a/notes.txt b/notes.txt",
        "new file mode 100644",
        "--- /dev/null",
        "+++ b/notes.txt",
        "@@ -0,0 +1,2 @@",
        "+hello",
        "+world",
        "\\ No newline at end of file",
    ]
    with pytest.raises(OrchestratorError, match="not a file in this worktree"):
        orch.code_patch(agent_id, "README-that-is-not-there")


def test_binary_and_oversized_files_are_named_not_rendered(
    orch: Orchestrator, agent_id: str, tree: Path
) -> None:
    (tree / "logo.png").write_bytes(b"\x89PNG\r\n\x00\x00binary")
    commit_all(tree)
    (tree / "blob.bin").write_bytes(b"text first\x00then binary")  # untracked
    big = "line\n" * (code_view.MAX_FILE_BYTES // 5 + 10)
    (tree / "big.log").write_text(big)  # untracked, above the cap

    rows = by_path(orch.code_changes(agent_id))
    assert rows["logo.png"]["binary"] is True and rows["logo.png"]["added"] is None
    assert rows["blob.bin"]["binary"] is True and rows["blob.bin"]["added"] is None
    assert rows["big.log"]["too_large"] is True and rows["big.log"]["added"] is None

    for path in ("logo.png", "blob.bin"):
        patch = orch.code_patch(agent_id, path)
        assert patch["binary"] is True and patch["patch"] is None
        shown = orch.code_file(agent_id, path)
        assert shown["binary"] is True and shown["content"] is None
    assert orch.code_patch(agent_id, "big.log")["too_large"] is True
    assert orch.code_patch(agent_id, "big.log")["patch"] is None

    shown = orch.code_file(agent_id, "big.log")
    assert shown["truncated"] is True and shown["size"] == len(big)
    assert len(shown["content"].encode()) == code_view.MAX_FILE_BYTES


def test_long_patches_and_long_lists_are_cut_and_say_so(
    orch: Orchestrator, agent_id: str, tree: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tree / "long.txt").write_text("".join(f"{n}\n" for n in range(3000)))
    for n in range(4):
        (tree / f"f{n}.txt").write_text("x\n")
    commit_all(tree)

    patch = orch.code_patch(agent_id, "long.txt")
    assert patch["truncated"] is True and patch["max_lines"] == code_view.MAX_PATCH_LINES
    assert patch["lines"] > code_view.MAX_PATCH_LINES
    assert len(patch["patch"].splitlines()) == code_view.MAX_PATCH_LINES

    monkeypatch.setattr(code_view, "MAX_CHANGED", 2)
    monkeypatch.setattr(code_view, "MAX_TREE", 3)
    changes = orch.code_changes(agent_id)
    files = orch.code_tree(agent_id)
    assert (changes["truncated"], changes["total"], len(changes["files"])) == (True, 5, 2)
    assert (files["truncated"], files["total"], len(files["files"])) == (True, 5, 3)
    assert changes["max_files"] == 2 and files["max_files"] == 3


# -- the file tree and a file's content ---------------------------------------------------


def test_the_tree_lists_tracked_and_untracked_files_but_never_ignored_ones(
    orch: Orchestrator, agent_id: str, tree: Path
) -> None:
    (tree / ".gitignore").write_text(".env\nbuild/\n")
    (tree / "src").mkdir()
    (tree / "src" / "main.py").write_text("print('hi')\n")
    commit_all(tree)
    (tree / "src" / "draft.py").write_text("pass\n")
    (tree / ".env").write_text("TOKEN=not-for-the-page\n")
    (tree / "build").mkdir()
    (tree / "build" / "out.js").write_text("ignored\n")

    files = orch.code_tree(agent_id)["files"]
    assert files == [".gitignore", "src/draft.py", "src/main.py"]
    assert orch.code_file(agent_id, "src/main.py")["content"] == "print('hi')\n"
    assert ".env" not in by_path(orch.code_changes(agent_id))
    for path in (".env", "build/out.js"):
        with pytest.raises(OrchestratorError, match="not a file git lists"):
            orch.code_file(agent_id, path)
        with pytest.raises(OrchestratorError, match="no changes"):
            orch.code_patch(agent_id, path)


@pytest.mark.parametrize(
    "path",
    [
        "../repo/README",
        "src/../../x",
        "/etc/passwd",
        "a\0b",
        "a//b",
        "./a",
        "a/",
        "",
        ".git/config",
        ".GIT/config",
        "sub/.git/HEAD",
        None,
    ],
)
def test_paths_outside_the_worktree_or_into_git_are_refused(
    orch: Orchestrator, agent_id: str, tree: Path, path: str | None
) -> None:
    (tree / "a").mkdir()
    (tree / "a" / "b").write_text("x\n")
    for read in (orch.code_file, orch.code_patch):
        with pytest.raises(OrchestratorError):
            read(agent_id, path)


def test_git_config_is_never_served_even_when_named_without_dot_git(
    orch: Orchestrator, agent_id: str, tree: Path
) -> None:
    """The worktree's `.git` is a file pointing at the repository; neither it nor what git
    keeps is ever listed."""
    assert ".git" not in orch.code_tree(agent_id)["files"]
    with pytest.raises(OrchestratorError):
        orch.code_file(agent_id, ".git")


def test_a_link_is_shown_as_its_target_and_never_followed(
    orch: Orchestrator, agent_id: str, tree: Path, tmp_path: Path
) -> None:
    secret = tmp_path / "outside"
    secret.mkdir()
    (secret / "secret.txt").write_text("SECRET-CONTENT\n")
    (tree / "docs").mkdir()
    (tree / "docs" / "guide.md").write_text("# Guide\n")
    os.symlink(secret / "secret.txt", tree / "escape")
    os.symlink("docs/guide.md", tree / "inside")
    os.symlink(secret, tree / "outdir")

    outside = orch.code_file(agent_id, "escape")
    inside = orch.code_file(agent_id, "inside")

    assert outside["kind"] == "symlink" and outside["content"] is None
    assert outside["target"] == str(secret / "secret.txt")
    assert inside == {**inside, "kind": "symlink", "target": "docs/guide.md", "content": None}
    for answer in (outside, inside, orch.code_patch(agent_id, "escape")):
        assert "SECRET-CONTENT" not in json.dumps(answer)
    with pytest.raises(OrchestratorError):
        orch.code_file(agent_id, "outdir/secret.txt")


def test_reading_never_goes_through_a_linked_directory(tree: Path, tmp_path: Path) -> None:
    """The second line of defence, below git's listing: a directory that is a link is not
    entered, whether its target is outside the worktree or inside it."""
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    (outside / "x.txt").write_text("outside\n")
    (tree / "real").mkdir()
    (tree / "real" / "y.txt").write_text("inside\n")
    os.symlink(outside, tree / "out")
    os.symlink(tree / "real", tree / "in")
    root = tree.resolve()

    for path in ("out/x.txt", "in/y.txt"):
        with pytest.raises((OSError, OrchestratorError)):
            code_view.read_entry(root, path, 100)
    assert code_view.read_entry(root, "real/y.txt", 100).data == b"inside\n"


def test_unusual_file_names_come_back_intact(orch: Orchestrator, agent_id: str, tree: Path) -> None:
    # The last one is the file `script>.html` in the folder `<script>alert(1)<`.
    names = ["with space.txt", "quote\"d'.txt", "ékezet-ü.md", "<script>alert(1)</script>.html"]
    (tree / "<script>alert(1)<").mkdir()
    for name in names:
        (tree / name).write_text(f"{name}\n")
    run_git(tree, "add", names[0], names[1])
    run_git(tree, "commit", "-q", "-m", "two of them")

    assert set(names) <= set(orch.code_tree(agent_id)["files"])
    assert set(names) <= set(by_path(orch.code_changes(agent_id)))
    for name in names:
        assert orch.code_file(agent_id, name)["content"] == f"{name}\n"
        assert f"+{name}\n" in orch.code_patch(agent_id, name)["patch"]


# -- which agents, which worktrees ---------------------------------------------------------


def test_only_an_agent_with_its_own_existing_worktree_is_shown(
    orch: Orchestrator, project: str, agent_id: str, tree: Path
) -> None:
    plain = orch.create_agent(project, name="no-tree", worktree=False)
    unborn = orch.create_agent(project, name="not-yet")
    calls = (orch.code_changes, orch.code_tree)

    for agent, message in (
        ("nobody-1234", "unknown agent"),
        (plain.id, "no worktree of its own"),
        (unborn.id, "does not exist"),
    ):
        for read in calls:
            with pytest.raises(OrchestratorError, match=message):
                read(agent)

    shutil.rmtree(tree)
    with pytest.raises(OrchestratorError, match="does not exist"):
        orch.code_tree(agent_id)
    tree.mkdir()  # a plain directory where the worktree was
    with pytest.raises(OrchestratorError, match="not a git repository"):
        orch.code_tree(agent_id)
    run_git(tree, "init", "-q")  # another repository there: not a worktree of the project
    with pytest.raises(OrchestratorError, match="is not a worktree of"):
        orch.code_tree(agent_id)


def test_a_project_no_longer_registered_is_refused(
    orch: Orchestrator, agent_id: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(orch.store, "get_project", lambda name: None)
    with pytest.raises(OrchestratorError, match="unknown project"):
        orch.code_tree(agent_id)


# -- nothing runs, nothing is written -------------------------------------------------------


def test_the_repositorys_own_configuration_runs_nothing(
    orch: Orchestrator, agent_id: str, tree: Path, tmp_path: Path
) -> None:
    """An external diff, a textconv driver, a clean filter (command and process) and an
    fsmonitor hook that each leave a marker: plain git runs them, the code view does not."""
    marks = tmp_path / "marks"
    marks.mkdir()
    (tree / "app.txt").write_text("one\n")
    commit_all(tree)
    hook = lambda name: f"touch '{marks / name}'; cat"  # noqa: E731
    run_git(tree, "config", "diff.external", f"touch '{marks / 'external'}' #")
    run_git(tree, "config", "diff.evil.textconv", hook("textconv"))
    run_git(tree, "config", "filter.evil.clean", hook("clean"))
    run_git(tree, "config", "filter.other.process", f"touch '{marks / 'process'}'")
    run_git(tree, "config", "core.fsmonitor", f"touch '{marks / 'fsmonitor'}'; true #")
    (tree / ".gitattributes").write_text("*.txt diff=evil filter=evil\n*.md filter=other\n")
    (tree / "app.txt").write_text("one\ntwo\n")
    (tree / "readme.md").write_text("x\n")
    run_git(tree, "add", "readme.md")
    (tree / "readme.md").write_text("y\n")
    os.utime(tree / "app.txt", (1, 1))
    os.utime(tree / "readme.md", (1, 1))

    # The control: plain git runs them.
    subprocess.run(["git", "-C", str(tree), "status"], capture_output=True)
    subprocess.run(["git", "-C", str(tree), "diff", "HEAD"], capture_output=True)
    textconv = ("diff", "--no-ext-diff", "--textconv", "HEAD")
    subprocess.run(["git", "-C", str(tree), *textconv], capture_output=True)
    assert {p.name for p in marks.iterdir()} == {
        "external",
        "fsmonitor",
        "textconv",
        "clean",
        "process",
    }
    shutil.rmtree(marks)
    marks.mkdir()

    rows = by_path(orch.code_changes(agent_id))
    assert rows["app.txt"]["added"] == 2
    assert "+two" in orch.code_patch(agent_id, "app.txt")["patch"]
    assert "+y" in orch.code_patch(agent_id, "readme.md")["patch"]
    assert orch.code_tree(agent_id)["files"]
    assert orch.code_file(agent_id, "app.txt")["content"] == "one\ntwo\n"

    assert list(marks.iterdir()) == []


def test_reading_writes_nothing_to_the_worktree_or_its_index(
    orch: Orchestrator, agent_id: str, tree: Path
) -> None:
    (tree / "a.txt").write_text("a\n")
    commit_all(tree)
    (tree / "a.txt").write_text("b\n")
    (tree / "u.txt").write_text("u\n")
    os.utime(tree / "a.txt", (1, 1))
    index = Path(
        run_git(tree, "rev-parse", "--path-format=absolute", "--git-path", "index").strip()
    )

    def snapshot() -> dict[str, tuple[int, int]]:
        found = {str(p): (p.stat().st_mtime_ns, p.stat().st_size) for p in tree.rglob("*")}
        return {**found, "index": (index.stat().st_mtime_ns, index.stat().st_size)}

    before = snapshot()
    orch.code_changes(agent_id)
    orch.code_patch(agent_id, "a.txt")
    orch.code_patch(agent_id, "u.txt")
    orch.code_tree(agent_id)
    orch.code_file(agent_id, "a.txt")
    assert snapshot() == before


# -- submodules: never entered ------------------------------------------------------------

SUBMODULE_HOOKS = ("external", "textconv", "clean", "process", "fsmonitor")


def armed_submodule(orch: Orchestrator, project: str, repo: Path, tmp_path: Path):
    """An agent whose worktree has the submodule `sub` (in the base too), moved to a new
    commit and with a modified file, whose own configuration names an external diff, a
    textconv driver, a clean filter, a filter process and an fsmonitor hook that each leave
    a marker; the superproject asks for submodule diffs and never ignores them.
    (agent id, worktree, markers directory)."""
    marks = tmp_path / "marks"
    marks.mkdir()
    source = tmp_path / "library"
    source.mkdir()
    run_git(source, "init", "-q", "-b", "main")
    (source / "lib.txt").write_text("one\n")
    commit_all(source, "library")
    run_git(repo, "-c", "protocol.file.allow=always", "submodule", "-q", "add", str(source), "sub")
    (repo / "app.txt").write_text("app\n")
    commit_all(repo, "with the library")
    agent = orch.create_agent(project, name="sub-user")
    git.add_worktree(repo, agent.cwd, agent.branch, "main")
    tree = Path(agent.cwd)
    run_git(tree, "-c", "protocol.file.allow=always", "submodule", "-q", "update", "--init")
    run_git(tree, "config", "diff.submodule", "diff")
    run_git(tree, "config", "diff.ignoreSubmodules", "none")
    run_git(tree, "config", "submodule.sub.ignore", "none")
    sub = tree / "sub"
    hook = lambda name: f"touch '{marks / name}'; cat"  # noqa: E731
    run_git(sub, "config", "diff.external", f"touch '{marks / 'external'}' #")
    run_git(sub, "config", "diff.evil.textconv", hook("textconv"))
    run_git(sub, "config", "filter.evil.clean", hook("clean"))
    run_git(sub, "config", "filter.other.process", f"touch '{marks / 'process'}'")
    run_git(sub, "config", "core.fsmonitor", f"touch '{marks / 'fsmonitor'}'; true #")
    (sub / ".gitattributes").write_text("*.txt diff=evil filter=evil\n*.md filter=other\n")
    (sub / "lib.txt").write_text("one\ntwo\n")
    commit_all(sub, "library moves on")
    (sub / "lib.txt").write_text("one\ntwo\nthree\n")
    (sub / "notes.md").write_text("x\n")
    run_git(sub, "add", "notes.md")
    (sub / "notes.md").write_text("y\n")
    for name in ("lib.txt", "notes.md"):
        os.utime(sub / name, (1, 1))
    (tree / "app.txt").write_text("app\nchanged\n")
    return agent.id, tree, marks


def test_a_submodule_is_compared_by_its_commit_and_runs_nothing_of_its_own(
    orch: Orchestrator, project: str, repo: Path, tmp_path: Path
) -> None:
    agent_id, tree, marks = armed_submodule(orch, project, repo, tmp_path)
    base = run_git(tree, "merge-base", "main", "HEAD").strip()

    # The control: plain git runs all of them, in the submodule.
    subprocess.run(["git", "-C", str(tree), "diff", base], capture_output=True)
    textconv = ("diff", "--no-ext-diff", "--textconv", "HEAD~1")
    subprocess.run(["git", "-C", str(tree / "sub"), *textconv], capture_output=True)
    assert {p.name for p in marks.iterdir()} == set(SUBMODULE_HOOKS)
    shutil.rmtree(marks)
    marks.mkdir()

    rows = by_path(orch.code_changes(agent_id))
    module = orch.code_patch(agent_id, "sub")
    plain = orch.code_patch(agent_id, "app.txt")
    files = orch.code_tree(agent_id)
    shown = orch.code_file(agent_id, "app.txt")
    for path in ("sub", "sub/lib.txt"):
        with pytest.raises(OrchestratorError):
            orch.code_file(agent_id, path)

    assert list(marks.iterdir()) == []
    assert rows["sub"]["submodule"] is True and rows["sub"]["status"] == "M"
    assert rows["app.txt"]["submodule"] is False
    assert module["submodule"] is True
    lines = module["patch"].splitlines()
    assert [line[:19] for line in lines if "Subproject" in line] == [
        "-Subproject commit ",
        "+Subproject commit ",
    ]
    assert "lib.txt" not in module["patch"] and "-dirty" not in module["patch"]
    assert "+changed" in plain["patch"]
    assert "sub" in files["files"] and files["submodules"] == ["sub"]
    assert not [path for path in files["files"] if path.startswith("sub/")]
    assert shown["content"] == "app\nchanged\n"


# -- paths only as the file system spells them ---------------------------------------------


def case_insensitive(directory: Path) -> bool:
    probe = directory / "Case-Probe"
    probe.write_text("")
    try:
        return (directory / "case-probe").exists()
    finally:
        probe.unlink()


def test_an_ignored_file_is_not_served_through_a_case_variant(
    orch: Orchestrator, agent_id: str, tree: Path
) -> None:
    """With core.ignorecase off (an agent can set it), `.gitignore`'s `secrets/` does not
    match `SECRETS/`, and a case-insensitive file system opens `secrets/` for it."""
    if not case_insensitive(tree):
        pytest.skip("the temporary directory's file system is case-sensitive: no case alias")
    (tree / ".gitignore").write_text("secrets/\n")
    commit_all(tree)
    run_git(tree, "config", "core.ignorecase", "false")
    (tree / "secrets").mkdir()
    (tree / "secrets" / "key").write_text("PRIVATE-KEY\n")
    assert (tree / "SECRETS" / "key").read_text() == "PRIVATE-KEY\n"  # the alias opens it

    for path in ("SECRETS/key", "Secrets/key", "secrets/KEY", "secrets/key"):
        for read in (orch.code_file, orch.code_patch):
            with pytest.raises(OrchestratorError):
                read(agent_id, path)
    for answer in (orch.code_tree(agent_id), orch.code_changes(agent_id)):
        assert "PRIVATE-KEY" not in json.dumps(answer) and "secrets" not in json.dumps(answer)


def test_unicode_variants_are_refused_and_real_names_are_served(
    orch: Orchestrator, agent_id: str, tree: Path
) -> None:
    precomposed = "café.txt"
    decomposed = "café.txt"
    (tree / precomposed).write_text("coffee\n")

    assert orch.code_file(agent_id, precomposed)["content"] == "coffee\n"
    assert "+coffee" in orch.code_patch(agent_id, precomposed)["patch"]
    assert by_path(orch.code_changes(agent_id))[precomposed]["added"] == 1
    for read in (orch.code_file, orch.code_patch):
        with pytest.raises(OrchestratorError):
            read(agent_id, decomposed)


# -- git's output read only up to a cap ------------------------------------------------------


@pytest.fixture
def reads(monkeypatch: pytest.MonkeyPatch) -> list[tuple[int, int, bool]]:
    """Every capped git call of the code view: (bytes taken from git, cap, cut)."""
    noted: list[tuple[int, int, bool]] = []
    real = git._run_capped

    def spy(*args, **kwargs):
        code, out, err, cut = real(*args, **kwargs)
        noted.append((len(out), args[4] if len(args) > 4 else kwargs["cap"], cut))
        return code, out, err, cut

    monkeypatch.setattr(git, "_run_capped", spy)
    return noted


def test_a_patch_is_read_from_git_only_up_to_its_cap(
    orch: Orchestrator, agent_id: str, tree: Path, monkeypatch: pytest.MonkeyPatch, reads
) -> None:
    monkeypatch.setattr(code_view, "MAX_PATCH_BYTES", 4096)
    (tree / "huge.txt").write_text("".join(f"line {n:06d} of a long file\n" for n in range(20000)))
    commit_all(tree)  # about 500 KB of patch

    patch = orch.code_patch(agent_id, "huge.txt")

    assert patch["truncated"] is True and patch["lines"] is None
    assert patch["max_bytes"] == 4096 and 0 < len(patch["patch"].encode()) <= 4096
    taken = [(read, cap) for read, cap, cut in reads if cut]
    assert taken == [(4096, 4096)]
    assert all(read <= cap for read, cap, _ in reads)


def test_long_listings_are_read_from_git_only_up_to_their_cap(
    orch: Orchestrator, agent_id: str, tree: Path, monkeypatch: pytest.MonkeyPatch, reads
) -> None:
    monkeypatch.setattr(code_view, "LIST_BYTES", 2048)
    (tree / "many").mkdir()
    for n in range(400):  # about 9 KB of names
        (tree / "many" / f"file-{n:04d}.txt").write_text("x\n")

    changes = orch.code_changes(agent_id)
    files = orch.code_tree(agent_id)

    for answer, paths in ((changes, list(by_path(changes))), (files, files["files"])):
        assert answer["truncated"] is True and answer["total"] is None
        assert 0 < len(paths) < 400
        assert all(path.startswith("many/file-") for path in paths)
    assert all(read <= cap for read, cap, _ in reads)
    assert any(cut and cap == 2048 for _, cap, cut in reads)


def test_the_capped_reader_stops_git_and_reports_the_cut(repo: Path) -> None:
    for n in range(50):
        (repo / f"f{n:03d}.txt").write_text("x\n")
    env = git._own_env()

    code, out, _, cut = git._run_capped(repo, ("ls-files", "-o", "-z"), 20, env, 100)
    whole = git._run_capped(repo, ("ls-files", "-o", "-z"), 20, env, 1 << 20)

    assert (code, len(out), cut) == (None, 100, True)
    assert whole[0] == 0 and whole[3] is False and len(whole[1]) == 50 * 9
    assert whole[1].startswith(out)


# -- the base: any revision, else the project's base branch ----------------------------------


@pytest.mark.parametrize("kind", ["remote", "tag", "commit", "deleted-branch"])
def test_the_base_may_be_any_revision_and_falls_back_to_the_project_base(
    orch: Orchestrator, project: str, repo: Path, kind: str
) -> None:
    older = run_git(repo, "rev-parse", "HEAD").strip()
    names = {"remote": "origin/main", "tag": "v1", "commit": older, "deleted-branch": "feature"}
    run_git(repo, "update-ref", "refs/remotes/origin/main", older)
    run_git(repo, "tag", "v1", older)
    run_git(repo, "branch", "feature", older)
    (repo / "later.txt").write_text("on main after the base\n")
    commit_all(repo, "later on main")
    agent = orch.create_agent(project, name="based", base=names[kind])
    assert agent.base_ref == names[kind]
    git.add_worktree(repo, agent.cwd, agent.branch, "main")
    tree = Path(agent.cwd)
    (tree / "mine.txt").write_text("mine\n")
    if kind == "deleted-branch":
        run_git(repo, "branch", "-D", "feature")

    answer = orch.code_changes(agent.id)

    if kind == "deleted-branch":
        assert (answer["base"], answer["base_fallback"]) == ("main", True)
        assert answer["requested_base"] == "feature"
        assert list(by_path(answer)) == ["mine.txt"]
    else:
        assert (answer["base"], answer["base_fallback"]) == (names[kind], False)
        assert list(by_path(answer)) == ["later.txt", "mine.txt"]
    assert orch.code_patch(agent.id, "mine.txt")["base"] == answer["base"]


def test_without_any_base_the_changes_explain_and_the_files_still_work(
    orch: Orchestrator, agent_id: str, tree: Path, repo: Path
) -> None:
    run_git(repo, "checkout", "-q", "-b", "elsewhere")
    run_git(repo, "branch", "-D", "main")
    (tree / "a.txt").write_text("a\n")

    with pytest.raises(OrchestratorError, match="no base to compare with"):
        orch.code_changes(agent_id)
    with pytest.raises(OrchestratorError, match="no base to compare with"):
        orch.code_patch(agent_id, "a.txt")
    assert orch.code_tree(agent_id)["files"] == ["a.txt"]
    assert orch.code_file(agent_id, "a.txt")["content"] == "a\n"


@pytest.mark.parametrize("base", ["--output=/tmp/x", "-p", "main\nHEAD", "HEAD@{-1}x"])
def test_a_base_is_never_read_as_an_option(
    orch: Orchestrator, project: str, repo: Path, base: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A base that names nothing usable falls back to the project's base branch."""
    agent = orch.create_agent(project, name="odd-base")
    git.add_worktree(repo, agent.cwd, agent.branch, "main")
    monkeypatch.setattr(orch, "agent", lambda agent_id: replace(agent, base_ref=base))

    answer = orch.code_changes(agent.id)

    assert (answer["base"], answer["base_fallback"], answer["requested_base"]) == (
        "main",
        True,
        base,
    )


# -- the routes --------------------------------------------------------------------------------


def test_the_routes_answer_get_with_the_core_answers(
    client: TestClient, orch: Orchestrator, agent_id: str, tree: Path
) -> None:
    name = "<script>alert(1)</script> ü.txt"
    (tree / "<script>alert(1)<").mkdir()
    (tree / name).write_text("<b>bold</b>\n")
    base = f"/api/agents/{agent_id}/code"

    changes = client.get(f"{base}/changes")
    patch = client.get(f"{base}/patch", params={"path": name})
    files = client.get(f"{base}/tree")
    shown = client.get(f"{base}/file", params={"path": name})

    for response in (changes, patch, files, shown):
        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-store"
    assert [row["path"] for row in changes.json()["files"]] == [name]
    assert files.json()["files"] == [name]
    assert shown.json()["content"] == "<b>bold</b>\n"
    assert "+<b>bold</b>" in patch.json()["patch"]


@pytest.mark.parametrize(
    ("query", "status"),
    [
        ("/code/file?path=../x", 400),
        ("/code/file?path=%2Fetc%2Fpasswd", 400),
        ("/code/file?path=a%00b", 400),
        ("/code/file", 400),
        ("/code/patch?path=.git%2Fconfig", 400),
        ("/code/file?path=missing.txt", 400),
    ],
)
def test_refusals_are_short_client_errors(
    client: TestClient, agent_id: str, query: str, status: int
) -> None:
    response = client.get(f"/api/agents/{agent_id}{query}")
    assert response.status_code == status
    assert set(response.json()) == {"error"} and "Traceback" not in response.text


def test_unknown_agents_and_missing_worktrees_are_client_errors(
    client: TestClient, agent_id: str, tree: Path
) -> None:
    assert client.get("/api/agents/nobody-1234/code/tree").status_code == 400
    shutil.rmtree(tree)
    for what in ("changes", "tree", "file?path=a", "patch?path=a"):
        response = client.get(f"/api/agents/{agent_id}/code/{what}")
        assert response.status_code == 400 and "error" in response.json()


def test_a_file_vanishing_mid_request_is_a_client_error(
    client: TestClient, agent_id: str, tree: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tree / "brief.txt").write_text("x\n")

    def vanish(root: Path, path: str, limit: int) -> code_view.Entry:
        (root / path).unlink()
        raise FileNotFoundError(2, "No such file or directory", path)

    monkeypatch.setattr(code_view, "read_entry", vanish)
    response = client.get(f"/api/agents/{agent_id}/code/file", params={"path": "brief.txt"})
    assert response.status_code == 400 and "does not exist" in response.json()["error"]


@pytest.mark.parametrize("what", ["changes", "patch", "tree", "file"])
def test_the_routes_take_only_get(client: TestClient, agent_id: str, what: str) -> None:
    url = f"/api/agents/{agent_id}/code/{what}"
    headers = {"origin": "http://localhost"}
    for method in ("POST", "PUT", "DELETE", "PATCH"):
        response = client.request(method, url, json={"path": "a"}, headers=headers)
        assert response.status_code == 405, method


@pytest.mark.parametrize("what", ["changes", "tree", "file?path=a.txt"])
def test_other_sites_are_refused(client: TestClient, agent_id: str, tree: Path, what: str) -> None:
    """The dashboard's Host check (DNS rebinding), and for these routes also an Origin or a
    Sec-Fetch-Site of another site, even on GET."""
    (tree / "a.txt").write_text("a\n")
    url = f"/api/agents/{agent_id}/code/{what}"

    assert client.get(url, headers={"host": "evil.example"}).status_code == 403
    assert client.get(url, headers={"origin": "http://evil.example"}).status_code == 403
    assert client.get(url, headers={"origin": "http://localhost:3000"}).status_code == 403
    for site in ("cross-site", "same-site"):
        assert client.get(url, headers={"sec-fetch-site": site}).status_code == 403
    assert client.get(url, headers={"origin": "http://localhost"}).status_code == 200
    assert client.get(url, headers={"sec-fetch-site": "same-origin"}).status_code == 200
    assert client.get(url, headers={"sec-fetch-site": "none"}).status_code == 200


def test_the_existing_requests_compute_nothing_of_the_code_view(
    client: TestClient, agent_id: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refuse(*args: object) -> None:
        raise AssertionError("the code view was computed")

    for name in ("code_changes", "code_patch", "code_tree", "code_file"):
        monkeypatch.setattr(Orchestrator, name, refuse)
    assert client.get("/api/state").status_code == 200
    assert client.get(f"/api/agents/{agent_id}/events").status_code == 200


# -- the page -------------------------------------------------------------------------------------


def page_functions(*names: str) -> str:
    """The named top-level functions and one-line constants of app.js, as source."""
    script = (STATIC / "app.js").read_text()
    parts = []
    for name in names:
        function = re.search(rf"^(?:async )?function {name}\(.*?^\}}\n", script, re.M | re.S)
        constant = re.search(rf"^const {name} = .*?;\n(?=\S|\n)", script, re.M | re.S)
        found = function or constant
        assert found, name
        parts.append(found.group(0))
    return "\n".join(parts)


# A minimal document: elements keep their text and children; markup is refused.
FAKE_DOM = r"""
class Node {
  constructor(tag) { this.tag = tag; this.children = []; this.className = ''; this.text = ''; this.dataset = {}; }
  set textContent(value) { this.text = String(value); this.children = []; }
  get textContent() { return this.text + this.children.map(c => c.textContent).join(''); }
  set innerHTML(value) { throw new Error('markup set: ' + value); }
  append(...nodes) { for (const n of nodes) this.children.push(typeof n === 'string' ? Object.assign(new Node('#text'), { text: n }) : n); }
  replaceChildren(...nodes) { this.children = []; this.append(...nodes); }
}
const document = { createElement: tag => new Node(tag) };
const dump = n => ({ tag: n.tag, cls: n.className, text: n.text, children: n.children.map(dump) });
"""


def run_node(code: str) -> object:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    done = subprocess.run([node, "-e", code], capture_output=True, text=True, check=True)
    return json.loads(done.stdout)


def test_the_page_builds_its_urls_and_notes() -> None:
    source = page_functions("codeUrl", "sizeText", "codeNote", "countText", "changesHead")
    cases = {
        "url": "codeUrl('fix-login-3f2a', 'file', 'a b/<x>&?.txt')",
        "plain": "codeUrl('fix-login-3f2a', 'tree')",
        "list": "codeNote({max_files: 1000, files: [1, 2], total: 1500, truncated: true})",
        "whole": "codeNote({max_files: 1000, files: [1], total: 1, truncated: false})",
        "patch": "codeNote({max_lines: 2000, lines: 4000, truncated: true, patch: ''})",
        "file": "codeNote({max_bytes: 524288, size: 2097152, truncated: true, content: ''})",
        "binary": "codeNote({binary: true})",
        "large": "codeNote({too_large: true})",
        "link": "codeNote({kind: 'symlink', target: '../x'})",
        "counts": "[countText({added: 3, removed: 1}), countText({binary: true}),"
        " countText({too_large: true}), countText({added: null}),"
        " countText({submodule: true, added: 1, removed: 1})]",
        "unknown": "codeNote({max_files: 1000, files: [1, 2, 3], total: null, truncated: true})",
        "bytes": "codeNote({max_lines: 2000, lines: null, truncated: true, max_bytes: 1048576})",
        "module": "codeNote({max_lines: 2000, lines: 2, truncated: false, submodule: true})",
        "head": "[changesHead({files: [1, 2], total: 2, base: 'origin/main'}),"
        " changesHead({files: [1], total: null, base: 'v1'}), changesHead({files: [], base: 'main'})]",
    }
    code = source + "\nprocess.stdout.write(JSON.stringify({"
    code += ", ".join(f"{key}: {expr}" for key, expr in cases.items()) + "}));"

    assert run_node(code) == {
        "url": "/api/agents/fix-login-3f2a/code/file?path=a%20b%2F%3Cx%3E%26%3F.txt",
        "plain": "/api/agents/fix-login-3f2a/code/tree",
        "list": "Showing 2 of 1500 files (the first 1000).",
        "whole": "",
        "patch": "Patch cut: showing at most 2000 of 4000 lines.",
        "file": "File cut: showing the first 512 KB of 2.0 MB.",
        "binary": "Binary file: not shown.",
        "large": "Too large to show.",
        "link": "Symbolic link to ../x (not followed).",
        "counts": ["+3 −1", "binary", "too large", "", "submodule"],
        "unknown": "Showing 3 files: the list was cut.",
        "bytes": "Patch cut: showing its first 1.0 MB.",
        "module": "Submodule: only its commit is compared.",
        "head": [
            "2 files changed against origin/main, committed and uncommitted",
            "1+ files changed against v1, committed and uncommitted",
            "No changes against main",
        ],
    }


def test_the_page_nests_paths_into_folders() -> None:
    source = page_functions("fileTree")
    code = source + (
        "\nconst plain = n => ({dirs: Object.fromEntries([...n.dirs].map(([k, v]) => [k, plain(v)])),"
        " files: n.files});"
        "\nprocess.stdout.write(JSON.stringify(plain(fileTree(['a.txt', 'src/x.py', 'src/lib/y.py']))));"
    )
    assert run_node(code) == {
        "dirs": {
            "src": {
                "dirs": {"lib": {"dirs": {}, "files": [{"name": "y.py", "path": "src/lib/y.py"}]}},
                "files": [{"name": "x.py", "path": "src/x.py"}],
            }
        },
        "files": [{"name": "a.txt", "path": "a.txt"}],
    }


def test_the_page_renders_patches_as_text_never_as_markup() -> None:
    source = page_functions("el", "patchClass", "renderPatch")
    patch = (
        "diff --git a/<script>.html b/<script>.html\n--- /dev/null\n+++ b/<script>.html\n"
        "@@ -0,0 +1,2 @@\n+<script>alert(1)</script>\n-<img src=x onerror=alert(1)>\n context\n"
    )
    code = (
        FAKE_DOM
        + source
        + f"\nprocess.stdout.write(JSON.stringify(dump(renderPatch({json.dumps(patch)}))));"
    )

    pre = run_node(code)

    assert (pre["tag"], pre["cls"]) == ("pre", "code-text patch")
    lines = [(child["cls"], child["text"]) for child in pre["children"]]
    assert lines == [
        ("meta", "diff --git a/<script>.html b/<script>.html\n"),
        ("meta", "--- /dev/null\n"),
        ("meta", "+++ b/<script>.html\n"),
        ("hunk", "@@ -0,0 +1,2 @@\n"),
        ("add", "+<script>alert(1)</script>\n"),
        ("del", "-<img src=x onerror=alert(1)>\n"),
        ("", " context\n"),
    ]


def test_the_page_shows_names_and_contents_only_as_text() -> None:
    """Every code-view function that puts a name, a line or a message into the page does it
    through el() (textContent); none of them sets markup."""
    script = (STATIC / "app.js").read_text()
    start = script.index("// -- the code view of a worktree")
    section = script[start : script.index("// -- model and effort", start)]
    assert "innerHTML" not in section and "insertAdjacentHTML" not in section
    assert "outerHTML" not in section and "document.write" not in section


def test_the_page_declares_each_top_level_name_once() -> None:
    """A classic script's later function of the same name silently replaces the earlier one
    (the folder picker's renderFolder would replace the code view's)."""
    script = (STATIC / "app.js").read_text()
    pattern = r"^(?:async )?function ([\w$]+)|^(?:const|let|var) ([\w$]+)"
    names = [a or b for a, b in re.findall(pattern, script, re.M)]
    assert len(names) > 100
    assert sorted({n for n in names if names.count(n) > 1}) == []
    assert "renderCodeFolder" in names
