"""The live git state of board items (git.branch_state, core/board_git): the cache, its
pass (budget, stamp, safety)."""

from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
import time
from datetime import UTC, datetime
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


def row_of(store: Store, entry: board.Item) -> dict[str, Any]:
    return store.board_git()[entry.id]


class Spy(list):
    """Every git call of git._run: (its -C directory, its arguments); `envs` their env."""

    envs: list[dict[str, str] | None]


@pytest.fixture
def spy(monkeypatch: pytest.MonkeyPatch) -> Spy:
    calls = Spy()
    calls.envs = []
    real = git._run

    def recorded(repo: str | Path, args: tuple[str, ...], timeout: float | None, env=None):
        calls.append((str(repo), args))
        calls.envs.append(env)
        return real(repo, args, timeout, env)

    monkeypatch.setattr(git, "_run", recorded)
    return calls


# -- git.branch_state --------------------------------------------------------------------


def test_branch_state_counts_ahead_behind_and_changes(
    store: Store, project: Project, tmp_path: Path
) -> None:
    wt = tmp_path / "wt"
    worker_agent = worker(store, project, wt, "feat")
    fresh = git.branch_state(project.path, "feat", "main", worktree=wt)
    start = run_git(wt, "rev-parse", "HEAD")
    assert fresh == git.BranchState(start, 0, 0, 0, False)  # never ahead: not merged

    tip = commit(wt)
    commit(project.path, "on-main.txt")
    (wt / "loose.txt").write_text("x")
    state = git.branch_state(project.path, "feat", "main", worktree=worker_agent.cwd)

    assert state == git.BranchState(tip, 1, 1, 1, False)
    assert git.branch_state(project.path, "feat", "main").dirty is None  # no worktree asked
    assert git.branch_state(project.path, "gone", "main") == git.BranchState()  # no branch
    assert git.branch_state(project.path, "gone", "main", ahead_tip=tip).merged is False

    (wt / "loose.txt").unlink()
    run_git(project.path, "merge", "-q", "--no-ff", "-m", "merge", "feat")
    merged = git.branch_state(project.path, "feat", "main", worktree=wt, ahead_tip=tip)
    assert merged == git.BranchState(tip, 0, 2, 0, True)
    assert git.branch_state(project.path, "feat", "main", ahead_tip=start).merged is True
    assert git.branch_state(project.path, "feat", "main").merged is False  # no ahead tip
    unknown = git.branch_state(project.path, "feat", "main", ahead_tip="1" * 40)
    assert (unknown.merged, unknown.error) == (False, None)  # a tip git no longer has


def test_branch_state_reports_errors_instead_of_raising(project: Project, tmp_path: Path) -> None:
    assert "not a usable branch name" in git.branch_state(project.path, "-x", "main").error
    assert "not a commit id" in git.branch_state(project.path, "b", "main", ahead_tip="HEAD").error
    assert git.branch_state(tmp_path / "nowhere", "b", "main").error
    for tip in (None, "a" * 40):
        no_base = git.branch_state(project.path, "main", "nope", ahead_tip=tip)
        assert no_base.error == f"there is no branch nope in {project.path}"


def test_branch_state_ignores_variables_that_point_git_elsewhere(
    store: Store, project: Project, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, spy: Spy
) -> None:
    wt = tmp_path / "wt"
    worker(store, project, wt, "feat")
    tip = commit(wt)
    decoy = init_repo(tmp_path / "decoy")
    for name, value in {
        "GIT_DIR": decoy / ".git",
        "GIT_WORK_TREE": decoy,
        "GIT_INDEX_FILE": decoy / ".git" / "index",
        "GIT_COMMON_DIR": decoy / ".git",
        "GIT_OBJECT_DIRECTORY": decoy / ".git" / "objects",
    }.items():
        monkeypatch.setenv(name, str(value))

    state = git.branch_state(project.path, "feat", "main", worktree=wt)

    assert state == git.BranchState(tip, 1, 0, 0, False)
    assert spy.envs and all(env is not None and "GIT_DIR" not in env for env in spy.envs)
    assert all(not set(git.REPOSITORY_ENV) & set(env or {}) for env in spy.envs)
    assert spy.envs[0]["PATH"] == os.environ["PATH"]  # the rest is kept
    spy.envs.clear()
    with pytest.raises(git.GitError):  # the other git calls keep the process's environment
        git.unsaved_work(project.path, wt, "feat", "main")
    assert spy.envs and all(env is None for env in spy.envs)


def test_the_variables_left_out_are_the_ones_git_names() -> None:
    named = subprocess.run(
        ["git", "rev-parse", "--local-env-vars"], capture_output=True, text=True, check=True
    ).stdout.split()
    assert set(named) <= set(git.REPOSITORY_ENV)


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

    assert board_git.check(store) == {"checked": [entry.id]}

    state = live(store, entry)
    assert state is not None
    assert (state["ahead"], state["behind"], state["merged"], state["error"]) == (0, 0, False, None)
    assert row_of(store, entry)["ahead_tip"] is None


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
    row = row_of(store, entry)
    assert row["ahead_tip"] == row["tip"] != tip

    run_git(project.path, "merge", "-q", "--no-ff", "-m", "merge feat", "feat")
    board_git.check(store)

    state = live(store, entry)
    assert state is not None and (state["ahead"], state["behind"], state["merged"]) == (0, 1, True)


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

    row = row_of(store, entry)
    assert (row["tip"], row["ahead"], row["ahead_tip"], row["merged"]) == (tip, None, tip, 1)


def test_commits_added_and_merged_between_two_checks_are_merged(
    store: Store, project: Project, tmp_path: Path
) -> None:
    wt = tmp_path / "wt"
    worker(store, project, wt, "feat")
    seen = commit(wt)
    entry = item(store, project, "feat")
    board_git.check(store)
    commit(wt, "later.txt")  # never seen: merged before the next check
    run_git(project.path, "merge", "-q", "--no-ff", "-m", "merge", "feat")

    board_git.check(store)

    row = row_of(store, entry)
    assert (row["ahead"], row["ahead_tip"], row["merged"]) == (0, seen, 1)


@pytest.mark.parametrize("how", ["branch -f", "reset --hard"])
def test_a_branch_reset_to_the_base_is_not_merged(
    store: Store, project: Project, tmp_path: Path, how: str
) -> None:
    wt = tmp_path / "wt"
    worker(store, project, wt, "feat")
    seen = commit(wt)
    entry = item(store, project, "feat", "in_progress")
    board_git.check(store)
    if how == "branch -f":
        run_git(wt, "checkout", "-q", "--detach")
        run_git(project.path, "branch", "-f", "feat", "main")
        run_git(wt, "checkout", "-q", "feat")
    else:
        run_git(wt, "reset", "-q", "--hard", "main")

    board_git.check(store)

    row = row_of(store, entry)
    assert (row["ahead"], row["dirty"], row["ahead_tip"], row["merged"]) == (0, 0, seen, 0)
    assert live(store, entry)["merged"] is False


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
    (wt / "half-done.txt").unlink()
    board_git.check(store)
    assert live(store, entry)["merged"] is True


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
    "--path-format=absolute", "--git-common-dir", "--verify", "--revs-only", "--show-toplevel",
    "--no-optional-locks", "--porcelain", "--left-right", "--count", "--not", "--",
}  # fmt: skip


def test_at_most_four_git_calls_per_item(
    store: Store, project: Project, tmp_path: Path, spy: list
) -> None:
    wt = tmp_path / "wt"
    worker(store, project, wt, "feat")
    commit(wt)
    entry = item(store, project, "feat")
    board_git.check(store)  # it has an ahead tip from now on
    assert len(spy) == 4
    for detach in (False, True):
        if detach:  # its HEAD is not the branch's tip
            run_git(wt, "checkout", "-q", "--detach", "HEAD~1")
        spy.clear()
        board_git.check(store)
        assert len(spy) == 4 and row_of(store, entry)["error"] is None


def test_one_failing_item_skips_only_itself(
    store: Store, project: Project, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_git(project.path, "branch", "feat")
    first, locked = item(store, project, "feat"), item(store, project, "feat")
    other = store.add_project(Project("other", str(init_repo(tmp_path / "other")), "main", "fake"))
    run_git(other.path, "branch", "feat")
    elsewhere = board.add(store, "other", "elsewhere", state="ready", branch="feat")
    save, agents = store.save_board_git, store.list_agents

    def locked_save(row: dict[str, Any], conn: Any = None, wait: float = 30) -> None:
        assert wait <= board_git.SYNC_BUDGET  # bounded by the pass
        if row["item_id"] == locked.id:
            raise sqlite3.OperationalError("database is locked")
        save(row, conn, wait)

    def broken_agents(project_name: str | None = None) -> list[Agent]:
        if project_name == "demo":
            raise sqlite3.OperationalError("disk I/O error")
        return agents(project_name)

    monkeypatch.setattr(store, "save_board_git", locked_save)
    assert board_git.check(store)["checked"] == [first.id, elsewhere.id]
    assert set(store.board_git()) == {first.id, elsewhere.id}

    monkeypatch.setattr(store, "save_board_git", save)
    monkeypatch.setattr(store, "list_agents", broken_agents)
    assert board_git.check(store)["checked"] == [elsewhere.id]


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

    assert board_git.sync(store) == {"checked": [entry.id]}
    assert board_git.sync(store) == {"checked": []}  # within SYNC_EVERY

    stamp = Path(os.environ["ORCH_HOME"]) / board_git.STAMP
    stamp.write_text(f"{time.time() - board_git.SYNC_EVERY - 1:.3f}\n")
    assert board_git.sync(store) == {"checked": [entry.id]}


def test_last_sync_is_the_time_of_the_last_pass(store: Store) -> None:
    assert board_git.last_sync() is None
    before = datetime.now(UTC).replace(microsecond=0)
    board_git.sync(store)
    synced = board_git.last_sync()
    assert synced is not None and before <= datetime.fromisoformat(synced) <= datetime.now(UTC)
    (Path(os.environ["ORCH_HOME"]) / board_git.STAMP).write_text("garbage")
    assert board_git.last_sync() is None


def test_a_pass_never_raises(store: Store, monkeypatch: pytest.MonkeyPatch) -> None:
    def broken(*args: Any) -> list:
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(board, "listing", broken)

    assert board_git.sync(store) == {"checked": []}


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

    assert board_git.check(store) == {"checked": [entry.id]}
    assert live(store, entry) is not None
    Store(path)  # opening it again changes nothing
    assert list(store.board_git()) == [entry.id]
