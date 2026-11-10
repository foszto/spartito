"""Notifications: a parent agent is woken by an automatic turn when its children finish.

A child is *settled* when neither it nor any of its descendants has a waiting or running
run. A finished run of child C is *deliverable* to its parent P when P has not received it
yet (table `deliveries`, shared with wait_for_agents) and either P sent it, or it is a
notification turn of C whose origin is P: it reports work C did for P (that is where a lead
writes its final report). Runs without metadata (queued by older code) are never
deliverable, so going live floods no one.

A notification for P is *due* when a settled child has deliverable runs and either P asked
to hear about that child on its own (`notify: each`, the default) or every child P sent
work to is settled. Then one notification run is queued on P; its text is written only
when it starts, after a short settle delay, so completions that arrive meanwhile share one
turn. Only the end of a run triggers this check (`run_ended`), never a read.
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
from .store import MESSAGE, NOTIFICATION, Store, now, subtree_sql

SETTLE_SECONDS = 5.0  # a starting notification waits this long for more completions
SETTLE_ENV = "ORCH_NOTIFY_DELAY"  # overrides SETTLE_SECONDS (tests set 0)
AGENT_CHARS = 4000  # answer text per agent in one notification
MESSAGE_CHARS = 10000  # the whole notification text
LISTED_IDS = 50  # earlier undelivered runs of one agent listed by id
LISTED_AGENTS = 20  # agents listed in not_shown and still_running; the rest are counted
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
    sent: bool  # the parent sent it a run
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
            " AND ((m.kind = ? AND m.sender = ?) OR (m.kind = ? AND m.origin = ?))"
            " AND NOT EXISTS (SELECT 1 FROM deliveries d WHERE d.caller = ? AND d.run_id = r.id)"
            " ORDER BY r.id",
            (child, MESSAGE, parent_id, NOTIFICATION, parent_id, parent_id),
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
                sent=mode is not None,
                deliverable=[r["id"] for r in deliverable],
            )
        )
    return found


def reportable(kids: list[Child]) -> list[Child]:
    """The children whose results are due to be reported now. `all` waits only for the
    children the parent sent work to, not for others (e.g. started by the user)."""
    everyone = all(kid.settled for kid in kids if kid.sent)
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
    """Check the agent's parent, and further up, since a finished grandchild can leave a
    child settled."""
    seen = {agent_id}
    agent = store.get_agent(agent_id)
    while agent and agent.parent_id and agent.parent_id not in seen:
        seen.add(agent.parent_id)
        try:
            notify(store, agent.parent_id, launch)
        except Exception as exc:  # one broken ancestor must not keep the others unaware
            print(f"notifying {agent.parent_id} failed: {exc!r}", file=sys.stderr)
        agent = store.get_agent(agent.parent_id)


def run_ended(
    store: Store, run_id: int, agent_id: str, launch: Launch, recheck: bool = False
) -> None:
    """Every path that ends a run calls this afterwards (the run may be deleted already).

    A notification turn that did not succeed gives back what it claimed: those results
    reach the parent with its next notification, which only a later run end causes (no
    retry loop), and wait_for_agents returns them meanwhile. The agent itself is checked
    once when a message turn of it succeeded (that brings back results released by a
    failed notification) or with `recheck` (a notification of it was lost before it
    started); the end of a notification never checks its own agent. Then the ancestors
    are checked and the run is marked checked.
    """
    run = store.get_run(run_id)
    if run is not None and not run.status.active and run.status is not RunStatus.DONE:
        release(store, run_id)
    if run is not None and run.status is RunStatus.DONE and store.meta(run_id).kind == MESSAGE:
        recheck = True
    if recheck:
        try:
            notify(store, agent_id, launch)
        except Exception as exc:
            print(f"notifying {agent_id} failed: {exc!r}", file=sys.stderr)
    notify_ancestors(store, agent_id, launch)
    if run is not None:
        store.mark_checked(run_id)


def release(store: Store, run_id: int) -> None:
    """Undo the deliveries a notification run claimed."""
    with store.transaction() as conn:
        row = conn.execute(
            "SELECT agent_id, claimed FROM run_meta WHERE run_id = ? AND kind = ?",
            (run_id, NOTIFICATION),
        ).fetchone()
        if row is None or not row["claimed"]:
            return
        conn.executemany(
            "DELETE FROM deliveries WHERE caller = ? AND run_id = ?",
            [(row["agent_id"], claimed) for claimed in json.loads(row["claimed"])],
        )
        conn.execute("UPDATE run_meta SET claimed = NULL WHERE run_id = ?", (run_id,))


def settled(store: Store, agent_id: str) -> bool:
    """Whether the agent and all its descendants are idle: no run is waiting or running,
    and every ended run had its parents checked (else a notification may be on its way).
    Read only."""
    return not store.subtree_busy(agent_id)


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
        claimed = [run_id for kid in due_now for run_id in kid.deliverable]
        conn.executemany(
            "INSERT OR IGNORE INTO deliveries (caller, run_id, delivered_at) VALUES (?, ?, ?)",
            [(run.agent_id, run_id, now()) for run_id in claimed],
        )
        origin, cause = _origin(conn, run.agent_id, claimed)
        conn.execute(
            "UPDATE run_meta SET origin = ?, cause_run = ?, claimed = ? WHERE run_id = ?",
            (origin, cause, json.dumps(claimed), run.id),
        )
        store.start_run(conn, run.id, prompt=render(conn, kids, due_now))
    started = store.get_run(run.id)
    if started is None:
        raise Gone
    return started


def _origin(
    conn: sqlite3.Connection, agent_id: str, reported: list[int]
) -> tuple[str | None, int | None]:
    """Origin and cause of a notification on `agent_id` reporting `reported`: its parent,
    if one of those runs was queued during a turn of this agent that worked for the parent
    (and that turn's cause), else nothing — the report then concerns no one above."""
    parent = conn.execute("SELECT parent_id FROM agents WHERE id = ?", (agent_id,)).fetchone()
    if not parent or not parent["parent_id"] or not reported:
        return None, None
    marks = ", ".join("?" * len(reported))
    row = conn.execute(
        "SELECT c.origin, c.cause_run FROM run_meta r JOIN run_meta c ON c.run_id = r.cause_run"
        f" WHERE r.run_id IN ({marks}) AND c.agent_id = ? AND c.origin = ? LIMIT 1",
        (*reported, agent_id, parent["parent_id"]),
    ).fetchone()
    return (row["origin"], row["cause_run"]) if row else (None, None)


# -- the text ---------------------------------------------------------------


def _compact(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, separators=(",", ":"))


def _text(shown: list[dict[str, Any]], unshown: list[dict[str, Any]], running: list[str]) -> str:
    body: dict[str, Any] = {"agents": shown}
    notes = []
    if any("earlier_run_ids" in agent for agent in shown):
        notes.append(
            "earlier_run_ids count as delivered without being shown;"
            " get_agent <id> run_id=<n> result_chars=0 reads one"
        )
    if unshown:
        body["not_shown"] = unshown[:LISTED_AGENTS]
        notes.append(
            f"{len(unshown)} agent(s) in not_shown had no room: their results count as"
            " delivered without being shown; get_agent <id> result_chars=0 reads one"
        )
        if len(unshown) > LISTED_AGENTS:
            body["not_shown_more"] = len(unshown) - LISTED_AGENTS
            notes.append("list_agents names the ones not listed")
    if running:
        body["still_running"] = running[:LISTED_AGENTS]
        if len(running) > LISTED_AGENTS:
            body["still_running_more"] = len(running) - LISTED_AGENTS
    if notes:
        body["note"] = "; ".join(notes)
    return HEADER + _compact(body)


def render(conn: sqlite3.Connection, kids: list[Child], due_now: list[Child]) -> str:
    """The notification text, at most MESSAGE_CHARS: header, then compact JSON per reported
    agent (its latest finished run with an answer of at most AGENT_CHARS, ids of earlier
    undelivered runs), the agents that did not fit (by id and status) and the children
    still running, both lists capped at LISTED_AGENTS with a count of the rest."""
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
    text = _text(shown, unshown, running)
    while len(text) > MESSAGE_CHARS and shown:  # the lists are capped; this is a last resort
        unshown.insert(0, shorts[shown.pop()["id"]])
        text = _text(shown, unshown, running)
    return text[:MESSAGE_CHARS]
