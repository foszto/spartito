"""Git plumbing: repository checks and worktree lifecycle."""

from __future__ import annotations

import fcntl
import subprocess
from pathlib import Path


class GitError(RuntimeError):
    pass


def git(repo: str | Path, *args: str, timeout: float | None = None) -> str:
    try:
        proc = subprocess.run(
            ["git", "-C", str(repo), *args], capture_output=True, text=True, timeout=timeout
        )
    except subprocess.TimeoutExpired:
        raise GitError(f"git {' '.join(args)}: no answer within {timeout:g} s") from None
    if proc.returncode != 0:
        raise GitError(f"git {' '.join(args)}: {proc.stderr.strip() or proc.stdout.strip()}")
    return proc.stdout.strip()


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


def branch_exists(repo: str | Path, branch: str) -> bool:
    proc = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}"],
        capture_output=True,
    )
    return proc.returncode == 0


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


def unsaved_work(repo: str | Path, path: str | Path, branch: str, base: str) -> tuple[int, int]:
    """What would be lost without the branch and the worktree at `path`: the number of
    commits on `branch` not in `base`, and of uncommitted changes (untracked files count)
    in the worktree. A branch or worktree that no longer exists has none. Raises GitError,
    also when `path` exists but is not that worktree."""
    commits = 0
    if branch_exists(repo, branch):
        counted = git(
            repo, "rev-list", "--count", f"{base}..refs/heads/{branch}", timeout=CHECK_TIMEOUT
        )
        commits = int(counted)
    changes = 0
    if Path(path).exists():
        top = git(path, "rev-parse", "--show-toplevel", timeout=CHECK_TIMEOUT)
        if Path(top).resolve() != Path(path).resolve():
            raise GitError(f"{path} is not a git worktree")
        changes = len(git(path, "status", "--porcelain", timeout=CHECK_TIMEOUT).splitlines())
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
