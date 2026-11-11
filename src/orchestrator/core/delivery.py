"""Waiting for agents and delivering their results to a caller (the MCP tool wait_for_agents):
each result once per caller, claimed in the store; notifications count as delivered too."""

from __future__ import annotations

import os
from collections.abc import Callable
from typing import Any, Protocol

from ..models import Agent, Run
from ..replies import run_reply
from ..store import Store
from ..timeline import AgentRuns


class Facade(Protocol):
    """What these functions use of the facade (they are its methods)."""

    store: Store

    def agent(self, agent_id: str) -> Agent: ...

    def wait_idle(
        self,
        agent_ids: list[str],
        timeout: float = 600,
        interrupt: Callable[[], bool] | None = None,
    ) -> bool: ...


EARLIER_WITH_TEXT = 2  # undelivered runs before the latest that wait_for_agents shows
LISTED_RUNS = 20  # further undelivered runs it lists by id and status
LISTED_IDS = 200  # and beyond those, by id only


def _agent_runs(shown: Run | None, claimed: list[Run], limit: int) -> dict[str, Any]:
    """The run part of one agent's wait_for_agents reply, within one budget of `limit`
    chars (0: no limit) for all its texts, newest first.

    `last_run` is the latest finished run, with its answer only if this call claimed it.
    Up to EARLIER_WITH_TEXT earlier claimed runs share what is left (`earlier_runs`);
    the rest are only listed (`other_runs`) — they count as delivered all the same.
    """
    budget = None if limit <= 0 else limit
    parts: dict[str, Any] = {}
    last = None
    if shown:
        is_new = any(run.id == shown.id for run in claimed)
        last, left = run_reply(shown, budget, answer=is_new)
        if not is_new:
            last["already_delivered"] = "get_agent result_chars=0 reads it again"
        budget = None if budget is None else left
    earlier = [run for run in claimed if not shown or run.id != shown.id]
    with_text, listed = [], []
    for run in reversed(earlier):
        if len(with_text) < EARLIER_WITH_TEXT and (budget is None or budget > 0):
            reply, left = run_reply(run, budget)
            budget = None if budget is None else left
            with_text.append(reply)
        else:
            listed.append({"id": run.id, "status": run.status.value})
    if with_text:
        parts["earlier_runs"] = with_text[::-1]
    if listed:
        parts["other_runs"] = listed[:LISTED_RUNS][::-1]
        note = "results not shown; get_agent run_id=<id> reads one"
        if older := [run["id"] for run in listed[LISTED_RUNS:]]:
            parts["older_run_ids"] = older[:LISTED_IDS][::-1]
            note += "; older ones in older_run_ids"
            if len(older) > LISTED_IDS:
                note += f" (newest {LISTED_IDS}; {len(older) - LISTED_IDS} before not listed)"
        parts["other_runs_note"] = note
    return {**parts, "last_run": last}


def delivery_floor(orch: Facade, caller: str, agent_id: str) -> int:
    """The first run of an agent a wait_for_agents call may deliver to `caller`: the one
    after the last run it already got, or, for a caller that never got one, the oldest
    run active when the call started that is not a compaction (else the latest finished
    one: a compaction after the answer, e.g. an automatic one, never hides it)."""
    last = orch.store.last_delivered(caller, agent_id)
    if last is not None:
        return last + 1
    timeline = AgentRuns.read(orch.store, agent_id)
    _, active, shown = timeline.state()
    active = [run for run in active if run.id not in timeline.compactions]
    if active:
        return active[0].id
    return shown.id if shown else 0


def wait_for_agents(orch: Facade, ids: list[str], timeout: float, limit: int) -> dict[str, Any]:
    """wait_for_agents in a worker thread: statuses until idle, then the undelivered results.

    Each agent's runs are read once and everything returned is derived from that read, so
    `finished` matches the statuses even when a run is queued after the idle check.
    """
    caller = os.environ.get("ORCH_AGENT_ID", "")

    def message_waiting() -> bool:
        return bool(caller and orch.store.ready_messages(caller))

    if message_waiting():
        return _statuses(orch, ids, _timelines(orch, ids), MESSAGE_NOTE)
    floors = {agent_id: delivery_floor(orch, caller, agent_id) for agent_id in ids}
    orch.wait_idle(ids, timeout, interrupt=message_waiting)
    timelines = _timelines(orch, ids)
    if message_waiting():
        return _statuses(orch, ids, timelines, MESSAGE_NOTE)
    states = {agent_id: timelines[agent_id].state() for agent_id in ids}
    below = {agent_id: orch.store.active_descendants(agent_id) for agent_id in ids}
    if any(active for _, active, _ in states.values()) or any(below.values()):
        return _statuses(orch, ids, timelines)
    outcomes = []
    for agent_id in ids:
        status, _, shown = states[agent_id]
        new = [run for run in timelines[agent_id].answers() if run.id >= floors[agent_id]]
        claimed = orch.store.claim(caller, [run.id for run in new])
        totals = timelines[agent_id].totals()
        outcome = {
            "id": agent_id,
            "status": status,
            "branch": orch.agent(agent_id).branch,
            "cost_usd": totals.cost or 0,
            "tokens": totals.tokens(),
        }
        claimed_runs = [run for run in new if run.id in claimed]
        outcomes.append({**outcome, **_agent_runs(shown, claimed_runs, limit)})
    return {"finished": True, "agents": outcomes}


MESSAGE_NOTE = "a message for you is waiting: end this turn now with a short status"


def _timelines(orch: Facade, ids: list[str]) -> dict[str, AgentRuns]:
    return {agent_id: AgentRuns.read(orch.store, agent_id) for agent_id in ids}


def _statuses(
    orch: Facade, ids: list[str], timelines: dict[str, AgentRuns], note: str | None = None
) -> dict[str, Any]:
    """The reply of an unfinished wait: statuses only, nothing counts as delivered."""
    agents = []
    for agent_id in ids:
        status, active, _ = timelines[agent_id].state()
        agents.append(
            {
                "id": agent_id,
                "status": status,
                "queued_runs": len(active),
                "children_running": orch.store.active_descendants(agent_id),
            }
        )
    reply: dict[str, Any] = {"finished": False, "agents": agents}
    if note:
        reply["note"] = note
    return reply


def mark_delivered(orch: Facade, caller: str, run_ids: list[int]) -> set[int]:
    """Count these runs as delivered to `caller` (those not yet were: returned), so no
    notification and no wait_for_agents of that caller repeats them."""
    return orch.store.claim(caller, run_ids)
