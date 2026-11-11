"""A small task board per project: ideas and collected tasks that outlive a conversation.

Table `board_items`. Every item belongs to a registered project and is in one state:
`idea` -> `ready` -> `in_progress` -> `done`; `position` orders the items within one state of
one project (a new item, and one that changes state, goes to the end). An item may name an
agent and a branch that work on it. The orchestrator and the leads use the MCP tools
`list_board`, `add_board_item` and `update_board_item`; the owner the dashboard and
`orch board`. Nothing is dispatched automatically: `dispatch` (the owner's button or
`orch board dispatch`) sends a project's `ready` items to its lead as one user message,
marks them `in_progress` and links them to the lead.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from dataclasses import asdict, dataclass, replace
from typing import Any

from .store import Store, now

IDEA, READY, IN_PROGRESS, DONE = "idea", "ready", "in_progress", "done"
STATES = (IDEA, READY, IN_PROGRESS, DONE)
TITLE_CHARS = 200
DESCRIPTION_CHARS = 4000
BRANCH_CHARS = 200
FIELDS = ("title", "description", "state", "agent_id", "branch")
OWNER_KIND, AGENT_KIND, AUTO_KIND = "owner", "agent", "auto"


@dataclass(frozen=True)
class Actor:
    """Who changes the board: the owner (dashboard, CLI), an agent through the MCP server
    (`name`: its id; None for the interactive `orch chat` session) or an automatic rule
    (`name`: the rule)."""

    kind: str = OWNER_KIND
    name: str | None = None


OWNER = Actor()


@dataclass
class Item:
    id: int
    project: str
    title: str
    description: str = ""
    state: str = IDEA
    position: int = 0
    agent_id: str | None = None
    branch: str | None = None
    created_at: str = ""
    updated_at: str = ""

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> Item:
        names = cls.__dataclass_fields__
        return cls(**{key: value for key, value in dict(row).items() if key in names})

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _text(value: Any, what: str, limit: int, required: bool = False) -> str:
    if value is None:
        value = ""
    if not isinstance(value, str):
        raise ValueError(f"{what} must be a string")
    value = value.strip()
    if required and not value:
        raise ValueError(f"{what} must not be empty")
    if len(value) > limit:
        raise ValueError(f"{what} is too long ({len(value)} chars; at most {limit})")
    return value


def check_state(state: Any) -> str:
    if state not in STATES:
        raise ValueError(f"unknown state {state!r} (one of: {', '.join(STATES)})")
    return state


def record_gap(conn: sqlite3.Connection, item_id: int, since: str) -> None:
    """Called just before `record` for a change of an existing item, in its transaction:
    `since` is the item's updated_at before this change. Later than the item's last entry
    (or it has none), the item was changed by code that keeps no history (an older process
    on the same database): an entry `unrecorded` without actor, at `since`, keeps that gap
    for good. A change by such code within the same second as the last entry cannot be told
    apart (the times are in seconds) and stays undetected."""
    last = conn.execute(
        "SELECT ts FROM board_history WHERE item_id = ? ORDER BY id DESC LIMIT 1", (item_id,)
    ).fetchone()
    if last is None or since > last["ts"]:
        conn.execute(
            "INSERT INTO board_history (item_id, ts, actor_kind, actor, action, changes)"
            " VALUES (?, ?, NULL, NULL, 'unrecorded', '{}')",
            (item_id, since),
        )


def link_run(conn: sqlite3.Connection, item_ids: list[int], stamp: str, run_id: int) -> None:
    """The run of the dispatch recorded at `stamp` for these items, known once its message
    was sent."""
    marks = ", ".join("?" for _ in item_ids)
    conn.execute(
        "UPDATE board_history SET run_id = ? WHERE action = 'dispatch' AND ts = ?"
        f" AND run_id IS NULL AND item_id IN ({marks})",
        (run_id, stamp, *item_ids),
    )


def _diff(before: Item | None, after: Item, fields: tuple[str, ...]) -> dict[str, Any]:
    """The fields that differ as {field: [old, new]}; the description only as "changed"."""
    out: dict[str, Any] = {}
    for key in fields:
        old, new = getattr(before, key) if before else None, getattr(after, key)
        if old != new:
            out[key] = "changed" if key == "description" else [old, new]
    return out


def _run_of(store: Store, actor: Actor) -> int | None:
    """The acting agent's running turn (read before the change's transaction)."""
    if actor.kind != AGENT_KIND or not actor.name:
        return None
    run = store.running_run(actor.name)
    return run.id if run else None


def record(
    conn: sqlite3.Connection,
    item_id: int,
    actor: Actor,
    action: str,
    changes: dict[str, Any],
    stamp: str,
    run_id: int | None = None,
) -> None:
    """Every change of an item comes here, in the transaction that makes it, and is kept in
    board_history: who made it, the action (add, edit, state, move, place, dispatch), what
    changed ({field: [old, new]}, see `_diff`), `stamp` (the item's new updated_at, see
    `unrecorded`) and the run (a dispatch's, or the acting agent's turn)."""
    conn.execute(
        "INSERT INTO board_history (item_id, ts, actor_kind, actor, action, changes, run_id)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
        (item_id, stamp, actor.kind, actor.name, action, json.dumps(changes), run_id),
    )


def history(store: Store, item_id: int) -> list[dict[str, Any]]:
    """The item's recorded changes, oldest first: {ts, actor_kind, actor, action, changes,
    run_id}."""
    with store.connection() as conn:
        rows = conn.execute(
            "SELECT ts, actor_kind, actor, action, changes, run_id FROM board_history"
            " WHERE item_id = ? ORDER BY id",
            (item_id,),
        ).fetchall()
    return [{**dict(row), "changes": json.loads(row["changes"])} for row in rows]


def unrecorded(item: Item, entries: list[dict[str, Any]]) -> bool:
    """Whether the item changed after its last recorded change (or has none): code from
    before the history (a lead's MCP server still running it) changed it. Such a gap
    before a later recorded change is an `unrecorded` entry (see `record_gap`)."""
    return not entries or item.updated_at > entries[-1]["ts"]


def _end(conn: sqlite3.Connection, project: str, state: str) -> int:
    """The position after the last item in this state of the project."""
    row = conn.execute(
        "SELECT MAX(position) FROM board_items WHERE project = ? AND state = ?", (project, state)
    ).fetchone()
    return 0 if row[0] is None else row[0] + 1


SQLITE_IDS = range(-(2**63), 2**63)  # an id outside (OverflowError in sqlite3) is unknown


def _get(conn: sqlite3.Connection, item_id: int) -> Item | None:
    if item_id not in SQLITE_IDS:
        return None
    row = conn.execute("SELECT * FROM board_items WHERE id = ?", (item_id,)).fetchone()
    return Item.from_row(row) if row else None


def get(store: Store, item_id: int) -> Item:
    """Raises ValueError for an unknown id."""
    with store.connection() as conn:
        item = _get(conn, item_id)
    if item is None:
        raise ValueError(f"there is no board item #{item_id}")
    return item


def listing(store: Store, project: str | None = None, state: str | None = None) -> list[Item]:
    """Items by project, state (in STATES order) and position."""
    where, args = [], []
    if project is not None:
        where.append("project = ?")
        args.append(project)
    if state is not None:
        where.append("state = ?")
        args.append(check_state(state))
    order = " ".join(f"WHEN '{name}' THEN {n}" for n, name in enumerate(STATES))
    query = (
        "SELECT * FROM board_items"
        + (" WHERE " + " AND ".join(where) if where else "")
        + f" ORDER BY project, CASE state {order} ELSE {len(STATES)} END, position, id"
    )
    with store.connection() as conn:
        return [Item.from_row(row) for row in conn.execute(query, args).fetchall()]


def add(
    store: Store,
    project: str,
    title: Any,
    description: Any = "",
    state: Any = IDEA,
    agent_id: Any = None,
    branch: Any = None,
    actor: Actor = OWNER,
) -> Item:
    """A new item at the end of its state, with its links checked first (as in `cleaned`):
    either it is stored whole or not at all. `project` must be registered (the caller
    checks). Raises ValueError."""
    title = _text(title, "title", TITLE_CHARS, required=True)
    description = _text(description, "description", DESCRIPTION_CHARS)
    state = check_state(state or IDEA)
    given = {"agent_id": agent_id, "branch": branch}
    links = cleaned(store, Item(0, project, title), {k: v for k, v in given.items() if v})
    run_id = _run_of(store, actor)
    with store.transaction() as conn:
        stamp = now()
        cursor = conn.execute(
            "INSERT INTO board_items (project, title, description, state, position,"
            " agent_id, branch, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                project,
                title,
                description,
                state,
                _end(conn, project, state),
                links.get("agent_id"),
                links.get("branch"),
                stamp,
                stamp,
            ),
        )
        item = _get(conn, cursor.lastrowid)
        assert item is not None
        changes = _diff(None, item, ("state", "agent_id", "branch"))
        record(conn, item.id, actor, "add", changes, stamp, run_id)
    return item


def cleaned(store: Store, item: Item, changes: dict[str, Any]) -> dict[str, Any]:
    """The changes as stored: only FIELDS; "" clears the agent and the branch, and an agent
    must exist in the item's project. Raises ValueError."""
    unknown = set(changes) - set(FIELDS)
    if unknown:
        raise ValueError(f"unknown fields: {', '.join(sorted(unknown))}")
    out: dict[str, Any] = {}
    if "title" in changes:
        out["title"] = _text(changes["title"], "title", TITLE_CHARS, required=True)
    if "description" in changes:
        out["description"] = _text(changes["description"], "description", DESCRIPTION_CHARS)
    if "state" in changes:
        out["state"] = check_state(changes["state"])
    if "agent_id" in changes:
        agent_id = _text(changes["agent_id"], "agent", 200) or None
        agent = store.get_agent(agent_id) if agent_id else None
        if agent_id and (agent is None or agent.project != item.project):
            raise ValueError(f"there is no agent '{agent_id}' in project '{item.project}'")
        out["agent_id"] = agent_id
    if "branch" in changes:
        out["branch"] = _text(changes["branch"], "branch", BRANCH_CHARS) or None
    return out


def update(store: Store, item_id: int, changes: dict[str, Any], actor: Actor = OWNER) -> Item:
    """Change some FIELDS of an item; a new state puts it at the end of that state.
    Raises ValueError."""
    item = get(store, item_id)
    fields = cleaned(store, item, changes)
    run_id = _run_of(store, actor)
    with store.transaction() as conn:
        current = _get(conn, item_id)
        if current is None:
            raise ValueError(f"there is no board item #{item_id}")
        fields = {key: value for key, value in fields.items() if getattr(current, key) != value}
        if "state" in fields:
            fields["position"] = _end(conn, current.project, fields["state"])
        if fields:  # a change that changes nothing writes nothing
            fields["updated_at"] = now()
            assignments = ", ".join(f"{key} = ?" for key in fields)
            conn.execute(
                f"UPDATE board_items SET {assignments} WHERE id = ?", (*fields.values(), item_id)
            )
        item = _get(conn, item_id)
        assert item is not None
        if fields:
            diff = _diff(current, item, FIELDS)
            action = "state" if set(diff) == {"state"} else "edit"
            record_gap(conn, item_id, current.updated_at)
            record(conn, item_id, actor, action, diff, item.updated_at, run_id)
    return item


def _ids(conn: sqlite3.Connection, project: str, state: str) -> list[int]:
    """The ids in one state of a project, in their order."""
    rows = conn.execute(
        "SELECT id FROM board_items WHERE project = ? AND state = ? ORDER BY position, id",
        (project, state),
    )
    return [row["id"] for row in rows]


def _renumber(conn: sqlite3.Connection, ids: list[int]) -> None:
    for position, item_id in enumerate(ids):
        conn.execute("UPDATE board_items SET position = ? WHERE id = ?", (position, item_id))


def place(store: Store, item_id: int, state: Any, before: Any = None, actor: Actor = OWNER) -> Item:
    """Put the item into `state` (its own or another one) just before the item `before`,
    which must be in that state of the same project (None: at the end). The state's
    positions are renumbered 0, 1, …, and so are those of the state it left; where it
    already is, nothing changes. One transaction. Raises ValueError."""
    state = check_state(state)
    if before is not None and type(before) is not int:
        raise ValueError("before must be a board item id or null")
    if before == item_id:
        raise ValueError(f"#{item_id} cannot be placed before itself")
    run_id = _run_of(store, actor)
    with store.transaction() as conn:
        item = _get(conn, item_id)
        if item is None:
            raise ValueError(f"there is no board item #{item_id}")
        current = _ids(conn, item.project, state)
        order = [other for other in current if other != item_id]
        if before is None:
            order.append(item_id)
        elif before in order:
            order.insert(order.index(before), item_id)
        elif _get(conn, before) is None:
            raise ValueError(f"there is no board item #{before}")
        else:
            raise ValueError(f"#{before} is not in {item.project} · {state}")
        if state != item.state or order != current:
            _renumber(conn, order)
            if state != item.state:
                _renumber(conn, [i for i in _ids(conn, item.project, item.state) if i != item_id])
            stamp = now()
            conn.execute(
                "UPDATE board_items SET state = ?, updated_at = ? WHERE id = ?",
                (state, stamp, item_id),
            )
            placed = _get(conn, item_id)
            assert placed is not None
            changes = _diff(item, placed, ("state", "position"))
            record_gap(conn, item_id, item.updated_at)
            record(conn, item_id, actor, "place", changes, stamp, run_id)
        item = _get(conn, item_id)
    assert item is not None
    return item


def move(store: Store, item_id: int, direction: str, actor: Actor = OWNER) -> Item:
    """Swap the item with its neighbour above (`up`) or below (`down`) in its state; at the
    edge nothing changes. The state's positions are renumbered 0, 1, … Raises ValueError."""
    if direction not in ("up", "down"):
        raise ValueError(f"direction must be 'up' or 'down', not {direction!r}")
    run_id = _run_of(store, actor)
    with store.transaction() as conn:
        item = _get(conn, item_id)
        if item is None:
            raise ValueError(f"there is no board item #{item_id}")
        ids = [
            row["id"]
            for row in conn.execute(
                "SELECT id FROM board_items WHERE project = ? AND state = ? ORDER BY position, id",
                (item.project, item.state),
            )
        ]
        at = ids.index(item_id)
        other = at - 1 if direction == "up" else at + 1
        if 0 <= other < len(ids):
            ids[at], ids[other] = ids[other], ids[at]
            stamp = now()
            for position, moved in enumerate(ids):
                conn.execute("UPDATE board_items SET position = ? WHERE id = ?", (position, moved))
            conn.execute("UPDATE board_items SET updated_at = ? WHERE id = ?", (stamp, item_id))
            changes = {"position": [item.position, other]}
            record_gap(conn, item_id, item.updated_at)
            record(conn, item_id, actor, "move", changes, stamp, run_id)
        item = _get(conn, item_id)
    assert item is not None
    return item


def message(project: str, items: list[Item], actor: Actor = OWNER) -> str:
    """The one message that hands a project's ready items to its lead."""
    count = f"{len(items)} item" + ("" if len(items) == 1 else "s")
    by = actor.name if actor.kind == AGENT_KIND and actor.name else "the owner"
    parts = [f"Board items ready for project {project} ({count}), dispatched by {by}:"]
    for item in items:
        parts.append(
            f"#{item.id} {item.title}" + (f"\n{item.description}" if item.description else "")
        )
    parts.append(
        "They are now `in_progress` and linked to you. As work proceeds, keep each item's state"
        " current with update_board_item (`done` when finished; `ready` or `idea` if it will"
        " not be done now; `agent`/`branch` for the worker and branch on it)."
    )
    return "\n\n".join(parts)


def dispatch(
    store: Store,
    project: str,
    lead_id: str,
    send: Callable[[str, str], int],
    ids: list[int] | None = None,
    actor: Actor = OWNER,
    states: tuple[str, ...] = (READY,),
) -> tuple[list[Item], int | None]:
    """Hand the project's `ready` items (with `ids`: only those of them, the ones the owner
    confirmed) to its lead: mark them `in_progress` and linked to `lead_id`, then send them
    as one message with `send(lead_id, text)` (returns the run id). No such item: nothing
    is sent (run id None). If sending fails, the items are restored as they were.
    `states`: the states an item may be taken from (ready first, then by position); with
    `ids`, (IDEA, READY) also takes the ideas named."""
    with store.transaction() as conn:  # claimed at once: a second dispatch finds none
        marks = ", ".join("?" for _ in states)
        rows = conn.execute(
            f"SELECT * FROM board_items WHERE project = ? AND state IN ({marks})"
            " ORDER BY CASE state WHEN ? THEN 0 ELSE 1 END, position, id",
            (project, *(check_state(state) for state in states), READY),
        ).fetchall()
        before = [Item.from_row(row) for row in rows]
        if ids is not None:
            before = [item for item in before if item.id in set(ids)]
        start, stamp = _end(conn, project, IN_PROGRESS), now()
        for offset, item in enumerate(before):
            conn.execute(
                "UPDATE board_items SET state = ?, position = ?, agent_id = ?, updated_at = ?"
                " WHERE id = ?",
                (IN_PROGRESS, start + offset, lead_id, stamp, item.id),
            )
            # The history follows each step: the claim here, its run once known (link_run),
            # a restore as `restore`.
            claimed = replace(item, state=IN_PROGRESS, agent_id=lead_id)
            changes = _diff(item, claimed, ("state", "agent_id"))
            record_gap(conn, item.id, item.updated_at)
            record(conn, item.id, actor, "dispatch", changes, stamp)
    if not before:
        return [], None
    try:
        run_id = send(lead_id, message(project, before, actor))
    except BaseException:
        with store.transaction() as conn:
            restored = now()
            for item in before:
                cursor = conn.execute(
                    "UPDATE board_items SET state = ?, position = ?, agent_id = ?, updated_at = ?"
                    " WHERE id = ? AND state = ?",
                    (
                        item.state,
                        item.position,
                        item.agent_id,
                        item.updated_at,
                        item.id,
                        IN_PROGRESS,
                    ),
                )
                if cursor.rowcount:  # its old updated_at comes back: the entry is later
                    claimed = replace(item, state=IN_PROGRESS, agent_id=lead_id)
                    back = _diff(claimed, item, ("state", "agent_id"))
                    record(conn, item.id, actor, "restore", back, restored)
        raise
    with store.transaction() as conn:
        link_run(conn, [item.id for item in before], stamp, run_id)
    return [get(store, item.id) for item in before], run_id
