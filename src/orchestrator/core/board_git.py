"""The live git state of the board items' branches, from a cache (table `board_git`).

Never computed on a request: `sync` runs a pass at most once every SYNC_EVERY seconds
across all processes (a stamp file, as the automatic archive check does), and a pass
stops after SYNC_BUDGET seconds (plus git.KILL_GRACE when a git call had to be killed).
A pass checks the `ready` and `in_progress` items that name a branch, the one checked
longest ago first, with git.branch_state: at most 4 git calls per item, run only in the
item's registered project and in a verified worktree of an agent of that project on the
branch. A failure with one item (git, the database) skips only that item. `states` reads
the cache for a set of items, `last_sync` tells when the last pass ran.

Merged (git.branch_state) means: the branch's tip at the last check that saw it ahead of
the base (`ahead_tip`) is in the base now, nothing on the branch is missing from the base
(or the branch is gone), and the worktree, if any, is clean. So a fresh branch, one reset
to the base and one merged by squashing are not merged. Accepted: a base branch that moves
back behind a branch without commits of its own and then forward again shows that branch
ahead and then in the base, which is what a fast-forward merge looks like, so it counts as
merged.
"""

from __future__ import annotations

import sys
import time
from datetime import UTC, datetime
from typing import Any

from .. import board, config, git
from ..models import Agent, Project
from ..store import Store, now
from .archive import due

SYNC_EVERY = 60.0  # seconds between two passes, across processes
SYNC_BUDGET = 2.0  # seconds a pass may spend, git included
STAMP = "board-git.stamp"
CHECKED_STATES = (board.READY, board.IN_PROGRESS)


def sync(store: Store) -> dict[str, Any]:
    """Run `check` unless a process ran it in the last SYNC_EVERY seconds (or runs it right
    now). Never raises; returns what `check` returns ({"checked": []} when skipped)."""
    if not due(STAMP, SYNC_EVERY):
        return {"checked": []}
    return check(store)


def check(store: Store) -> dict[str, Any]:
    """One pass, now, within SYNC_BUDGET: {"checked": [item ids]}. Never raises."""
    try:
        return _check(store, time.monotonic() + SYNC_BUDGET)
    except Exception as exc:
        print(f"board git check failed: {exc!r}", file=sys.stderr)
        return {"checked": []}


def last_sync() -> str | None:
    """When the last `sync` pass started (its stamp), as ISO 8601 UTC seconds; None: none
    yet (or the stamp is unreadable). Never raises."""
    try:
        stamped = float((config.home() / STAMP).read_text().strip())
        return datetime.fromtimestamp(stamped, UTC).isoformat(timespec="seconds")
    except OSError, ValueError, OverflowError:
        return None


def states(store: Store, items: list[board.Item]) -> dict[int, dict[str, Any]]:
    """The cached git state of the items, by id: {branch, ahead, behind, dirty, merged,
    checked_at, error}. Left out: an item without a branch, one not checked yet, and one
    whose cached state is for another branch than its current one."""
    rows = store.board_git([item.id for item in items if item.branch])
    return {
        item.id: {
            "branch": row["branch"],
            "ahead": row["ahead"],
            "behind": row["behind"],
            "dirty": row["dirty"],
            "merged": bool(row["merged"]),
            "checked_at": row["checked_at"],
            "error": row["error"],
        }
        for item in items
        if (row := rows.get(item.id)) is not None and row["branch"] == item.branch
    }


def _check(store: Store, deadline: float) -> dict[str, Any]:
    items = [item for state in CHECKED_STATES for item in board.listing(store, state=state)]
    items = [item for item in items if item.branch]
    projects = {project.name: project for project in store.list_projects()}
    cached = store.board_git([item.id for item in items])
    rows = {
        item.id: row
        for item in items
        if (row := cached.get(item.id)) is not None
        and item.project in projects
        and (row["branch"], row["base"]) == (item.branch, projects[item.project].base_branch)
    }  # a row for another branch or base starts afresh
    agents: dict[str, list[Agent]] = {}
    checked = []
    for item in sorted(items, key=lambda item: rows.get(item.id, {}).get("checked_at", "")):
        if item.project not in projects:
            continue
        if time.monotonic() >= deadline:
            break
        try:
            _check_item(store, item, projects[item.project], rows.get(item.id), agents, deadline)
            checked.append(item.id)
        except Exception as exc:  # this item only: the next one may well work
            print(f"board git check of #{item.id} failed: {exc!r}", file=sys.stderr)
    return {"checked": checked}


def _check_item(
    store: Store,
    item: board.Item,
    project: Project,
    row: dict[str, Any] | None,
    agents: dict[str, list[Agent]],
    deadline: float,
) -> dict[str, Any]:
    """Check one item and store its new cache row (also returned)."""
    assert item.branch
    if project.name not in agents:
        agents[project.name] = store.list_agents(project.name)
    state = git.branch_state(
        project.path,
        item.branch,
        project.base_branch,
        worktree=_worktree(item, agents[project.name]),
        ahead_tip=row["ahead_tip"] if row else None,
        deadline=deadline,
    )
    new = _next_row(item, project, row, state)
    store.save_board_git(new, wait=max(0.1, deadline - time.monotonic()))
    return new


def _worktree(item: board.Item, agents: list[Agent]) -> str | None:
    """The worktree of an agent of the item's project that owns one on the item's branch:
    the item's own agent if it is one of them, else the newest."""
    owners = [agent for agent in agents if agent.owns_worktree and agent.branch == item.branch]
    linked = [agent for agent in owners if agent.id == item.agent_id]
    return (linked or owners)[-1].cwd if owners else None


def _next_row(
    item: board.Item, project: Project, row: dict[str, Any] | None, state: git.BranchState
) -> dict[str, Any]:
    """The cache row after a check. A failed check keeps what was known and records why.
    A branch that is gone keeps its recorded tip. `ahead_tip` becomes the tip when the
    branch itself was counted ahead of the base (`behind` is known then); `done_tip` is
    left as it is."""
    if row is None:
        row = {
            "item_id": item.id,
            "branch": item.branch,
            "base": project.base_branch,
            "tip": None,
            "ahead": None,
            "behind": None,
            "dirty": None,
            "ahead_tip": None,
            "done_tip": None,
            "merged": 0,
        }
    if state.error:
        return {**row, "error": state.error, "checked_at": now()}
    seen_ahead = state.tip is not None and state.behind is not None and bool(state.ahead)
    return {
        **row,
        "tip": state.tip or row["tip"],
        "ahead": state.ahead,
        "behind": state.behind,
        "dirty": state.dirty,
        "ahead_tip": state.tip if seen_ahead else row["ahead_tip"],
        "merged": int(state.merged),
        "error": None,
        "checked_at": now(),
    }
