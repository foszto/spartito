"""Plain reads of the store that the interfaces (CLI, MCP server, dashboard) show, so they
go through the facade instead of the store."""

from __future__ import annotations

from typing import Any, Protocol

from ..models import Agent, Event, Project, Run, RunStatus, Usage, spent
from ..store import Store, pid_alive


class Facade(Protocol):
    """What these functions use of the facade (they are its methods)."""

    store: Store


def projects(orch: Facade) -> list[Project]:
    return orch.store.list_projects()


def agents(orch: Facade, project: str | None = None) -> list[Agent]:
    """The agents (of one project), the archived ones too."""
    return orch.store.list_agents(project)


def get_agent(orch: Facade, agent_id: str) -> Agent | None:
    return orch.store.get_agent(agent_id)


def agent_count(orch: Facade, project: str) -> int:
    """How many agents the project has, the archived ones not counted."""
    return len(orch.store.list_agents(project)) - len(orch.store.archived(project))


def archived_ids(orch: Facade) -> list[str]:
    """The ids of all archived agents, sorted."""
    return sorted(orch.store.archived())


def reconcile(orch: Facade) -> None:
    """End the runs whose runner process is gone (see Store.reconcile), before showing them."""
    orch.store.reconcile()


def active_runs(orch: Facade, agent_id: str) -> list[Run]:
    """The agent's queued and running turns."""
    return orch.store.active_runs(agent_id)


def running_count(orch: Facade) -> int:
    """How many agents have a running turn whose runner process is alive (or not yet known).
    Only reads: a dead runner's turn is not ended here (see `reconcile`)."""
    return len(
        {
            run.agent_id
            for run in orch.store.active_runs()
            if run.status == RunStatus.RUNNING and (run.pid is None or pid_alive(run.pid))
        }
    )


def get_run(orch: Facade, run_id: int) -> Run | None:
    return orch.store.get_run(run_id)


def run_kind(orch: Facade, run_id: int) -> str:
    """The run's kind (message, notification, compaction)."""
    return orch.store.meta(run_id).kind


def get_usage(orch: Facade, run_id: int) -> Usage | None:
    return orch.store.get_usage(run_id)


def total_spent(orch: Facade) -> dict[str, Any]:
    """Tokens and cost of every run recorded so far (models.spent): archiving or restoring
    an agent leaves them as they are. `tokens` as in the agent view (null: none reported)."""
    total = spent(*orch.store.all_usage())
    return {"cost_usd": total.cost or 0, "tokens": total.tokens()}


def get_event(orch: Facade, event_id: int) -> Event | None:
    return orch.store.get_event(event_id)


def events(orch: Facade, agent_id: str, after: int = 0, tail: int | None = None) -> list[Event]:
    """The agent's events after the event id `after`, oldest first; `tail`: only the last."""
    return orch.store.events(agent_id, after=after, tail=tail)
