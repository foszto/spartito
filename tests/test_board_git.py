"""The live git state of board items (git.branch_state, core/board_git): the cache, its
pass (budget, stamp, safety) and the automatic move to done of merged items."""

from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest

from orchestrator import board, git
from orchestrator.core import board_git
from orchestrator.models import Agent, Project
from orchestrator.store import SCHEMA, Store

GIT_USER = ["-c", "user.name=t", "-c", "user.email=t@example.com"]
MASTER_ERA_SCHEMA = Path(__file__).parent / "fixtures" / "master_era_schema.sql"


def run_git(path: str | Path, *args: str) -> str:
    done = subprocess.run(
        ["git", "-C", str(path), *GIT_USER, *args], check=True, capture_output=True, text=True
    )
    return done.stdout.strip()


def commit(path: str | Path, name: str = "file.txt") -> str:
    (Path(path) / name).write_text(name)
    run_git(path, "add", name)
    run_git(path, "commit", "-q", "-m", f"add {name}")
    return run_git(path, "rev-parse", "HEAD")


def init_repo(path: Path) -> Path:
    path.mkdir()
    run_git(path, "init", "-q", "-b", "main")
    run_git(path, "commit", "-q", "--allow-empty", "-m", "init")
    return path


@pytest.fixture
def store() -> Store:
    return Store()  # in the test's ORCH_HOME


@pytest.fixture
def project(store: Store, repo: Path) -> Project:
    return store.add_project(Project("demo", str(repo), "main", "fake"))


def worker(store: Store, project: Project, cwd: Path, branch: str, agent_id: str = "w-1") -> Agent:
    """An agent of `project` owning a worktree at `cwd` on `branch` (created here)."""
    run_git(project.path, "worktree", "add", "-q", "-b", branch, str(cwd), "main")
    return store.add_agent(
        Agent(agent_id, project.name, "w", "worker", "fake", str(cwd), branch=branch)
    )


def item(store: Store, project: Project, branch: str | None, state: str = "ready") -> board.Item:
    return board.add(store, project.name, f"work on {branch}", state=state, branch=branch)


def live(store: Store, entry: board.Item) -> dict[str, Any] | None:
    return board_git.states(store, [board.get(store, entry.id)]).get(entry.id)


def state_of(store: Store, entry: board.Item) -> str:
    return board.get(store, entry.id).state


class Recorder:
    """A recording hook for board_git.check: what it was called with."""

    def __init__(self) -> None:
        self.calls: list[tuple[int, str, str, bool]] = []

    def __call__(self, conn: sqlite3.Connection, before: board.Item, tip: str) -> None:
        moved = conn.execute("SELECT state FROM board_items WHERE id = ?", (before.id,)).fetchone()
        assert moved["state"] == "done"  # called after the move, in its transaction
        self.calls.append((before.id, before.state, tip, conn.in_transaction))


@pytest.fixture
def spy(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, tuple[str, ...]]]:
    """Every git call of git._run: (its -C directory, its arguments)."""
    calls: list[tuple[str, tuple[str, ...]]] = []
    real = git._run

    def recorded(repo: str | Path, args: tuple[str, ...], timeout: float | None):
        calls.append((str(repo), args))
        return real(repo, args, timeout)

    monkeypatch.setattr(git, "_run", recorded)
    return calls


# -- git.branch_state --------------------------------------------------------------------


def test_branch_state_counts_ahead_behind_and_changes(
    store: Store, project: Project, tmp_path: Path
) -> None:
    wt = tmp_path / "wt"
    worker_agent = worker(store, project, wt, "feat")
    fresh = git.branch_state(project.path, "feat", "main", worktree=wt)
    assert fresh == git.BranchState(run_git(wt, "rev-parse", "HEAD"), 0, 0, 0, True)

    tip = commit(wt)
    commit(project.path, "on-main.txt")
    (wt / "loose.txt").write_text("x")
    state = git.branch_state(project.path, "feat", "main", worktree=worker_agent.cwd)

    assert state == git.BranchState(tip, 1, 1, 1, False)
    assert git.branch_state(project.path, "feat", "main").dirty is None  # no worktree asked
    missing = git.branch_state(project.path, "gone", "main")
    assert missing == git.BranchState()  # no branch, no recorded tip: unknown
    assert git.branch_state(project.path, "gone", "main", last_tip=tip).merged is False


def test_branch_state_reports_errors_instead_of_raising(project: Project, tmp_path: Path) -> None:
    assert "not a usable branch name" in git.branch_state(project.path, "-x", "main").error
    assert "not a commit id" in git.branch_state(project.path, "b", "main", last_tip="HEAD").error
    elsewhere = git.branch_state(tmp_path / "nowhere", "b", "main").error
    assert elsewhere and git.branch_state(project.path, "b", "nope", last_tip="a" * 40).error


@pytest.mark.parametrize(
    "name",
    ["-x", "--output=x", "a..b", "a b", "a\tb", "a\nb", "a\x01b", "a\x7fb", "a~1", "a^", "a:b",
     "a?", "a*", "a[0]", "a\\b", "a@{1}", "", " "],
)  # fmt: skip
def test_hostile_branch_names_are_refused_without_git(
    store: Store, project: Project, spy: list, name: str
) -> None:
    assert not git.valid_branch(name)
    with store.connection() as conn:  # board.add strips whitespace; the column takes anything
        conn.execute(
            "INSERT INTO board_items (project, title, state, branch, created_at, updated_at)"
            " VALUES (?, 't', 'ready', ?, 't', 't')",
            (project.name, name),
        )
    board_git.check(store)

    assert spy == []
    assert all(row["error"] for row in store.board_git().values())


def test_ordinary_branch_names_are_accepted() -> None:
    for name in ("main", "feature/login", "orch/board-git-947b", "v1.2", "a-b_c", "fix@home"):
        assert git.valid_branch(name), name


# -- the pass and the cache --------------------------------------------------------------


def test_a_fresh_branch_is_not_merged(store: Store, project: Project) -> None:
    run_git(project.path, "branch", "feat")
    entry = item(store, project, "feat")

    assert board_git.check(store) == {"checked": [entry.id], "done": []}

    state = live(store, entry)
    assert state is not None
    assert (state["ahead"], state["behind"], state["merged"], state["error"]) == (0, 0, False, None)
    assert store.board_git()[entry.id]["seen_ahead"] == 0
    assert board_git.check(store) == {"checked": [entry.id], "done": []}
    assert state_of(store, entry) == "ready"  # never seen ahead: never counts as merged


def test_commits_give_ahead_and_a_merge_is_seen(
    store: Store, project: Project, tmp_path: Path
) -> None:
    wt = tmp_path / "wt"
    worker(store, project, wt, "feat")
    entry = item(store, project, "feat", "in_progress")
    board_git.check(store)
    tip = commit(wt)
    commit(wt, "second.txt")
    board_git.check(store)

    state = live(store, entry)
    assert state is not None and (state["ahead"], state["dirty"], state["merged"]) == (2, 0, False)
    row = store.board_git()[entry.id]
    assert row["seen_ahead"] == 1 and row["tip"] != tip

    assert state_of(store, entry) == "in_progress"

    run_git(project.path, "merge", "-q", "--no-ff", "-m", "merge feat", "feat")
    recorder = Recorder()
    assert board_git.check(store, recorder) == {"checked": [entry.id], "done": [entry.id]}

    state = live(store, entry)
    assert state is not None and (state["ahead"], state["behind"], state["merged"]) == (0, 1, True)
    assert state_of(store, entry) == "done"
    assert recorder.calls == [(entry.id, "in_progress", row["tip"], True)]


def test_a_branch_deleted_after_the_merge_is_merged_through_its_recorded_tip(
    store: Store, project: Project
) -> None:
    run_git(project.path, "checkout", "-q", "-b", "feat")
    tip = commit(project.path)
    run_git(project.path, "checkout", "-q", "main")
    entry = item(store, project, "feat")
    board_git.check(store)
    run_git(project.path, "merge", "-q", "--no-ff", "-m", "merge", "feat")
    run_git(project.path, "branch", "-q", "-D", "feat")

    board_git.check(store)

    row = store.board_git()[entry.id]
    assert (row["tip"], row["ahead"], row["merged_at"] is not None) == (tip, None, True)
    assert state_of(store, entry) == "done"


def test_a_deleted_branch_whose_tip_is_not_in_the_base_stays(
    store: Store, project: Project
) -> None:
    run_git(project.path, "checkout", "-q", "-b", "feat")
    commit(project.path)
    run_git(project.path, "checkout", "-q", "main")
    entry = item(store, project, "feat")
    board_git.check(store)
    run_git(project.path, "branch", "-q", "-D", "feat")  # deleted, never merged

    assert board_git.check(store)["done"] == []
    assert state_of(store, entry) == "ready" and live(store, entry)["merged"] is False


def test_a_squash_merge_is_not_merged(store: Store, project: Project) -> None:
    run_git(project.path, "checkout", "-q", "-b", "feat")
    commit(project.path)
    run_git(project.path, "checkout", "-q", "main")
    entry = item(store, project, "feat")
    board_git.check(store)
    run_git(project.path, "merge", "-q", "--squash", "feat")
    run_git(project.path, "commit", "-q", "-m", "squashed")

    board_git.check(store)

    state = live(store, entry)
    assert state is not None and (state["ahead"], state["merged"]) == (1, False)
    assert state_of(store, entry) == "ready"  # accepted: a squash merge is moved by hand


def test_a_dirty_worktree_is_not_merged(store: Store, project: Project, tmp_path: Path) -> None:
    wt = tmp_path / "wt"
    worker(store, project, wt, "feat")
    commit(wt)
    entry = item(store, project, "feat")
    board_git.check(store)
    run_git(project.path, "merge", "-q", "--no-ff", "-m", "merge", "feat")
    (wt / "half-done.txt").write_text("x")

    board_git.check(store)

    state = live(store, entry)
    assert state is not None and (state["ahead"], state["dirty"], state["merged"]) == (0, 1, False)
    assert state_of(store, entry) == "ready"

    (wt / "half-done.txt").unlink()
    assert board_git.check(store)["done"] == [entry.id]


def test_an_item_moved_back_out_of_done_stays(
    store: Store, project: Project, tmp_path: Path
) -> None:
    wt = tmp_path / "wt"
    worker(store, project, wt, "feat")
    commit(wt)
    entry = item(store, project, "feat", "in_progress")
    board_git.check(store)
    run_git(project.path, "merge", "-q", "--no-ff", "-m", "merge", "feat")
    assert board_git.check(store)["done"] == [entry.id]

    board.update(store, entry.id, {"state": "in_progress"})  # by hand: not finished after all
    assert board_git.check(store) == {"checked": [entry.id], "done": []}
    assert state_of(store, entry) == "in_progress" and live(store, entry)["merged"] is True

    commit(wt, "more.txt")  # more work on it, merged again: done again
    board_git.check(store)
    run_git(project.path, "merge", "-q", "--no-ff", "-m", "merge again", "feat")
    assert board_git.check(store)["done"] == [entry.id]


def test_a_failing_recorder_leaves_the_item_for_the_next_pass(
    store: Store, project: Project, tmp_path: Path
) -> None:
    wt = tmp_path / "wt"
    worker(store, project, wt, "feat")
    commit(wt)
    entry, other = item(store, project, "feat"), item(store, project, "feat")
    board_git.check(store)
    run_git(project.path, "merge", "-q", "--no-ff", "-m", "merge", "feat")

    def broken(conn: sqlite3.Connection, before: board.Item, tip: str) -> None:
        if before.id == entry.id:
            raise RuntimeError("history is gone")

    assert board_git.check(store, broken)["done"] == [other.id]
    assert state_of(store, entry) == "ready"
    assert "history is gone" in store.board_git()[entry.id]["error"]
    assert board_git.check(store)["done"] == [entry.id]


def test_move_done_only_moves_ready_and_in_progress_items(store: Store, project: Project) -> None:
    entry = item(store, project, "feat", "idea")
    finished = item(store, project, "x", "done")
    row = {name: None for name in Store.BOARD_GIT_COLUMNS}
    row.update(item_id=entry.id, branch="feat", base="main", seen_ahead=1, checked_at="t")

    assert board_git.move_done(store, entry.id, row) is False
    assert state_of(store, entry) == "idea"
    board.update(store, entry.id, {"state": "ready"})
    assert board_git.move_done(store, entry.id, row) is True
    assert board.get(store, entry.id).position == finished.position + 1


def test_only_ready_and_in_progress_items_with_a_branch_are_checked(
    store: Store, project: Project
) -> None:
    for name in ("a", "b", "c", "d"):
        run_git(project.path, "branch", name)
    checked = [item(store, project, "a").id, item(store, project, "b", "in_progress").id]
    item(store, project, "c", "idea")
    item(store, project, "d", "done")
    item(store, project, None)

    assert sorted(board_git.check(store)["checked"]) == sorted(checked)


def test_the_item_checked_longest_ago_goes_first(store: Store, project: Project) -> None:
    run_git(project.path, "branch", "feat")
    first, second = item(store, project, "feat"), item(store, project, "feat")
    board_git.check(store)
    with store.connection() as conn:
        conn.execute("UPDATE board_git SET checked_at = '2000' WHERE item_id = ?", (second.id,))

    assert board_git.check(store)["checked"] == [second.id, first.id]


def test_a_cached_state_for_another_branch_is_ignored(store: Store, project: Project) -> None:
    run_git(project.path, "branch", "feat")
    entry = item(store, project, "feat")
    board_git.check(store)
    assert live(store, entry) is not None

    board.update(store, entry.id, {"branch": "other"})

    assert live(store, entry) is None and board_git.states(store, []) == {}


def test_git_runs_only_in_the_project_and_verified_worktrees(
    store: Store, project: Project, tmp_path: Path, spy: list
) -> None:
    wt = tmp_path / "wt"
    worker(store, project, wt, "feat")
    commit(wt)
    stranger = init_repo(tmp_path / "stranger")  # not a worktree of the project
    store.add_agent(Agent("w-2", project.name, "x", "worker", "fake", str(stranger), branch="x"))
    run_git(project.path, "branch", "x")
    other = store.add_project(Project("other", str(init_repo(tmp_path / "other")), "main", "fake"))
    elsewhere = tmp_path / "elsewhere"
    run_git(other.path, "worktree", "add", "-q", "-b", "feat", str(elsewhere), "main")
    store.add_agent(Agent("o-1", other.name, "o", "worker", "fake", str(elsewhere), branch="feat"))
    good, bad = item(store, project, "feat"), item(store, project, "x")

    board_git.check(store)

    places = [where for where, _ in spy]
    assert set(places) == {project.path, str(wt), str(stranger)}
    assert places.count(str(stranger)) == 1  # the verification, nothing after it
    rows = store.board_git()
    assert "is not a worktree of" in rows[bad.id]["error"]
    assert rows[good.id]["error"] is None and rows[good.id]["ahead"] == 1
    for _, args in spy:
        assert not any(arg.startswith("-") and arg not in ALLOWED_OPTIONS for arg in args), args


ALLOWED_OPTIONS = {
    "--path-format=absolute", "--git-common-dir", "--verify", "--quiet", "--show-toplevel",
    "--no-optional-locks", "--porcelain", "--left-right", "--count", "--not", "--",
}  # fmt: skip


def test_at_most_four_git_calls_per_item(
    store: Store, project: Project, tmp_path: Path, spy: list
) -> None:
    wt = tmp_path / "wt"
    worker(store, project, wt, "feat")
    commit(wt)
    run_git(wt, "checkout", "-q", "--detach", "HEAD~1")  # its HEAD is not the branch's tip
    item(store, project, "feat")

    board_git.check(store)

    assert len(spy) == 4


@pytest.fixture
def hanging_git(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A `git` first on PATH that never answers."""
    bin_dir = tmp_path / "hanging-bin"
    bin_dir.mkdir()
    fake = bin_dir / "git"
    fake.write_text(f"#!{sys.executable}\nimport time\ntime.sleep(60)\n")
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")


def test_a_pass_ends_within_its_budget(
    store: Store, project: Project, monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest
) -> None:
    for name in ("a", "b", "c"):
        run_git(project.path, "branch", name)
        item(store, project, name)
    request.getfixturevalue("hanging_git")
    monkeypatch.setattr(board_git, "SYNC_BUDGET", 1.0)

    started = time.monotonic()
    result = board_git.sync(store)
    took = time.monotonic() - started

    assert took < 1.0 + git.KILL_GRACE + 0.5
    assert len(result["checked"]) == 1
    assert "no answer within" in store.board_git()[result["checked"][0]]["error"]


def test_the_stamp_gates_a_second_pass(store: Store, project: Project) -> None:
    run_git(project.path, "branch", "feat")
    entry = item(store, project, "feat")

    assert board_git.sync(store) == {"checked": [entry.id], "done": []}
    assert board_git.sync(store) == {"checked": [], "done": []}  # within SYNC_EVERY

    stamp = Path(os.environ["ORCH_HOME"]) / board_git.STAMP
    stamp.write_text(f"{time.time() - board_git.SYNC_EVERY - 1:.3f}\n")
    assert board_git.sync(store) == {"checked": [entry.id], "done": []}


def test_a_pass_never_raises(store: Store, monkeypatch: pytest.MonkeyPatch) -> None:
    def broken(*args: Any) -> list:
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(board, "listing", broken)

    assert board_git.sync(store) == {"checked": [], "done": []}


# -- older databases ---------------------------------------------------------------------


@pytest.mark.parametrize("schema", ["master", "master-era"])
def test_an_existing_database_opens_with_the_cache_table(
    tmp_path: Path, repo: Path, schema: str
) -> None:
    path = tmp_path / "old.db"
    with sqlite3.connect(path) as conn:
        conn.executescript(SCHEMA if schema == "master" else MASTER_ERA_SCHEMA.read_text())
        conn.execute("DROP TABLE IF EXISTS board_git")  # the schema before this table
        conn.execute("INSERT INTO projects VALUES ('demo', ?, 'main', 'fake', 't')", (str(repo),))
    conn.close()
    if schema == "master":
        with sqlite3.connect(path) as conn:
            conn.execute(
                "INSERT INTO board_items (project, title, state, branch, created_at, updated_at)"
                " VALUES ('demo', 'old item', 'ready', 'feat', 't', 't')"
            )
        conn.close()
    run_git(repo, "branch", "feat")

    store = Store(path)
    if schema != "master":
        board.add(store, "demo", "old item", state="ready", branch="feat")
    (entry,) = board.listing(store)

    assert board_git.check(store) == {"checked": [entry.id], "done": []}
    assert live(store, entry) is not None
    Store(path)  # opening it again changes nothing
    assert list(store.board_git()) == [entry.id]
