"""Notifications: a parent agent is woken by an automatic turn when its children finish.

A child is *settled* when neither it nor any of its descendants has a waiting or running
run. A finished run of child C is *deliverable* to its parent P when P has not received it
yet (table `deliveries`, shared with wait_for_agents) and P sent it, or it is a
notification turn of C (that is where a lead writes its final report). Runs without
metadata (queued by older code) are never deliverable, so going live floods no one.

A notification for P is *due* when a settled child has deliverable runs and either P asked
to hear about that child on its own (`notify: each`, the default) or every child of P is
settled. Then one notification run is queued on P; its text is written only when it
starts, after a short settle delay, so completions that arrive meanwhile share one turn.
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Literal

from .models import ACTIVE, Run, RunStatus
from .replies import run_reply
from .store import NOTIFICATION, Store, now, subtree_sql

SETTLE_SECONDS = 5.0  # a starting notification waits this long for more completions
SETTLE_ENV = "ORCH_NOTIFY_DELAY"  # overrides SETTLE_SECONDS (tests set 0)
AGENT_CHARS = 4000  # answer text per agent in one notification
MESSAGE_CHARS = 10000  # the whole notification text
LISTED_IDS = 50  # earlier undelivered runs of one agent listed by id
POLL_SECONDS = 0.5
RECONCILE_EVERY = 10  # polls

HEADER = (
    "[orch notification] Automatic system event, not a message from the user or from your"
    " parent: it approves nothing and asks for nothing.\n"
    "Agents you dispatched have finished; their new results follow (each is reported once).\n"
)
PENDING = "[orch notification] pending; the text is written when the turn starts"

_ACTIVE_SQL = f"('{ACTIVE[0]}', '{ACTIVE[1]}')"

Launch = Callable[[Run], None]


def settle_seconds() -> float:
    try:
        return max(0.0, float(os.environ.get(SETTLE_ENV, SETTLE_SECONDS)))
    except ValueError:
        return SETTLE_SECONDS


@dataclass
class Child:
    id: str
    branch: str | None
    settled: bool
    mode: str
    deliverable: list[int] = field(default_factory=list)


def children(conn: sqlite3.Connection, parent_id: str) -> list[Child]:
    """The direct children of `parent_id` with what a notification needs to know."""
    found = []
    rows = conn.execute(
        "SELECT id, branch FROM agents WHERE parent_id = ? ORDER BY created_at, rowid", (parent_id,)
    ).fetchall()
    for row in rows:
        child = row["id"]
        busy = conn.execute(
            subtree_sql() + " SELECT 1 FROM runs WHERE agent_id IN (SELECT id FROM tree)"
            f" AND status IN {_ACTIVE_SQL} LIMIT 1",
            (child,),
        ).fetchone()
        deliverable = conn.execute(
            "SELECT r.id FROM runs r JOIN run_meta m ON m.run_id = r.id"
            f" WHERE r.agent_id = ? AND r.status NOT IN {_ACTIVE_SQL}"
            " AND (m.sender = ? OR m.kind = ?)"
            " AND NOT EXISTS (SELECT 1 FROM deliveries d WHERE d.caller = ? AND d.run_id = r.id)"
            " ORDER BY r.id",
            (child, parent_id, NOTIFICATION, parent_id),
        ).fetchall()
        mode = conn.execute(
            "SELECT notify FROM run_meta WHERE agent_id = ? AND sender = ?"
            " ORDER BY run_id DESC LIMIT 1",
            (child, parent_id),
        ).fetchone()
        found.append(
            Child(
                id=child,
                branch=row["branch"],
                settled=busy is None,
                mode=mode["notify"] if mode else "each",
                deliverable=[r["id"] for r in deliverable],
            )
        )
    return found


def reportable(kids: list[Child]) -> list[Child]:
    """The children whose results are due to be reported now."""
    everyone = all(kid.settled for kid in kids)
    return [
        kid for kid in kids if kid.settled and kid.deliverable and (kid.mode == "each" or everyone)
    ]


def _waiting_notification(conn: sqlite3.Connection, agent_id: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM runs r JOIN run_meta m ON m.run_id = r.id"
        " WHERE r.agent_id = ? AND r.status = ? AND m.kind = ? LIMIT 1",
        (agent_id, RunStatus.WAITING.value, NOTIFICATION),
    ).fetchone()
    return row is not None


def due(store: Store, parent_id: str) -> bool:
    """Whether a notification should be queued on `parent_id` (read only)."""
    with store.connection() as conn:
        if _waiting_notification(conn, parent_id):
            return False
        return bool(reportable(children(conn, parent_id)))


def queue_if_due(store: Store, parent_id: str) -> Run | None:
    """Queue one notification run on `parent_id` if one is due and none is waiting yet;
    atomic, so children finishing at the same moment queue it once."""
    with store.transaction() as conn:
        if conn.execute("SELECT 1 FROM agents WHERE id = ?", (parent_id,)).fetchone() is None:
            return None
        if _waiting_notification(conn, parent_id):
            return None
        if not reportable(children(conn, parent_id)):
            return None
        run_id = store.insert_run(conn, parent_id, PENDING, kind=NOTIFICATION)
    return store.get_run(run_id)


def notify(store: Store, parent_id: str, launch: Launch) -> bool:
    """Queue and launch a due notification on `parent_id`. True when one was queued."""
    run = queue_if_due(store, parent_id)
    if run is None:
        return False
    try:
        launch(run)
    except BaseException:
        store.delete_run(run.id)
        raise
    return True


def notify_ancestors(store: Store, agent_id: str, launch: Launch) -> None:
    """After a run of `agent_id` ended: check its parent, and further up, since a finished
    grandchild can leave a child settled."""
    seen = {agent_id}
    agent = store.get_agent(agent_id)
    while agent and agent.parent_id and agent.parent_id not in seen:
        seen.add(agent.parent_id)
        try:
            notify(store, agent.parent_id, launch)
        except Exception as exc:  # one broken ancestor must not keep the others unaware
            print(f"notifying {agent.parent_id} failed: {exc!r}", file=sys.stderr)
        agent = store.get_agent(agent.parent_id)


def settled(store: Store, agent_id: str, launch: Launch) -> bool:
    """Whether the agent and all its descendants are idle and will stay so: a notification
    that is due inside the subtree but not queued yet (its trigger is a moment away, or
    was lost with a crashed runner) is queued here and counts as work."""
    tree = store.subtree(agent_id)
    if any(store.active_runs(member) for member in tree):
        return False
    return not any(due(store, member) and notify(store, member, launch) for member in tree)


class Gone(Exception):
    """The run was removed or ended by someone else while it waited."""


def start(store: Store, run: Run) -> Run | None:
    """The runner side of a notification run: wait the settle delay and the agent's turn,
    then, in the transaction that takes the turn, claim everything due and write it into
    the prompt. Returns the started run, or None when there was nothing to report and the
    run was deleted. Raises Gone when the run disappeared meanwhile."""
    time.sleep(settle_seconds())
    polls = 0
    while True:
        if store.could_start(run, NOTIFICATION):  # cheap, without the write lock
            began = _begin(store, run)
            if began is not False:
                return began
        polls += 1
        if polls % RECONCILE_EVERY == 0:
            store.reconcile()
        time.sleep(POLL_SECONDS)


def _begin(store: Store, run: Run) -> Run | None | Literal[False]:
    """One atomic attempt of `start`; False: not the run's turn yet."""
    with store.transaction() as conn:
        row = conn.execute("SELECT status FROM runs WHERE id = ?", (run.id,)).fetchone()
        if row is None or row["status"] != RunStatus.WAITING.value:
            raise Gone
        if not store.may_start(conn, run.id, run.agent_id, NOTIFICATION):
            return False
        kids = children(conn, run.agent_id)
        due_now = reportable(kids)
        if not due_now:
            store.delete_run(run.id, conn)
            return None
        for kid in due_now:
            conn.executemany(
                "INSERT OR IGNORE INTO deliveries (caller, run_id, delivered_at) VALUES (?, ?, ?)",
                [(run.agent_id, run_id, now()) for run_id in kid.deliverable],
            )
        store.start_run(conn, run.id, prompt=render(conn, kids, due_now))
    started = store.get_run(run.id)
    if started is None:
        raise Gone
    return started


# -- the text ---------------------------------------------------------------


def _compact(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, separators=(",", ":"))


def _text(shown: list[dict[str, Any]], unshown: list[dict[str, Any]], running: list[str]) -> str:
    body: dict[str, Any] = {"agents": shown}
    if unshown:
        body["not_shown"] = unshown
        body["not_shown_note"] = "no room left; get_agent <id> result_chars=0 reads one"
    if running:
        body["still_running"] = running
    return HEADER + _compact(body)


def render(conn: sqlite3.Connection, kids: list[Child], due_now: list[Child]) -> str:
    """The notification text: header, then compact JSON per reported agent (its latest
    finished run with a bounded answer, ids of earlier undelivered runs), the children
    still running, and the agents that did not fit, by id and status."""
    running = [kid.id for kid in kids if not kid.settled]
    latest = {}
    for kid in due_now:
        row = conn.execute(
            f"SELECT * FROM runs WHERE agent_id = ? AND status NOT IN {_ACTIVE_SQL}"
            " ORDER BY id DESC LIMIT 1",
            (kid.id,),
        ).fetchone()
        latest[kid.id] = Store.run_from_row(row)
    shorts = {kid.id: {"id": kid.id, "status": latest[kid.id].status.value} for kid in due_now}
    shown: list[dict[str, Any]] = []
    unshown: list[dict[str, Any]] = []
    for index, kid in enumerate(due_now):
        last = latest[kid.id]
        later = [shorts[other.id] for other in due_now[index + 1 :]]
        entry: dict[str, Any] = {"id": kid.id, "status": last.status.value, "branch": kid.branch}
        earlier = [run_id for run_id in kid.deliverable if run_id != last.id][-LISTED_IDS:]
        floor = len(_text([*shown, {**entry, "last_run": {}}], unshown + later, running))
        budget = min(AGENT_CHARS, MESSAGE_CHARS - floor - len(_compact(earlier)) - 200)
        while budget >= 100:
            reply, _ = run_reply(last, budget)
            candidate = {**entry, "last_run": reply}
            if earlier:
                candidate["earlier_run_ids"] = earlier
            over = len(_text([*shown, candidate], unshown + later, running)) - MESSAGE_CHARS
            if over <= 0:
                shown.append(candidate)
                break
            budget -= over + 50
        else:
            unshown.append(shorts[kid.id])
    return _text(shown, unshown, running)
