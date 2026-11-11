"""Git plumbing: repository checks and worktree lifecycle."""

from __future__ import annotations

import contextlib
import fcntl
import os
import re
import signal
import subprocess
import time
from dataclasses import dataclass
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


CHECK_TIMEOUT = 20.0  # seconds for one git command of `unsaved_work` and `branch_state`


def _left(deadline: float | None) -> float:
    """The timeout of the next git call: CHECK_TIMEOUT, and with `deadline`
    (time.monotonic()) at most the time left until then."""
    return CHECK_TIMEOUT if deadline is None else min(CHECK_TIMEOUT, deadline - time.monotonic())


def _changes(path: str | Path, timeout: float) -> int:
    """The number of uncommitted changes (untracked files count) in the worktree `path`."""
    # No optional locks: never take the index lock of a worktree another agent uses.
    status = git(path, "--no-optional-locks", "status", "--porcelain", timeout=timeout)
    return len(status.splitlines())


def _unmerged(repo: str | Path, tips: list[str], base: str, timeout: float) -> int:
    """The number of commits reachable from any of `tips` that are not in `base`; 0 is
    what "merged" means here (for `unsaved_work` and `branch_state` alike)."""
    return int(git(repo, "rev-list", "--count", *tips, "--not", base, "--", timeout=timeout))


def unsaved_work(
    repo: str | Path, path: str | Path, branch: str, base: str, deadline: float | None = None
) -> tuple[int, int]:
    """What would be lost without the branch and the worktree at `path`: the number of
    commits not in `base` that are reachable from `branch` or from the worktree's HEAD
    (detached, or on another branch), and of uncommitted changes (untracked files count)
    in the worktree. A branch or worktree that no longer exists has none. Every git call
    ends within CHECK_TIMEOUT and, with `deadline` (time.monotonic()), by then. Raises
    GitError, also when `path` exists but is not that worktree."""
    tips = [f"refs/heads/{branch}"] if branch_exists(repo, branch, timeout=_left(deadline)) else []
    changes = 0
    where = repo
    if Path(path).exists():
        top = git(path, "rev-parse", "--show-toplevel", timeout=_left(deadline))
        if Path(top).resolve() != Path(path).resolve():
            raise GitError(f"{path} is not a git worktree")
        changes = _changes(path, _left(deadline))
        tips.append("HEAD")
        where = path
    commits = 0
    if tips:
        commits = _unmerged(where, tips, base, _left(deadline))
    return commits, changes


# A branch name as `branch_state` accepts it: no leading "-", no "..", no "@{", no whitespace
# or control characters and none of ~^:?*[\ (so it can only ever name refs/heads/<name>).
_BAD_BRANCH = re.compile(r"^-|\.\.|@\{|[\s\x00-\x1f\x7f~^:?*\[\\]")
_COMMIT = re.compile(r"[0-9a-f]{40}([0-9a-f]{24})?")


def valid_branch(name: object) -> bool:
    return isinstance(name, str) and bool(name) and not _BAD_BRANCH.search(name)


@dataclass(frozen=True)
class BranchState:
    """What `branch_state` found. `tip`: the branch's commit, None when it does not exist
    (or on error); `ahead`/`behind`: commits on the branch not in the base and the other
    way round (when the worktree's HEAD is not the branch's tip, `ahead` counts the
    commits not in the base on the branch or at that HEAD, and `behind` is None);
    `dirty`: uncommitted changes in the worktree, None when no worktree was checked;
    `merged`: nothing on the branch (or, without it, at `last_tip`) or at the worktree's
    HEAD is missing from the base and the worktree is clean, the test of `unsaved_work`
    (None: unknown, e.g. the branch is gone and no tip was recorded); `error`: why
    nothing could be told (then every other field is None)."""

    tip: str | None = None
    ahead: int | None = None
    behind: int | None = None
    dirty: int | None = None
    merged: bool | None = None
    error: str | None = None


def branch_state(
    repo: str | Path,
    branch: str,
    base: str,
    worktree: str | Path | None = None,
    last_tip: str | None = None,
    deadline: float | None = None,
) -> BranchState:
    """The state of `branch` of the repository at `repo` (a registered project's path)
    against its local branch `base`, with the uncommitted changes of `worktree` (an
    agent's worktree on that branch; checked only when it exists and is verified to be
    a worktree of `repo`: its top level is `worktree` and its common git directory the
    repository's). `last_tip`: the tip recorded earlier, used when the branch is gone.
    At most 4 git calls, each within CHECK_TIMEOUT and, with `deadline`
    (time.monotonic()), by then; git runs only in `repo` and in the verified `worktree`.
    Names that could be read as anything but a local branch are refused before any git
    call. Never raises: a failure is returned as `error`."""
    if not valid_branch(branch) or not valid_branch(base):
        return BranchState(error=f"not a usable branch name: {branch!r} (base {base!r})")
    if last_tip is not None and not _COMMIT.fullmatch(last_tip):
        return BranchState(error=f"not a commit id: {last_tip!r}")
    head, base_ref = f"refs/heads/{branch}", f"refs/heads/{base}"
    probe = ("rev-parse", "--path-format=absolute", "--git-common-dir", "--verify", "--quiet")
    verify = ("rev-parse", "--show-toplevel", "--path-format=absolute", "--git-common-dir")
    try:
        # 1: the repository's common git directory and the branch's tip (exit 1: no branch).
        code, out, err = _run(repo, (*probe, head), _left(deadline))
        found = out.splitlines()
        if code not in (0, 1) or not found:
            raise GitError(f"git rev-parse in {repo}: {err.strip() or out.strip()}")
        common, tip = Path(found[0]).resolve(), found[1] if code == 0 and len(found) > 1 else None
        dirty = at = None
        if worktree is not None and Path(worktree).exists():
            # 2: is it a worktree of this repository, and where is its HEAD; 3: its changes.
            found = git(worktree, *verify, "--verify", "HEAD", timeout=_left(deadline)).splitlines()
            where = [Path(line).resolve() for line in found[:2]]
            if len(found) != 3 or where != [Path(worktree).resolve(), common]:
                raise GitError(f"{worktree} is not a worktree of {repo}")
            dirty, at = _changes(worktree, _left(deadline)), found[2]
        # 4: the commits not in the base (and, for the branch alone, those it lacks).
        behind = None
        if tip is not None and at in (None, tip):
            both = ("rev-list", "--left-right", "--count", f"{head}...{base_ref}", "--")
            counted = git(repo, *both, timeout=_left(deadline)).split()
            unmerged: int | None = int(counted[0])
            behind = int(counted[1])
        else:
            tips = [t for t in (tip and head, last_tip if tip is None else None, at) if t]
            unmerged = _unmerged(repo, tips, base_ref, _left(deadline)) if tips else None
    except (GitError, OSError, ValueError, IndexError) as exc:
        return BranchState(error=str(exc) or type(exc).__name__)
    ahead = unmerged if tip is not None else None
    merged = None if unmerged is None else unmerged == 0 and not dirty
    return BranchState(tip, ahead, behind, dirty, merged)


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
