"""The code view of an agent's worktree, read-only: the files changed against the base branch
(committed and uncommitted together) with each file's patch, and the worktree's files with
their content.

Only an agent of a registered project that owns a worktree is shown, once git verifies the
worktree to be one of the project's repository. A path is accepted only when it is a plain
relative path that git lists (tracked, or untracked and not ignored; never `.git/`), and a
file is opened one directory at a time without following any symbolic link, after its
resolved path is checked to be inside the resolved worktree. A link is shown as its target.
Nothing is written or cached; every git call is a read under git.VIEW_FLAGS.

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
MAX_PATCH_CHARS = 1024 * 1024  # of one file's patch, however long its lines
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
    """An agent's verified worktree: its resolved top level, the base branch and the
    environment of its git calls."""

    agent: Agent
    root: Path
    base: str
    env: dict[str, str]


def _worktree(orch: Facade, agent_id: str) -> Worktree:
    agent = orch.agent(agent_id)
    if agent.project is None or not agent.owns_worktree:
        raise OrchestratorError(f"{agent.id} has no worktree of its own")
    project = orch.project(agent.project)
    if not Path(agent.cwd).exists():
        raise OrchestratorError(f"the worktree of {agent.id} does not exist (yet)")
    root = git.verified_worktree(project.path, agent.cwd)
    return Worktree(agent, root, agent.base_ref or project.base_branch, git.view_env(root))


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
    return path in git.listed_files(tree.root, tree.env, path, untracked)


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
    """The files the agent's worktree changed against its base branch (from the commit its
    HEAD shares with the base): committed, staged and unstaged changes of tracked files and
    the untracked files that are not ignored (as new files), sorted by path, at most
    MAX_CHANGED of them. Each: path, status (A, M, D, T), added and removed lines (null for
    a binary or too large file), binary, too_large, untracked.
    {agent_id, base, merge_base, files, total, truncated, max_files}."""
    tree = _worktree(orch, agent_id)
    commit = git.merge_base(tree.root, tree.base, tree.env)
    rows: dict[str, dict[str, Any]] = {}
    for path, status, added, removed in git.diff_stat(tree.root, commit, tree.env):
        binary = added is None
        rows[path] = {"path": path, "status": status, "added": added, "removed": removed}
        rows[path].update(binary=binary, too_large=False, untracked=False)
    untracked = [p for p in git.listed_files(tree.root, tree.env, untracked=True) if p not in rows]
    paths = sorted([*rows, *untracked])
    shown = paths[:MAX_CHANGED]
    files = [rows[p] if p in rows else _untracked_stat(tree, p) for p in shown]
    return {
        "agent_id": tree.agent.id,
        "base": tree.base,
        "merge_base": commit,
        "files": files,
        "total": len(paths),
        "truncated": len(paths) > len(shown),
        "max_files": MAX_CHANGED,
    }


def _cut(patch: str) -> tuple[str, int, bool]:
    """The patch cut to MAX_PATCH_LINES lines and MAX_PATCH_CHARS characters; with its
    number of lines and whether it was cut."""
    lines = patch.splitlines(keepends=True)
    kept = "".join(lines[:MAX_PATCH_LINES])
    cut = len(lines) > MAX_PATCH_LINES or len(kept) > MAX_PATCH_CHARS
    return kept[:MAX_PATCH_CHARS], len(lines), cut


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
    """The patch of one changed file of `code_changes`, cut at MAX_PATCH_LINES lines (and
    MAX_PATCH_CHARS characters); a binary or too large file has none.
    {path, patch, binary, too_large, lines, truncated, max_lines}."""
    path = checked_path(path)
    tree = _worktree(orch, agent_id)
    commit = git.merge_base(tree.root, tree.base, tree.env)
    answer: dict[str, Any] = {"path": path, "patch": None, "binary": False, "too_large": False}
    answer.update(lines=0, truncated=False, max_lines=MAX_PATCH_LINES)
    tracked = [row for row in git.diff_stat(tree.root, commit, tree.env, path) if row[0] == path]
    if tracked:
        if tracked[0][2] is None:
            return {**answer, "binary": True}
        text = git.diff_patch(tree.root, commit, path, tree.env)
    elif _known(tree, path, untracked=True):
        entry = read_entry(tree.root, path, MAX_FILE_BYTES + 1)
        if entry.kind == "file" and is_binary(entry.data):
            return {**answer, "binary": True}
        if entry.size > MAX_FILE_BYTES:
            return {**answer, "too_large": True}
        text = _new_file_patch(path, entry)
    else:
        raise OrchestratorError(f"{path} has no changes against {tree.base}")
    patch, lines, cut = _cut(text)
    return {**answer, "patch": patch, "lines": lines, "truncated": cut}


@_guarded
def code_tree(orch: Facade, agent_id: str) -> dict[str, Any]:
    """The files of the agent's worktree that git knows or would track (tracked, and
    untracked but not ignored), sorted, at most MAX_TREE of them.
    {agent_id, files, total, truncated, max_files}."""
    tree = _worktree(orch, agent_id)
    files = git.listed_files(tree.root, tree.env)
    return {
        "agent_id": tree.agent.id,
        "files": files[:MAX_TREE],
        "total": len(files),
        "truncated": len(files) > MAX_TREE,
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
