"""Git plumbing: repository checks and worktree lifecycle."""

from __future__ import annotations

import contextlib
import fcntl
import os
import signal
import subprocess
import time
from pathlib import Path


class GitError(RuntimeError):
    pass


KILL_GRACE = 1.0  # seconds a killed git gets to close its pipes before it is left behind


def _run(repo: str | Path, args: tuple[str, ...], timeout: float | None) -> tuple[int, str, str]:
    """Run git; returns (exit code, stdout, stderr). With `timeout` it really returns within
    about timeout + KILL_GRACE: git runs in a process group of its own, which is killed on
    timeout (hooks and helpers too), and a process that still holds the pipes (stuck in
    the kernel, or moved to another session) is left behind. Raises GitError on timeout."""
    if timeout is not None and timeout <= 0:
        raise GitError(f"git {' '.join(args)}: no time left")
    proc = subprocess.Popen(
        ["git", "-C", str(repo), *args],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=timeout is not None,
    )
    try:
        out, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(proc.pid, signal.SIGKILL)
        with contextlib.suppress(subprocess.TimeoutExpired):
            proc.communicate(timeout=KILL_GRACE)
        raise GitError(f"git {' '.join(args)}: no answer within {timeout:g} s") from None
    return proc.returncode, out, err


def git(repo: str | Path, *args: str, timeout: float | None = None) -> str:
    code, out, err = _run(repo, args, timeout)
    if code != 0:
        raise GitError(f"git {' '.join(args)}: {err.strip() or out.strip()}")
    return out.strip()


def repo_root(path: str | Path) -> Path:
    return Path(git(path, "rev-parse", "--show-toplevel"))


def current_branch(repo: str | Path) -> str:
    # symbolic-ref also answers in a repository that has no commit yet.
    return git(repo, "symbolic-ref", "--short", "HEAD")


def has_commits(repo: str | Path) -> bool:
    proc = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "--verify", "--quiet", "HEAD"], capture_output=True
    )
    return proc.returncode == 0


def ensure_initial_commit(repo: str | Path) -> bool:
    """A worktree needs a commit to branch from; give a brand-new repository an empty one.

    Returns True when the commit was created. Files in the checkout are left untouched
    and uncommitted.
    """
    if has_commits(repo):
        return False
    # Agents start in parallel, each in its own process: only one may create the commit.
    with open(Path(git(repo, "rev-parse", "--absolute-git-dir")) / "orch-init.lock", "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if has_commits(repo):
            return False
        git(repo, "commit", "--allow-empty", "-m", "Initial commit")
    return True


def common_dir(path: str | Path) -> Path:
    """The shared .git directory; a worktree's commits are written there."""
    return Path(git(path, "rev-parse", "--path-format=absolute", "--git-common-dir"))


def branch_exists(repo: str | Path, branch: str, timeout: float | None = None) -> bool:
    args = ("rev-parse", "--verify", "--quiet", f"refs/heads/{branch}")
    return _run(repo, args, timeout)[0] == 0


def add_worktree(repo: str | Path, path: str | Path, branch: str, base_ref: str) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    if branch_exists(repo, branch):
        git(repo, "worktree", "add", str(path), branch)
    else:
        git(repo, "worktree", "add", "-b", branch, str(path), base_ref)


def remove_worktree(repo: str | Path, path: str | Path, branch: str | None = None) -> None:
    if Path(path).exists():
        git(repo, "worktree", "remove", "--force", str(path))
    git(repo, "worktree", "prune")
    if branch and branch_exists(repo, branch):
        git(repo, "branch", "-D", branch)


CHECK_TIMEOUT = 20.0  # seconds for one git command of `unsaved_work`


def unsaved_work(
    repo: str | Path, path: str | Path, branch: str, base: str, deadline: float | None = None
) -> tuple[int, int]:
    """What would be lost without the branch and the worktree at `path`: the number of
    commits not in `base` that are reachable from `branch` or from the worktree's HEAD
    (detached, or on another branch), and of uncommitted changes (untracked files count)
    in the worktree. A branch or worktree that no longer exists has none. Every git call
    ends within CHECK_TIMEOUT and, with `deadline` (time.monotonic()), by then. Raises
    GitError, also when `path` exists but is not that worktree."""

    def left() -> float:
        return (
            CHECK_TIMEOUT if deadline is None else min(CHECK_TIMEOUT, deadline - time.monotonic())
        )

    tips = [f"refs/heads/{branch}"] if branch_exists(repo, branch, timeout=left()) else []
    changes = 0
    where = repo
    if Path(path).exists():
        top = git(path, "rev-parse", "--show-toplevel", timeout=left())
        if Path(top).resolve() != Path(path).resolve():
            raise GitError(f"{path} is not a git worktree")
        # No optional locks: never take the index lock of a worktree another agent uses.
        status = git(path, "--no-optional-locks", "status", "--porcelain", timeout=left())
        changes = len(status.splitlines())
        tips.append("HEAD")
        where = path
    commits = 0
    if tips:
        counted = git(where, "rev-list", "--count", *tips, "--not", base, "--", timeout=left())
        commits = int(counted)
    return commits, changes


def _cap(text: str, limit: int | None) -> str:
    """At most `limit` lines (None or 0: all), then `(+N more)` for the rest."""
    lines = text.splitlines()
    if not limit or len(lines) <= limit:
        return text
    return "\n".join([*lines[:limit], f"(+{len(lines) - limit} more)"])


def summary(
    path: str | Path,
    base_ref: str,
    max_commits: int | None = None,
    max_status: int | None = None,
) -> str:
    """Commits and uncommitted changes of a worktree relative to its base.

    `max_commits`/`max_status` cap the commit and status lines (None or 0: all).
    """
    try:
        commits = _cap(git(path, "log", "--oneline", f"{base_ref}..HEAD"), max_commits)
        dirty = _cap(git(path, "status", "--short"), max_status)
    except GitError as exc:
        return str(exc)
    parts = []
    if commits:
        parts.append(f"commits since {base_ref}:\n{commits}")
    if dirty:
        parts.append(f"uncommitted:\n{dirty}")
    return "\n".join(parts) or "no changes"
