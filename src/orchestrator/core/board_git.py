"""The live git state of the board items' branches, from a cache (table `board_git`).

Never computed on a request: `sync` runs a pass at most once every SYNC_EVERY seconds
across all processes (a stamp file, as the automatic archive check does), and a pass
stops after SYNC_BUDGET seconds (plus git.KILL_GRACE when a git call had to be killed).
A pass checks the `ready` and `in_progress` items that name a branch, the one checked
longest ago first, with git.branch_state: at most 4 git calls per item, run only in the
item's registered project and in a verified worktree of an agent of that project on the
branch. A failure with one item (git, the database) skips only that item. `states` reads
the cache for a set of items, `last_sync` tells when the last pass ran.

Merged (git.branch_state) means: nothing on the branch is missing from the base (or the
branch is gone), the worktree, if any, is clean, and the work at the branch's tip at the
last check that saw it ahead of the base (`ahead_tip`) is in the base, as those commits or
as patch-equivalent ones (git cherry, asked once per tip: so a branch rebased or reworded
and then merged counts). So a fresh branch and one reset to the base (old or newer) are
not merged; a tip whose work is not in the base, or that git no longer has, is forgotten
until the branch is seen ahead again. Not detected: a squash merge of more than one commit,
and commits whose content changed between the last check that saw them and the merge (the
item is moved by hand). Accepted: a base branch that moves
back behind a branch without commits of its own and then forward again shows that branch
ahead and then in the base, which is what a fast-forward merge looks like, so it counts as
merged.

A pass moves an item in `ready` or `in_progress` to `done` (`move_done`) when it finds its
branch merged, once per merged tip: then `done_tip` = `ahead_tip`, and whatever happens to
the item afterwards (moved back by hand, its worktree dirty and clean again), it does not
move again until a new tip was seen ahead and that one is merged. A merge while the item is
in another state is only noted (`done_tip` = `ahead_tip`, nothing moves): the pass also
checks the items in other states whose cached `ahead_tip` is not `done_tip` yet, after the
`ready` and `in_progress` items and only with what is left of its budget. So an item put into `done` by hand before its branch was merged, and
reopened later, stays where it was put.
"""

from __future__ import annotations

import sqlite3
import sys
import time
from collections.abc import Callable
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
AUTO_RULE = "merged"  # who moves an item to done: actor kind "auto", this rule

# Records an automatic move to done, inside its transaction: called as
# record(conn, the item as it was before the move, the merged tip's commit id).
Recorder = Callable[[sqlite3.Connection, board.Item, str], None]


def sync(store: Store, record: Recorder | None = None) -> dict[str, Any]:
    """Run `check` unless a process ran it in the last SYNC_EVERY seconds (or runs it right
    now). Never raises; returns what `check` returns (empty lists when skipped)."""
    if not due(STAMP, SYNC_EVERY):
        return {"checked": [], "done": []}
    return check(store, record)


def check(store: Store, record: Recorder | None = None) -> dict[str, Any]:
    """One pass, now, within SYNC_BUDGET: {"checked": [item ids], "done": [item ids moved
    to done]}; `record` as in `move_done`. Never raises."""
    try:
        return _check(store, time.monotonic() + SYNC_BUDGET, record)
    except Exception as exc:
        print(f"board git check failed: {exc!r}", file=sys.stderr)
        return {"checked": [], "done": []}


def move_done(
    store: Store,
    item_id: int,
    row: dict[str, Any],
    record: Recorder | None = None,
    wait: float = 30,
) -> bool:
    """Store the item's cache `row` and, if the item is still `ready` or `in_progress`, move
    it to the end of `done`, in one transaction. `record`, if given, is called in that
    transaction after the move as record(conn, the item before the move, row["done_tip"])
    to record who moved it (actor kind "auto", rule AUTO_RULE); if it raises, nothing is
    changed. `wait`: seconds to wait for the database's write lock. Returns whether the
    item moved."""
    with store.transaction(wait) as conn:
        store.save_board_git(row, conn)
        found = conn.execute(
            "SELECT * FROM board_items WHERE id = ? AND state IN (?, ?)", (item_id, *CHECKED_STATES)
        ).fetchone()
        if found is None:
            return False
        before = board.Item.from_row(found)
        conn.execute(
            "UPDATE board_items SET state = ?, updated_at = ?, position = (SELECT"
            " COALESCE(MAX(position) + 1, 0) FROM board_items WHERE project = ? AND state = ?)"
            " WHERE id = ? AND state IN (?, ?)",
            (board.DONE, now(), before.project, board.DONE, item_id, *CHECKED_STATES),
        )
        if record is not None:
            record(conn, before, row["done_tip"])
    return True


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


def _check(store: Store, deadline: float, record: Recorder | None) -> dict[str, Any]:
    items = [item for item in board.listing(store) if item.branch]
    projects = {project.name: project for project in store.list_projects()}
    cached = store.board_git([item.id for item in items])
    rows = {
        item.id: row
        for item in items
        if (row := cached.get(item.id)) is not None
        and item.project in projects
        and (row["branch"], row["base"]) == (item.branch, projects[item.project].base_branch)
    }  # a row for another branch or base starts afresh
    items = [
        item
        for item in items
        if item.state in CHECKED_STATES
        or ((row := rows.get(item.id)) and row["ahead_tip"] and row["ahead_tip"] != row["done_tip"])
    ]  # other states: only while a merge may still have to be noted
    agents: dict[str, list[Agent]] = {}
    checked, done = [], []

    def order(item: board.Item) -> tuple[bool, str]:  # live items first, oldest check first
        return item.state not in CHECKED_STATES, rows.get(item.id, {}).get("checked_at", "")

    for item in sorted(items, key=order):
        if item.project not in projects:
            continue
        if time.monotonic() >= deadline:
            break
        try:
            project, row = projects[item.project], rows.get(item.id)
            if _check_item(store, item, project, row, agents, deadline, record):
                done.append(item.id)
            checked.append(item.id)
        except Exception as exc:  # this item only: the next one may well work
            print(f"board git check of #{item.id} failed: {exc!r}", file=sys.stderr)
    return {"checked": checked, "done": done}


def _check_item(
    store: Store,
    item: board.Item,
    project: Project,
    row: dict[str, Any] | None,
    agents: dict[str, list[Agent]],
    deadline: float,
    record: Recorder | None,
) -> bool:
    """Check one item and store its new cache row. A merge not acted on or noted yet sets
    `done_tip`, and moves the item to done if it is `ready` or `in_progress`. Returns
    whether it moved."""
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
        in_base=bool(row and row["ahead_in_base"]),
    )
    new = _next_row(item, project, row, state)
    if new["error"] is None and new["merged"] and new["ahead_tip"] != new["done_tip"]:
        new["done_tip"] = new["ahead_tip"]
        if item.state in CHECKED_STATES:
            return move_done(store, item.id, new, record, max(0.1, deadline - time.monotonic()))
    store.save_board_git(new, wait=max(0.1, deadline - time.monotonic()))
    return False


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
    branch itself was counted ahead of the base (`behind` is known then), and is forgotten
    when its work is found not to be in the base; `done_tip` is left as it is."""
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
            "ahead_in_base": 0,
            "done_tip": None,
            "merged": 0,
        }
    if state.error:
        return {**row, "error": state.error, "checked_at": now()}
    seen_ahead = state.tip is not None and state.behind is not None and bool(state.ahead)
    ahead_tip, in_base = row["ahead_tip"], row["ahead_in_base"]
    if seen_ahead:
        ahead_tip, in_base = state.tip, 0
    elif state.reached is not None:
        ahead_tip, in_base = (ahead_tip, 1) if state.reached else (None, 0)
    return {
        **row,
        "tip": state.tip or row["tip"],
        "ahead": state.ahead,
        "behind": state.behind,
        "dirty": state.dirty,
        "ahead_tip": ahead_tip,
        "ahead_in_base": in_base,
        "merged": int(state.merged),
        "error": None,
        "checked_at": now(),
    }
