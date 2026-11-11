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


def _run(
    repo: str | Path,
    args: tuple[str, ...],
    timeout: float | None,
    env: dict[str, str] | None = None,
) -> tuple[int, str, str]:
    """Run git; returns (exit code, stdout, stderr). With `timeout` it really returns within
    about timeout + KILL_GRACE: git runs in a process group of its own, which is killed on
    timeout (hooks and helpers too), and a process that still holds the pipes (stuck in
    the kernel, or moved to another session) is left behind. Raises GitError on timeout.
    `env`: git's environment (None: this process's)."""
    if timeout is not None and timeout <= 0:
        raise GitError(f"git {' '.join(args)}: no time left")
    proc = subprocess.Popen(
        ["git", "-C", str(repo), *args],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=timeout is not None,
        env=env,
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


def git(
    repo: str | Path, *args: str, timeout: float | None = None, env: dict[str, str] | None = None
) -> str:
    code, out, err = _run(repo, args, timeout, env)
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


def _changes(path: str | Path, timeout: float, env: dict[str, str] | None = None) -> int:
    """The number of uncommitted changes (untracked files count) in the worktree `path`."""
    # No optional locks: never take the index lock of a worktree another agent uses.
    status = git(path, "--no-optional-locks", "status", "--porcelain", timeout=timeout, env=env)
    return len(status.splitlines())


def _unmerged(
    repo: str | Path, tips: list[str], base: str, timeout: float, env: dict[str, str] | None = None
) -> int:
    """The number of commits reachable from any of `tips` that are not in `base`; 0: nothing
    there is missing from the base (for `unsaved_work` and `branch_state` alike)."""
    args = ("rev-list", "--count", *tips, "--not", base, "--")
    return int(git(repo, *args, timeout=timeout, env=env))


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


# The variables that point git at another repository, worktree, index or object store (git
# rev-parse --local-env-vars): they would override -C, so `branch_state` runs git without them.
REPOSITORY_ENV = (
    "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    "GIT_CONFIG",
    "GIT_CONFIG_PARAMETERS",
    "GIT_CONFIG_COUNT",
    "GIT_OBJECT_DIRECTORY",
    "GIT_DIR",
    "GIT_WORK_TREE",
    "GIT_IMPLICIT_WORK_TREE",
    "GIT_GRAFT_FILE",
    "GIT_INDEX_FILE",
    "GIT_NO_REPLACE_OBJECTS",
    "GIT_REPLACE_REF_BASE",
    "GIT_PREFIX",
    "GIT_SHALLOW_FILE",
    "GIT_COMMON_DIR",
)


def _own_env() -> dict[str, str]:
    """This process's environment without REPOSITORY_ENV (and GIT_CONFIG_KEY_n/VALUE_n)."""
    return {
        name: value
        for name, value in os.environ.items()
        if name not in REPOSITORY_ENV
        and not name.startswith(("GIT_CONFIG_KEY_", "GIT_CONFIG_VALUE_"))
    }


@dataclass(frozen=True)
class BranchState:
    """What `branch_state` found. `tip`: the branch's commit, None when it does not exist
    (or on error); `ahead`/`behind`: commits on the branch not in the base and the other
    way round (when the worktree's HEAD is not the branch's tip, `ahead` counts the
    commits not in the base on the branch or at that HEAD, and `behind` is None);
    `dirty`: uncommitted changes in the worktree, None when no worktree was checked;
    `merged`: see `branch_state`; `error`: why nothing could be told (then every other
    field is None)."""

    tip: str | None = None
    ahead: int | None = None
    behind: int | None = None
    dirty: int | None = None
    merged: bool = False
    error: str | None = None


def branch_state(
    repo: str | Path,
    branch: str,
    base: str,
    worktree: str | Path | None = None,
    ahead_tip: str | None = None,
    deadline: float | None = None,
) -> BranchState:
    """The state of `branch` of the repository at `repo` (a registered project's path)
    against its local branch `base`, with the uncommitted changes of `worktree` (an
    agent's worktree on that branch; checked only when it exists and is verified to be
    a worktree of `repo`: its top level is `worktree` and its common git directory the
    repository's).

    `merged`: `ahead_tip` (the branch's tip when it was last seen ahead of the base) is
    given and is in the base now, nothing on the branch (if it still exists) or at the
    worktree's HEAD is missing from the base (the test of `unsaved_work`), and the
    worktree, if checked, is clean. So a branch never seen ahead, or reset to the base,
    is not merged; nor is one merged by squashing (its commits never reach the base).

    At most 4 git calls, each within CHECK_TIMEOUT and, with `deadline` (time.monotonic()),
    by then, run only in `repo` and in the verified `worktree`, without the environment
    variables that would point git elsewhere (REPOSITORY_ENV). Names that could be read
    as anything but a local branch are refused before any git call. Never raises: a
    failure is returned as `error`."""
    if not valid_branch(branch) or not valid_branch(base):
        return BranchState(error=f"not a usable branch name: {branch!r} (base {base!r})")
    if ahead_tip is not None and not _COMMIT.fullmatch(ahead_tip):
        return BranchState(error=f"not a commit id: {ahead_tip!r}")
    env = _own_env()
    head, base_ref = f"refs/heads/{branch}", f"refs/heads/{base}"
    # 1: the common git directory, the base, whether `ahead_tip` is in the base (`X...B`
    # prints B, X and ^ each merge base of the two; X is in B when X is their only merge
    # base) and the branch's tip. With --revs-only a missing branch prints nothing, and as
    # git reads all that follows a name that is no revision as paths, the branch goes last.
    revs = [base_ref, *([f"{ahead_tip}...{base_ref}"] if ahead_tip else []), head]
    probe = ("rev-parse", "--path-format=absolute", "--git-common-dir", "--revs-only", *revs)
    verify = ("rev-parse", "--show-toplevel", "--path-format=absolute", "--git-common-dir")
    try:
        found = git(repo, *probe, timeout=_left(deadline), env=env).splitlines()
        if len(found) < 2:
            raise GitError(f"there is no branch {base} in {repo}")
        common, rest = Path(found[0]).resolve(), found[2:]
        reached = False
        if ahead_tip:
            bases = [line for line in rest[2:] if line.startswith("^")]
            reached = bases == [f"^{ahead_tip}"]
            rest = rest[2 + len(bases) :]
        tip = rest[0] if rest else None
        dirty = at = None
        if worktree is not None and Path(worktree).exists():
            # 2: is it a worktree of this repository, and where is its HEAD; 3: its changes.
            args = (*verify, "--verify", "HEAD")
            found = git(worktree, *args, timeout=_left(deadline), env=env).splitlines()
            where = [Path(line).resolve() for line in found[:2]]
            if len(found) != 3 or where != [Path(worktree).resolve(), common]:
                raise GitError(f"{worktree} is not a worktree of {repo}")
            dirty, at = _changes(worktree, _left(deadline), env), found[2]
        # 4: the commits not in the base (and, for the branch alone, those it lacks).
        behind = None
        if tip is not None and at in (None, tip):
            both = ("rev-list", "--left-right", "--count", f"{head}...{base_ref}", "--")
            counted = git(repo, *both, timeout=_left(deadline), env=env).split()
            unmerged = int(counted[0])
            behind = int(counted[1])
        else:
            tips = [t for t in (tip and head, at) if t]
            unmerged = _unmerged(repo, tips, base_ref, _left(deadline), env) if tips else 0
    except (GitError, OSError, ValueError, IndexError) as exc:
        return BranchState(error=str(exc) or type(exc).__name__)
    ahead = unmerged if tip is not None else None
    return BranchState(tip, ahead, behind, dirty, reached and unmerged == 0 and not dirty)


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
