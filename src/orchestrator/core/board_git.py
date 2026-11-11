"""The live git state of the board items' branches, from a cache (table `board_git`).

Never computed on a request: `sync` runs a pass at most once every SYNC_EVERY seconds
across all processes (a stamp file, as the automatic archive check does), and a pass
stops after SYNC_BUDGET seconds (plus git.KILL_GRACE when a git call had to be killed).
A pass checks the `ready` and `in_progress` items that name a branch, the one checked
longest ago first, with git.branch_state: at most 4 git calls per item, run only in the
item's registered project and in a verified worktree of an agent of that project on the
branch. `states` reads the cache for a set of items.
"""

from __future__ import annotations

import sys
import time
from typing import Any

from .. import board, git
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
            "merged": row["merged_at"] is not None,
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
        project = projects.get(item.project)
        if project is None or not item.branch:
            continue
        if time.monotonic() >= deadline:
            break
        if project.name not in agents:
            agents[project.name] = store.list_agents(project.name)
        row = rows.get(item.id)
        state = git.branch_state(
            project.path,
            item.branch,
            project.base_branch,
            worktree=_worktree(item, agents[project.name]),
            last_tip=row["tip"] if row else None,
            deadline=deadline,
        )
        store.save_board_git(_next_row(item, project, row, state))
        checked.append(item.id)
    return {"checked": checked}


def _worktree(item: board.Item, agents: list[Agent]) -> str | None:
    """The worktree of an agent of the item's project that owns one on the item's branch:
    the item's own agent if it is one of them, else the newest."""
    owners = [agent for agent in agents if agent.owns_worktree and agent.branch == item.branch]
    linked = [agent for agent in owners if agent.id == item.agent_id]
    return (linked or owners)[-1].cwd if owners else None


def _next_row(
    item: board.Item, project: Project, row: dict[str, Any] | None, state: git.BranchState
) -> dict[str, Any]:
    """The cache row after a check. A failed check keeps what was known and records why. A
    branch that is gone keeps its recorded tip. `merged_at` is set when the branch is first
    seen merged after it was seen ahead (so a fresh branch never counts as merged), and
    cleared when it is seen not merged."""
    stamp = now()
    if row is None:
        row = {
            "item_id": item.id,
            "branch": item.branch,
            "base": project.base_branch,
            "tip": None,
            "ahead": None,
            "behind": None,
            "dirty": None,
            "seen_ahead": 0,
            "merged_at": None,
        }
    if state.error:
        return {**row, "error": state.error, "checked_at": stamp}
    merged = bool(state.merged and row["seen_ahead"])
    return {
        **row,
        "tip": state.tip or row["tip"],
        "ahead": state.ahead,
        "behind": state.behind,
        "dirty": state.dirty,
        "seen_ahead": int(bool(row["seen_ahead"] or state.ahead)),
        "merged_at": (row["merged_at"] or stamp) if merged else None,
        "error": None,
        "checked_at": stamp,
    }
