"""Git plumbing: repository checks and worktree lifecycle."""

from __future__ import annotations

import contextlib
import fcntl
import os
import re
import selectors
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
    lenient: bool = False,
) -> tuple[int, str, str]:
    """Run git; returns (exit code, stdout, stderr). With `timeout` it really returns within
    about timeout + KILL_GRACE: git runs in a process group of its own, which is killed on
    timeout (hooks and helpers too), and a process that still holds the pipes (stuck in
    the kernel, or moved to another session) is left behind. Raises GitError on timeout.
    `env`: git's environment (None: this process's). `lenient`: the output read as UTF-8
    with invalid bytes replaced (else in the locale's encoding, strictly)."""
    if timeout is not None and timeout <= 0:
        raise GitError(f"git {' '.join(args)}: no time left")
    proc = subprocess.Popen(
        ["git", "-C", str(repo), *args],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8" if lenient else None,
        errors="replace" if lenient else None,
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
    `merged`: see `branch_state`; `reached`: whether the work at `ahead_tip` is in the
    base (True), is not or git no longer has that commit (False: forget it), or was not
    asked (None); `error`: why nothing could be told (then every other field is None)."""

    tip: str | None = None
    ahead: int | None = None
    behind: int | None = None
    dirty: int | None = None
    merged: bool = False
    reached: bool | None = None
    error: str | None = None


def _reached(
    repo: str | Path, commit: str, base_ref: str, deadline: float | None, env: dict[str, str]
) -> bool:
    """Whether every commit of `commit` that is not in the base has a patch-equivalent one
    there (`git cherry`: no "+" line); an ancestor of the base has none to look for. False
    as well when git no longer has `commit` (asked only after cherry failed). Raises
    GitError when that cannot be told (a timeout, another failure)."""
    code, out, err = _run(repo, ("cherry", base_ref, commit), _left(deadline), env)
    if code == 0:
        return not any(line.startswith("+") for line in out.splitlines())
    known = ("rev-parse", "--verify", "--quiet", f"{commit}^{{commit}}")
    if _run(repo, known, _left(deadline), env)[0] == 1:
        return False
    raise GitError(f"git cherry {base_ref} {commit}: {err.strip() or out.strip()}")


def branch_state(
    repo: str | Path,
    branch: str,
    base: str,
    worktree: str | Path | None = None,
    ahead_tip: str | None = None,
    deadline: float | None = None,
    in_base: bool = False,
) -> BranchState:
    """The state of `branch` of the repository at `repo` (a registered project's path)
    against its local branch `base`, with the uncommitted changes of `worktree` (an
    agent's worktree on that branch; checked only when it exists and is verified to be
    a worktree of `repo`: its top level is `worktree` and its common git directory the
    repository's).

    `merged`: nothing on the branch (if it still exists) or at the worktree's HEAD is
    missing from the base (the test of `unsaved_work`), the worktree, if checked, is
    clean, and the work at `ahead_tip` (the branch's tip when it was last seen ahead of
    the base) is in the base: `in_base` (known from an earlier check) or, asked only then,
    every commit of it is in the base or has a patch-equivalent one there (git cherry: so
    a rebased or reworded branch merged counts, one reset to the base does not). The
    answer is `reached`; a tip git no longer has is reported as not reached.

    At most 5 git calls (a 6th only when the cherry call fails, to tell a tip git no
    longer has from an error), each within CHECK_TIMEOUT and, with `deadline`
    (time.monotonic()), by then, run only in `repo` and in the verified `worktree`,
    without the environment variables that would point git elsewhere (REPOSITORY_ENV).
    Names that could be read as anything but a local branch are refused before any git
    call. Never raises: a failure is returned as `error`."""
    if not valid_branch(branch) or not valid_branch(base):
        return BranchState(error=f"not a usable branch name: {branch!r} (base {base!r})")
    if ahead_tip is not None and not _COMMIT.fullmatch(ahead_tip):
        return BranchState(error=f"not a commit id: {ahead_tip!r}")
    env = _own_env()
    head, base_ref = f"refs/heads/{branch}", f"refs/heads/{base}"
    # 1: the common git directory, the base and the branch's tip. With --revs-only a missing
    # branch prints nothing (and a missing base nothing from there on).
    probe = ("rev-parse", "--path-format=absolute", "--git-common-dir", "--revs-only")
    verify = ("rev-parse", "--show-toplevel", "--path-format=absolute", "--git-common-dir")
    try:
        found = git(repo, *probe, base_ref, head, timeout=_left(deadline), env=env).splitlines()
        if len(found) < 2:
            raise GitError(f"there is no branch {base} in {repo}")
        common, tip = Path(found[0]).resolve(), found[2] if len(found) > 2 else None
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
        # 5 (6): did the work at `ahead_tip` reach the base; asked once per tip, and only
        # when the rest says merged.
        reached = True if ahead_tip and in_base else None
        if ahead_tip and not in_base and unmerged == 0 and not dirty:
            reached = _reached(repo, ahead_tip, base_ref, deadline, env)
    except (GitError, OSError, ValueError, IndexError) as exc:
        return BranchState(error=str(exc) or type(exc).__name__)
    ahead = unmerged if tip is not None else None
    merged = bool(reached) and unmerged == 0 and not dirty
    return BranchState(tip, ahead, behind, dirty, merged, reached)


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


# -- reading a worktree for the dashboard's code view -----------------------------------
#
# An agent can write its repository's configuration and attributes, so these calls must not
# run anything that configuration names: no external diff or textconv driver, no fsmonitor
# hook, no clean filter (every configured filter driver is blanked, see `view_env`), no lazy
# fetch of a partial clone, and no git started inside a submodule (whose own configuration
# would apply): diffs show a submodule as its commit only and never ask whether its work
# tree is dirty, whatever `diff.submodule`, `diff.ignoreSubmodules` or `submodule.*.ignore`
# say. No pager, no optional locks (nothing written to the index), paths taken literally.
# Names are listed the same way whatever the repository's own settings: composed (NFC) on
# macOS, where git converts what the file system gives it (core.precomposeUnicode), so that a
# name stored decomposed on disk is never listed in the bytes it has there; and ignore
# patterns match in any letter case (core.ignoreCase), the stricter reading.
# Every call is read as a stream and stops at a byte cap (`_run_capped`) and a timeout, and
# runs without REPOSITORY_ENV.

VIEW_FLAGS = (
    "--no-pager",
    "--no-optional-locks",
    "--literal-pathspecs",
    "-c",
    "core.fsmonitor=false",
    "-c",
    "core.quotePath=false",
    "-c",
    "core.precomposeUnicode=true",
    "-c",
    "core.ignoreCase=true",
    "-c",
    "diff.submodule=short",
    "-c",
    "submodule.recurse=false",
)
DIFF_FLAGS = (
    "--no-ext-diff",
    "--no-textconv",
    "--no-renames",
    "--no-color",
    "--no-relative",
    "--submodule=short",
    "--ignore-submodules=dirty",
)
VIEW_CAP = 4 * 1024 * 1024  # bytes of one git call's output, unless the caller sets another
READ_CHUNK = 64 * 1024
ERROR_CAP = 64 * 1024  # bytes of git's error output kept
SUBMODULE = "160000"  # the mode of a submodule's entry (a commit, not a file)


@dataclass(frozen=True)
class Output:
    """What a capped git call printed: `text` (UTF-8, invalid bytes replaced) of the `read`
    bytes taken, at most the cap; `cut`: the cap was reached and git was stopped."""

    text: str
    cut: bool
    read: int


def _stop(proc: subprocess.Popen) -> None:
    """Kill git's process group (helpers too) and give it KILL_GRACE to go."""
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(proc.pid, signal.SIGKILL)
    with contextlib.suppress(subprocess.TimeoutExpired):
        proc.wait(timeout=KILL_GRACE)


def _run_capped(
    repo: str | Path, args: tuple[str, ...], timeout: float, env: dict[str, str], cap: int
) -> tuple[int | None, bytes, str, bool]:
    """Run git, reading its output as it comes and never more than `cap` bytes of it:
    (exit code, None when stopped; stdout; stderr; cut). Reaching the cap counts as cut:
    git is stopped (its process group killed). Raises GitError when git does not end
    within `timeout` (it is stopped then too)."""
    proc = subprocess.Popen(
        ["git", "-C", str(repo), *args],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
        env=env,
    )
    assert proc.stdout is not None and proc.stderr is not None
    out, err, cut = bytearray(), bytearray(), False
    deadline = time.monotonic() + timeout
    try:
        with selectors.DefaultSelector() as pipes:
            pipes.register(proc.stdout, selectors.EVENT_READ)
            pipes.register(proc.stderr, selectors.EVENT_READ)
            while pipes.get_map() and not cut:
                left = deadline - time.monotonic()
                if left <= 0:
                    _stop(proc)
                    raise GitError(f"git {args[-1]}: no answer within {timeout:g} s")
                for key, _ in pipes.select(left):
                    if key.fileobj is proc.stdout:
                        chunk = os.read(key.fd, min(READ_CHUNK, cap - len(out)))
                        out += chunk
                        cut = len(out) >= cap
                    else:
                        chunk = os.read(key.fd, READ_CHUNK)
                        err += chunk[: max(0, ERROR_CAP - len(err))]
                    if not chunk:
                        pipes.unregister(key.fileobj)
                    if cut:
                        break
        if cut:
            _stop(proc)
            return None, bytes(out), err.decode("utf-8", "replace"), True
        try:
            code = proc.wait(timeout=max(0.0, deadline - time.monotonic()) + KILL_GRACE)
        except subprocess.TimeoutExpired:
            _stop(proc)
            raise GitError(f"git {args[-1]}: no answer within {timeout:g} s") from None
        return code, bytes(out), err.decode("utf-8", "replace"), False
    finally:
        proc.stdout.close()
        proc.stderr.close()


def _view(worktree: str | Path, env: dict[str, str], *args: str, cap: int = VIEW_CAP) -> Output:
    """git with VIEW_FLAGS in `worktree`, at most `cap` bytes of its output."""
    code, out, err, cut = _run_capped(worktree, (*VIEW_FLAGS, *args), CHECK_TIMEOUT, env, cap)
    if not cut and code != 0:
        raise GitError(f"git {args[0]}: {err.strip() or 'failed'}")
    return Output(out.decode("utf-8", "replace"), cut, len(out))


def view_env(worktree: str | Path) -> dict[str, str]:
    """The environment of the code view's git calls: this process's without REPOSITORY_ENV,
    no lazy fetch, and every filter driver the configuration names blanked (no clean
    command or process, not required), so that reading a worktree file runs nothing."""
    env = _own_env()
    env["GIT_NO_LAZY_FETCH"] = "1"
    code, out, _ = _run(
        worktree,
        ("--no-pager", "config", "--null", "--name-only", "--get-regexp", r"^filter\."),
        CHECK_TIMEOUT,
        env,
        lenient=True,
    )
    if code not in (0, 1):  # 1: none configured
        raise GitError("git config: the filter drivers could not be read")
    drivers = {name[len("filter.") :].rpartition(".")[0] for name in out.split("\0") if name}
    blank = [
        (f"filter.{driver}.{key}", value)
        for driver in sorted(drivers)
        for key, value in (("clean", ""), ("process", ""), ("required", "false"))
    ]
    env["GIT_CONFIG_COUNT"] = str(len(blank))
    for index, (key, value) in enumerate(blank):
        env[f"GIT_CONFIG_KEY_{index}"] = key
        env[f"GIT_CONFIG_VALUE_{index}"] = value
    return env


def verified_worktree(repo: str | Path, worktree: str | Path) -> Path:
    """The resolved top level of `worktree` once it is verified to be a worktree of the
    repository at `repo`: its top level is `worktree` itself and its common git directory
    the repository's. Raises GitError (also when it does not exist)."""
    if not Path(worktree).is_dir():
        raise GitError(f"{worktree} does not exist")
    env = _own_env()
    args = ("rev-parse", "--path-format=absolute", "--show-toplevel", "--git-common-dir")
    found = git(worktree, *args, timeout=CHECK_TIMEOUT, env=env).splitlines()
    common = git(repo, *args[:2], args[3], timeout=CHECK_TIMEOUT, env=env)
    top = Path(worktree).resolve()
    if len(found) != 2 or [Path(p).resolve() for p in found] != [top, Path(common).resolve()]:
        raise GitError(f"{worktree} is not a worktree of {repo}")
    return top


def resolve_commit(worktree: str | Path, revision: str, env: dict[str, str]) -> str | None:
    """The commit `revision` names in the worktree's repository (a branch, a remote-tracking
    branch, a tag, a commit id, …), None when it names none. Never read as an option."""
    if not revision or revision.startswith("-") or any(c in revision for c in "\0\n\r"):
        return None
    args = ("rev-parse", "--verify", "--quiet", "--end-of-options", f"{revision}^{{commit}}")
    code, out, _ = _run(worktree, (*VIEW_FLAGS, *args), CHECK_TIMEOUT, env, lenient=True)
    found = out.strip()
    return found if code == 0 and _COMMIT.fullmatch(found) else None


def merge_base(worktree: str | Path, commit: str, env: dict[str, str]) -> str | None:
    """The commit the worktree's HEAD shares with `commit` (an id), None when none."""
    if not _COMMIT.fullmatch(commit):
        raise GitError(f"not a commit id: {commit!r}")
    args = ("merge-base", commit, "HEAD")
    code, out, err = _run(worktree, (*VIEW_FLAGS, *args), CHECK_TIMEOUT, env, lenient=True)
    if code == 1:
        return None
    if code != 0 or not _COMMIT.fullmatch(out.strip()):
        raise GitError(f"git merge-base: {err.strip() or out.strip()}")
    return out.strip()


def _entries(output: Output) -> list[str]:
    """The entries of a -z listing; when it was cut, the last (perhaps partial) one is left
    out."""
    entries = output.text.split("\0")
    return entries[:-1] if output.cut else entries


def _paths(output: Output) -> list[str]:
    """The paths of a -z listing; a nested repository's `dir/` is left out."""
    return [path for path in _entries(output) if path and not path.endswith("/")]


def listed_files(
    worktree: str | Path,
    env: dict[str, str],
    path: str | None = None,
    untracked: bool = False,
    cap: int = VIEW_CAP,
) -> tuple[list[str], bool]:
    """The files git knows or would track (`untracked`: only the untracked, not ignored
    ones), sorted, each once; with `path` only that one (or those below it). With whether
    the listing was cut at `cap` bytes."""
    kinds = ("-o",) if untracked else ("-c", "-o")
    args = ("ls-files", *kinds, "--exclude-standard", "-z", "--", *([path] if path else []))
    output = _view(worktree, env, *args, cap=cap)
    return sorted(set(_paths(output))), output.cut


def tracked_files(
    worktree: str | Path, env: dict[str, str], cap: int = VIEW_CAP
) -> tuple[list[tuple[str, bool]], bool]:
    """The files in the index: (path, whether it is a submodule); with whether the listing
    was cut at `cap` bytes."""
    output = _view(worktree, env, "ls-files", "-c", "-s", "-z", cap=cap)
    found = []
    for entry in _entries(output):
        info, tab, name = entry.partition("\t")
        if tab and name:
            found.append((name, info.split(" ")[0] == SUBMODULE))
    return found, output.cut


@dataclass(frozen=True)
class Change:
    """One tracked file that differs: its status letter (A, M, D, T), added and removed
    lines (None for a binary file), whether it is a submodule (its commit only)."""

    path: str
    status: str
    added: int | None
    removed: int | None
    submodule: bool


def diff_stat(
    worktree: str | Path,
    commit: str,
    env: dict[str, str],
    path: str | None = None,
    cap: int = VIEW_CAP,
) -> tuple[list[Change], bool]:
    """The tracked files that differ between `commit` and the worktree (committed, staged
    and unstaged changes together); with `path` only that one. With whether git's output
    was cut at `cap` bytes."""
    if not _COMMIT.fullmatch(commit):
        raise GitError(f"not a commit id: {commit!r}")
    spec = ("--", *([path] if path else []))
    raw = _view(worktree, env, "diff", *DIFF_FLAGS, "--raw", "-z", commit, *spec, cap=cap)
    fields = _entries(raw)
    kinds: dict[str, tuple[str, bool]] = {}
    for i in range(0, len(fields) - 1, 2):
        info = fields[i].lstrip(":").split(" ")
        if len(info) >= 5:
            kinds[fields[i + 1]] = (info[4][:1], SUBMODULE in info[:2])
    counts = _view(worktree, env, "diff", *DIFF_FLAGS, "--numstat", "-z", commit, *spec, cap=cap)
    found = []
    for entry in _entries(counts):
        added, _, rest = entry.partition("\t")
        removed, _, name = rest.partition("\t")
        if not name:
            continue
        status, submodule = kinds.get(name, ("M", False))
        binary = added == "-"
        added_n = None if binary else int(added)
        removed_n = None if binary else int(removed)
        found.append(Change(name, status, added_n, removed_n, submodule))
    return found, raw.cut or counts.cut


def diff_patch(
    worktree: str | Path, commit: str, path: str, env: dict[str, str], cap: int = VIEW_CAP
) -> Output:
    """The patch of one tracked file between `commit` and the worktree, at most `cap`
    bytes of it (a submodule: its commit change only)."""
    if not _COMMIT.fullmatch(commit):
        raise GitError(f"not a commit id: {commit!r}")
    return _view(worktree, env, "diff", *DIFF_FLAGS, "--patch", commit, "--", path, cap=cap)
