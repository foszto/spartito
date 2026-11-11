"""What callers see of agents: the agent view, the listings, run history and context, and
waiting until agents are settled."""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, Protocol

from .. import config, git, notify, settings
from ..models import MESSAGE, Agent, Project, RunStatus, Usage
from ..store import Store
from ..timeline import AgentRuns


class Facade(Protocol):
    """What these functions use of the facade (they are its methods)."""

    store: Store

    def project(self, name: str) -> Project: ...

    def agent(self, agent_id: str) -> Agent: ...

    def effective(self, agent: Agent) -> dict[str, str | None]: ...

    def view(
        self, agent: Agent, result_limit: int | None = None, detail: bool = False
    ) -> dict[str, Any]: ...

    def _context(self, agent: Agent, timeline: AgentRuns) -> dict[str, Any] | None: ...

    def wait_idle(
        self,
        agent_ids: list[str],
        timeout: float = 600,
        interrupt: Callable[[], bool] | None = None,
    ) -> bool: ...

    def settled(self, agent_id: str) -> bool: ...

    def compact_refusal(self, agent: Agent) -> str | None: ...

    def tidy(self) -> list[str]: ...


QUEUE_PREVIEW_CHARS = 120
QUEUE_LISTED = 20  # waiting runs in the detail view's `queue`; `queued_runs` has the count
WAIT_POLL_SECONDS = 1.0


def run_usage(usage: Usage | None) -> dict[str, Any]:
    """One run's own usage: tokens (short keys), cost and the model the CLI reported."""
    if usage is None:
        return {"tokens": None, "cost_usd": None, "model": None}
    return {"tokens": usage.tokens(), "cost_usd": usage.cost, "model": usage.model}


def context_view(record: Usage | None) -> dict[str, Any] | None:
    """How full the conversation is (see models.context_now): `tokens` of `window`,
    `percent`; after a compaction no request followed yet, `compacted` with the size
    unknown. None when nothing is known."""
    if record is None:
        return None
    tokens, window = record.context_tokens, record.context_window
    percent = round(100 * tokens / window) if tokens is not None and window else None
    return {"tokens": tokens, "window": window, "percent": percent, "compacted": tokens is None}


def result_file(run_id: int) -> str | None:
    """Where the runner wrote the run's full final answer, if it did."""
    path = config.result_file(run_id)
    return str(path) if path.is_file() else None


def view(
    orch: Facade, agent: Agent, result_limit: int | None = None, detail: bool = False
) -> dict[str, Any]:
    """One agent as callers see it; kept small because listings repeat it per agent.

    `model`/`effort` are what the next turn runs with (explicit value, project setting,
    global setting, role); `cost_usd` and `tokens` (`in`, `out`, `cache_read`,
    `cache_write`; null when no run reported tokens) are the totals of all runs.
    `last_run.result` is the answer once: structured output as compact JSON when the
    run has it, else the text, cut at `result_limit` chars.
    With `detail`: `last_run.structured` (the text stays in `result`),
    `model_override`/`effort_override` (the values set on the agent),
    `last_run.usage` (that run's own tokens, cost and reported model),
    `last_run.result_file` (the full final answer on disk) and `queue` (the waiting
    runs: id, kind, sender, created_at, prompt preview).
    `children_running` is how many descendants have a queued or running turn.
    `context`: how full the conversation is (see context_view and _context: with the
    automatic compaction threshold), null when unknown.
    A compaction run is never `last_run` nor the status (see agent_state); with
    `detail`, `last_compaction` is the latest one that ended (id, status, result, error,
    ended_at, automatic), `last_run.kind` is `message` or `notification`, and `compact_refusal`
    says why `compact` is not possible now (null: it is).
    """
    timeline = AgentRuns.read(orch.store, agent.id)
    runs, compactions, usage = timeline.runs, timeline.compactions, timeline.usage
    effective = orch.effective(agent)
    status, active, shown = timeline.state()
    result = shown.result if shown else None
    if shown and shown.structured is not None and not detail:
        # The answer once: structured output as compact JSON instead of the text.
        result = json.dumps(shown.structured, ensure_ascii=False, separators=(",", ":"))
    if result and result_limit and len(result) > result_limit:
        result = result[:result_limit] + "… [truncated]"
    totals = timeline.totals()
    view = {
        "id": agent.id,
        "project": agent.project,
        "name": agent.name,
        "role": agent.role,
        "provider": agent.provider,
        "model": effective["model"],
        "effort": effective["effort"],
        "parent_id": agent.parent_id,
        "status": status,
        "branch": agent.branch,
        "cwd": agent.cwd,
        "session_id": agent.session_id,
        "queued_runs": len(active),
        "children_running": orch.store.active_descendants(agent.id),
        "runs": len(runs),
        "cost_usd": totals.cost or 0,
        "tokens": totals.tokens(),
        "context": orch._context(agent, timeline),
        "last_run": shown
        and {
            "id": shown.id,
            "status": shown.status.value,
            "result": result,
            "error": shown.error,
            "started_at": shown.started_at,
            "ended_at": shown.ended_at,
        },
    }
    if detail:
        view["compact_refusal"] = orch.compact_refusal(agent)
        ended = [r for r in runs if r.id in compactions and not r.status.active]
        view["last_compaction"] = None
        if ended:
            last = ended[-1]
            view["last_compaction"] = {
                "id": last.id,
                "status": last.status.value,
                "result": last.result,
                "error": last.error,
                "ended_at": last.ended_at,
                "automatic": bool(orch.store.meta(last.id).auto),
            }
        view["model_override"] = agent.model
        view["effort_override"] = agent.effort
        if mark := orch.store.archived(agent.project).get(agent.id):
            view["archived"] = mark
        meta = timeline.meta
        if shown:
            view["last_run"]["kind"] = meta[shown.id].kind if shown.id in meta else MESSAGE
            view["last_run"]["structured"] = shown.structured
            view["last_run"]["usage"] = run_usage(usage.get(shown.id))
            view["last_run"]["result_file"] = result_file(shown.id)
        view["queue"] = [
            {
                "id": run.id,
                "kind": meta[run.id].kind if run.id in meta else MESSAGE,
                "sender": meta[run.id].sender if run.id in meta else None,
                "created_at": run.created_at,
                "prompt": run.prompt[:QUEUE_PREVIEW_CHARS],
            }
            for run in [run for run in active if run.status is RunStatus.WAITING][:QUEUE_LISTED]
        ]
    return view


def _context(orch: Facade, agent: Agent, timeline: AgentRuns) -> dict[str, Any] | None:
    """context_view, with `compact_at` (the automatic compaction threshold; 0: off) and
    `compact_paused` (the size the last compaction left while that pauses automatic
    compaction, see compaction_paused; else null)."""
    context = context_view(timeline.context_now(agent.session_id))
    if context is not None:
        threshold = settings.compact_at(orch.store, agent.project)
        left = timeline.compaction_left(agent.session_id)
        paused = threshold and left is not None and left[0] is not None and left[0] > threshold
        context["compact_at"] = threshold
        context["compact_paused"] = left[0] if paused else None
    return context


def run_history(orch: Facade, agent: Agent, limit: int = 10) -> list[dict[str, Any]]:
    """The agent's latest runs with their kind, own tokens, cost and reported model."""
    timeline = AgentRuns.read(orch.store, agent.id)
    usage, meta = timeline.usage, timeline.meta
    return [
        {
            "id": run.id,
            "kind": meta[run.id].kind if run.id in meta else MESSAGE,
            "status": run.status.value,
            "created_at": run.created_at,
            "started_at": run.started_at,
            "ended_at": run.ended_at,
            **run_usage(usage.get(run.id)),
        }
        for run in timeline.runs[-limit:]
    ]


def overview(
    orch: Facade, project: str | None = None, archived: bool = False
) -> list[dict[str, Any]]:
    """The agents (of one project) as listings show them: the ones not archived, or
    with `archived` only the archived ones (with `archived_at` and `archived_how`).
    The default listing first runs the automatic archive check when it is due."""
    orch.store.reconcile()
    if not archived:
        orch.tidy()
    marks = orch.store.archived(project)
    views = []
    for agent in orch.store.list_agents(project):
        if (agent.id in marks) != archived:
            continue
        view = orch.view(agent, result_limit=400)
        if archived:
            view["archived_at"] = marks[agent.id]["archived_at"]
            view["archived_how"] = marks[agent.id]["how"]
        views.append(view)
    return views


def wait_idle(
    orch: Facade,
    agent_ids: list[str],
    timeout: float = 600,
    interrupt: Callable[[], bool] | None = None,
) -> bool:
    """Block until the agents are settled: neither they nor any of their descendants
    has a queued or running turn, or an ended one whose parents were not checked yet
    for a notification (True); or until timeout, or until `interrupt()` is true
    (False). Only reads (and reconciles dead runners): waiting never queues a turn."""
    agents = [orch.agent(agent_id) for agent_id in agent_ids]
    deadline = time.monotonic() + timeout
    while True:
        orch.store.reconcile()
        idle = all(orch.settled(agent.id) for agent in agents)
        if idle or time.monotonic() >= deadline or (interrupt and interrupt()):
            return idle
        time.sleep(WAIT_POLL_SECONDS)


def settled(orch: Facade, agent_id: str) -> bool:
    return notify.settled(orch.store, agent_id)


def wait(
    orch: Facade, agent_ids: list[str], timeout: float = 600
) -> tuple[bool, list[dict[str, Any]]]:
    """Block like wait_idle, then return whether they are idle and their detail views
    (the last run's full text in `result`, structured output in `structured`)."""
    idle = orch.wait_idle(agent_ids, timeout)
    return idle, [orch.view(orch.agent(agent_id), detail=True) for agent_id in agent_ids]


def changes(
    orch: Facade, agent_id: str, max_commits: int | None = None, max_status: int | None = None
) -> str:
    """Commits and uncommitted changes; `max_commits`/`max_status` cap the lines."""
    agent = orch.agent(agent_id)
    if agent.project is None:
        return ""
    if not Path(agent.cwd).exists():
        return "workspace not created yet"
    base = agent.base_ref or orch.project(agent.project).base_branch
    return git.summary(agent.cwd, base, max_commits, max_status)
