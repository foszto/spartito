"""The code view of an agent's worktree, read-only: the files changed against the base branch
(committed and uncommitted together) with each file's patch, and the worktree's files with
their content.

Only an agent of a registered project that owns a worktree is shown, once git verifies the
worktree to be one of the project's repository. A path is accepted only when it is a plain
relative path that is, byte for byte, both the name the file system has (no case or Unicode
variant) and a name git lists (tracked, or untracked and not ignored; never `.git/`; listed
under forced settings, see git.VIEW_FLAGS, so a name stored decomposed is never served), and a file is opened one
directory at a time without following any symbolic link, after its resolved path is checked
to be inside the resolved worktree. A link is shown as its target; a submodule as its commit,
never entered. Nothing is written or cached; every git call is a read under git.VIEW_FLAGS,
its output capped in bytes.

The changes are counted from the commit the worktree's HEAD shares with the agent's base (any
revision: a branch, a remote-tracking branch, a tag, a commit), or, when that names no commit
any more, with the project's base branch; the answer names the base used.

A failure (unknown agent, no worktree, a path refused, a file gone meanwhile, a git error) is
an OrchestratorError with a short message.
"""

from __future__ import annotations

import functools
import os
import stat
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from .. import git
from ..errors import OrchestratorError
from ..models import Agent, Project

MAX_FILE_BYTES = 512 * 1024  # of a file's content served (and of an untracked file counted)
MAX_PATCH_LINES = 2000  # of one file's patch
MAX_PATCH_BYTES = 1024 * 1024  # of one file's patch read from git, however long its lines
LIST_BYTES = 4 * 1024 * 1024  # of one listing read from git (files, changes)
MAX_CHANGED = 1000  # files in the list of changes
MAX_TREE = 5000  # files in the file tree
BINARY_SNIFF = 8192  # a NUL byte in this many first bytes: a binary file
MAX_PATH_CHARS = 4096


class Facade(Protocol):
    """What these functions use of the facade (they are its methods)."""

    def project(self, name: str) -> Project: ...

    def agent(self, agent_id: str) -> Agent: ...


@dataclass(frozen=True)
class Worktree:
    """An agent's verified worktree: its project, its resolved top level and the
    environment of its git calls."""

    agent: Agent
    project: Project
    root: Path
    env: dict[str, str]


@dataclass(frozen=True)
class Base:
    """What the changes are counted against: `name` (the revision used), `requested` (the
    agent's own base), `merge_base` (the commit HEAD shares with it)."""

    name: str
    requested: str
    merge_base: str

    @property
    def fallback(self) -> bool:
        return self.name != self.requested


def _worktree(orch: Facade, agent_id: str) -> Worktree:
    agent = orch.agent(agent_id)
    if agent.project is None or not agent.owns_worktree:
        raise OrchestratorError(f"{agent.id} has no worktree of its own")
    project = orch.project(agent.project)
    if not Path(agent.cwd).exists():
        raise OrchestratorError(f"the worktree of {agent.id} does not exist (yet)")
    root = git.verified_worktree(project.path, agent.cwd)
    return Worktree(agent, project, root, git.view_env(root))


def _base(tree: Worktree) -> Base:
    """The agent's base, or the project's base branch when the agent's names no commit
    (or none sharing history with HEAD) any more."""
    project = tree.project.base_branch
    requested = tree.agent.base_ref or project
    candidates = [(requested, requested)]
    if project != requested and git.valid_branch(project):
        candidates.append((project, f"refs/heads/{project}"))
    for name, revision in candidates:
        commit = git.resolve_commit(tree.root, revision, tree.env)
        shared = commit and git.merge_base(tree.root, commit, tree.env)
        if shared:
            return Base(name, requested, shared)
    also = f" nor the project's base branch '{project}'" if len(candidates) > 1 else ""
    raise OrchestratorError(
        f"no base to compare with: neither its base '{requested}'{also} names a commit"
        " this branch shares history with"
    )


def _guarded(read: Callable[..., dict[str, Any]]) -> Callable[..., dict[str, Any]]:
    """A git or file system failure as an OrchestratorError: a clean answer to the caller."""

    @functools.wraps(read)
    def guarded(*args: Any) -> dict[str, Any]:
        try:
            return read(*args)
        except (git.GitError, OSError, ValueError) as exc:
            raise OrchestratorError(str(exc) or type(exc).__name__) from None

    return guarded


def checked_path(path: Any) -> str:
    """`path` when it is a plain relative path inside a worktree: a non-empty string without
    NUL, not absolute, no empty, `.` or `..` component, nothing under `.git`."""
    if not isinstance(path, str) or not path:
        raise OrchestratorError("a path is required")
    if "\0" in path or len(path) > MAX_PATH_CHARS:
        raise OrchestratorError("not a usable path")
    if path.startswith("/"):
        raise OrchestratorError("the path must be relative to the worktree")
    parts = path.split("/")
    if any(part in ("", ".", "..") for part in parts):
        raise OrchestratorError("the path must not have empty, '.' or '..' components")
    if any(part.lower() == ".git" for part in parts):
        raise OrchestratorError("files under .git are not shown")
    return path


@dataclass(frozen=True)
class Entry:
    """What `read_entry` found: a file (`data`: at most `limit` bytes of it; `size`: its
    whole size) or a symbolic link (`target`)."""

    kind: str
    size: int = 0
    data: bytes = b""
    target: str | None = None


_DIRS = os.O_RDONLY | os.O_NOFOLLOW | os.O_DIRECTORY | getattr(os, "O_CLOEXEC", 0)


def _entries(root: Path, directory: tuple[str, ...]) -> set[str] | None:
    """The names in the directory `directory` (components) below `root`, entered without
    following links; None when it cannot be read (gone, a link, not a directory)."""
    try:
        fd = os.open(root, _DIRS)
    except OSError:
        return None
    try:
        for part in directory:
            inner = os.open(part, _DIRS, dir_fd=fd)
            os.close(fd)
            fd = inner
        return set(os.listdir(fd))
    except OSError:
        return None
    finally:
        os.close(fd)


def on_disk(root: Path, paths: list[str]) -> list[str]:
    """Those of `paths` (in order) whose every component is, byte for byte, an entry of its
    directory: no case variant and no Unicode normalisation variant of it, which a case- or
    normalisation-insensitive file system would open as well. Each directory is read once."""
    seen: dict[tuple[str, ...], set[str] | None] = {}
    kept = []
    for path in paths:
        parts = path.split("/")
        for index, part in enumerate(parts):
            directory = tuple(parts[:index])
            if directory not in seen:
                seen[directory] = _entries(root, directory)
            names = seen[directory]
            if names is None or part not in names:
                break
        else:
            kept.append(path)
    return kept


def spelled(root: Path, path: str) -> None:
    """Refuse `path` unless each of its components is, byte for byte, the entry of its
    directory (on_disk): git's matching (ignore patterns among it) may treat a variant as
    another name. Raises OrchestratorError."""
    if not on_disk(root, [path]):
        raise OrchestratorError(f"{path} is not a file in this worktree (names are exact)")


def read_entry(root: Path, path: str, limit: int) -> Entry:
    """The file at `path` below the resolved `root`, never through a symbolic link: each
    directory is opened relative to the one before without following links, the last
    component is looked at without following it (a link: its target, unopened), and the
    file's resolved path must lie inside `root`. Raises OSError, OrchestratorError."""
    parts = path.split("/")
    # Resolved, with the last component as it is (a link there is shown, never followed).
    resolved = Path(os.path.realpath(root.joinpath(*parts[:-1]))) / parts[-1]
    if not resolved.is_relative_to(root):
        raise OrchestratorError(f"{path} lies outside the worktree")
    flags = os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
    fd = os.open(root, flags | os.O_DIRECTORY)
    try:
        for part in parts[:-1]:
            inner = os.open(part, flags | os.O_DIRECTORY, dir_fd=fd)
            os.close(fd)
            fd = inner
        info = os.stat(parts[-1], dir_fd=fd, follow_symlinks=False)
        if stat.S_ISLNK(info.st_mode):
            target = os.fsencode(os.readlink(parts[-1], dir_fd=fd))
            return Entry("symlink", target=target.decode("utf-8", "replace"))
        if not stat.S_ISREG(info.st_mode):
            raise OrchestratorError(f"{path} is not a regular file")
        handle = os.open(parts[-1], flags | os.O_NONBLOCK, dir_fd=fd)
    finally:
        os.close(fd)
    with os.fdopen(handle, "rb") as file:
        info = os.fstat(file.fileno())
        if not stat.S_ISREG(info.st_mode):
            raise OrchestratorError(f"{path} is not a regular file")
        return Entry("file", size=info.st_size, data=file.read(limit))


def is_binary(data: bytes) -> bool:
    return b"\0" in data[:BINARY_SNIFF]


def line_count(text: str) -> int:
    """Lines as git counts them: a last line without a newline counts too."""
    return text.count("\n") + (1 if text and not text.endswith("\n") else 0)


def _known(tree: Worktree, path: str, untracked: bool = False) -> bool:
    """Whether git lists `path` itself, spelled as the file system has it."""
    spelled(tree.root, path)
    return path in git.listed_files(tree.root, tree.env, path, untracked, LIST_BYTES)[0]


def _untracked_stat(tree: Worktree, path: str) -> dict[str, Any]:
    """An untracked file as a new file of the list of changes."""
    row: dict[str, Any] = {"path": path, "status": "A", "untracked": True, "added": None}
    row.update(removed=0, binary=False, too_large=False)
    try:
        entry = read_entry(tree.root, path, MAX_FILE_BYTES + 1)
    except OSError, OrchestratorError:
        return {**row, "removed": None}  # gone meanwhile, or not a file
    if entry.kind == "symlink":
        return {**row, "added": 1, "symlink": True}
    if is_binary(entry.data):
        return {**row, "binary": True, "removed": None}
    if entry.size > MAX_FILE_BYTES:
        return {**row, "too_large": True, "removed": None}
    return {**row, "added": line_count(entry.data.decode("utf-8", "replace"))}


@_guarded
def code_changes(orch: Facade, agent_id: str) -> dict[str, Any]:
    """The files the agent's worktree changed against its base (from the commit its HEAD
    shares with it, see _base): committed, staged and unstaged changes of tracked files and
    the untracked files that are not ignored (as new files), sorted by path, at most
    MAX_CHANGED of them, from at most LIST_BYTES of each git listing. Each: path, status (A,
    M, D, T), added and removed lines (null for a binary or too large file), binary,
    too_large, untracked, submodule (a changed commit; its own files are never looked at).
    {agent_id, base, requested_base, base_fallback, merge_base, files, total (null when a
    listing was cut), truncated, max_files}."""
    tree = _worktree(orch, agent_id)
    base = _base(tree)
    changes, cut = git.diff_stat(tree.root, base.merge_base, tree.env, cap=LIST_BYTES)
    rows: dict[str, dict[str, Any]] = {}
    for change in changes:
        row = {"path": change.path, "status": change.status, "added": change.added}
        row.update(removed=change.removed, binary=change.added is None, too_large=False)
        rows[change.path] = {**row, "untracked": False, "submodule": change.submodule}
    others, others_cut = git.listed_files(tree.root, tree.env, untracked=True, cap=LIST_BYTES)
    others = on_disk(tree.root, others)  # what code_patch would serve, no other spelling
    cut = cut or others_cut
    paths = sorted({*rows, *others})
    shown = paths[:MAX_CHANGED]
    files = [rows[p] if p in rows else _untracked_stat(tree, p) for p in shown]
    return {
        "agent_id": tree.agent.id,
        "base": base.name,
        "requested_base": base.requested,
        "base_fallback": base.fallback,
        "merge_base": base.merge_base,
        "files": files,
        "total": None if cut else len(paths),
        "truncated": cut or len(paths) > len(shown),
        "max_files": MAX_CHANGED,
    }


def _cut(patch: str, cut: bool) -> tuple[str, int | None, bool]:
    """The patch (`cut`: git's output was cut at MAX_PATCH_BYTES) at most MAX_PATCH_LINES
    lines long; with its number of lines (None when unknown: cut by bytes) and whether
    it was cut."""
    lines = patch.splitlines(keepends=True)
    kept = "".join(lines[:MAX_PATCH_LINES])
    return kept, None if cut else len(lines), cut or len(lines) > MAX_PATCH_LINES


def _new_file_patch(path: str, entry: Entry) -> str:
    """An untracked file as git would show it added."""
    if entry.kind == "symlink":
        body, mode = f"{entry.target}\n", "120000"
    else:
        body, mode = entry.data.decode("utf-8", "replace"), "100644"
    lines = body.splitlines()
    head = [f"diff --git a/{path} b/{path}", f"new file mode {mode}", "--- /dev/null"]
    head += [f"+++ b/{path}", f"@@ -0,0 +1,{len(lines)} @@"] if lines else []
    tail = [] if not body or body.endswith("\n") else ["\\ No newline at end of file"]
    return "\n".join([*head, *(f"+{line}" for line in lines), *tail]) + "\n"


@_guarded
def code_patch(orch: Facade, agent_id: str, path: Any) -> dict[str, Any]:
    """The patch of one changed file of `code_changes`, at most MAX_PATCH_BYTES read from
    git and cut at MAX_PATCH_LINES lines; a binary or too large file has none, a submodule
    only its commit change. {path, base, patch, binary, too_large, submodule, lines (null
    when cut by bytes), truncated, max_lines, max_bytes}."""
    path = checked_path(path)
    tree = _worktree(orch, agent_id)
    base = _base(tree)
    answer: dict[str, Any] = {"path": path, "base": base.name, "patch": None, "binary": False}
    answer.update(too_large=False, submodule=False, lines=0, truncated=False)
    answer.update(max_lines=MAX_PATCH_LINES, max_bytes=MAX_PATCH_BYTES)
    changes = git.diff_stat(tree.root, base.merge_base, tree.env, path, LIST_BYTES)[0]
    tracked = [change for change in changes if change.path == path]
    if tracked:
        answer["submodule"] = tracked[0].submodule
        if tracked[0].added is None:
            return {**answer, "binary": True}
        output = git.diff_patch(tree.root, base.merge_base, path, tree.env, MAX_PATCH_BYTES)
        text, byte_cut = output.text, output.cut
    elif _known(tree, path, untracked=True):
        entry = read_entry(tree.root, path, MAX_FILE_BYTES + 1)
        if entry.kind == "file" and is_binary(entry.data):
            return {**answer, "binary": True}
        if entry.size > MAX_FILE_BYTES:
            return {**answer, "too_large": True}
        text, byte_cut = _new_file_patch(path, entry), False
    else:
        raise OrchestratorError(f"{path} has no changes against {base.name}")
    patch, lines, cut = _cut(text, byte_cut)
    return {**answer, "patch": patch, "lines": lines, "truncated": cut}


@_guarded
def code_tree(orch: Facade, agent_id: str) -> dict[str, Any]:
    """The files of the agent's worktree that git knows or would track (tracked, and
    untracked but not ignored), sorted, at most MAX_TREE of them, from at most LIST_BYTES
    of each git listing; `submodules`: those of them that are submodules (not browsable).
    {agent_id, files, submodules, total (null when a listing was cut), truncated,
    max_files}."""
    tree = _worktree(orch, agent_id)
    tracked, cut = git.tracked_files(tree.root, tree.env, LIST_BYTES)
    others, others_cut = git.listed_files(tree.root, tree.env, untracked=True, cap=LIST_BYTES)
    cut = cut or others_cut
    # What code_file would serve: listed by git in the bytes the file system has.
    files = on_disk(tree.root, sorted({path for path, _ in tracked} | set(others)))
    shown = files[:MAX_TREE]
    modules = {path for path, submodule in tracked if submodule}
    return {
        "agent_id": tree.agent.id,
        "files": shown,
        "submodules": [path for path in shown if path in modules],
        "total": None if cut else len(files),
        "truncated": cut or len(files) > MAX_TREE,
        "max_files": MAX_TREE,
    }


@_guarded
def code_file(orch: Facade, agent_id: str, path: Any) -> dict[str, Any]:
    """One file of the worktree that git lists: its text cut at MAX_FILE_BYTES bytes (none
    for a binary file), or for a symbolic link its target, never followed.
    {path, kind (file, symlink), target, content, binary, size, truncated, max_bytes}."""
    path = checked_path(path)
    tree = _worktree(orch, agent_id)
    if not _known(tree, path):
        raise OrchestratorError(f"{path} is not a file git lists in this worktree")
    try:
        entry = read_entry(tree.root, path, MAX_FILE_BYTES)
    except FileNotFoundError:
        raise OrchestratorError(f"{path} does not exist in the worktree") from None
    answer: dict[str, Any] = {"path": path, "kind": entry.kind, "target": entry.target}
    answer.update(content=None, binary=False, size=entry.size, truncated=False)
    answer["max_bytes"] = MAX_FILE_BYTES
    if entry.kind == "symlink":
        return answer
    if is_binary(entry.data):
        return {**answer, "binary": True}
    text = entry.data.decode("utf-8", "replace")
    return {**answer, "content": text, "truncated": entry.size > MAX_FILE_BYTES}
