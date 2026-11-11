"""Plain reads of the store that the interfaces (CLI, MCP server, dashboard) show, so they
go through the facade instead of the store."""

from __future__ import annotations

from typing import Protocol

from ..models import Event, Run, Usage
from ..store import Store


class Facade(Protocol):
    """What these functions use of the facade (they are its methods)."""

    store: Store


def agent_count(orch: Facade, project: str) -> int:
    """How many agents the project has, the archived ones not counted."""
    return len(orch.store.list_agents(project)) - len(orch.store.archived(project))


def reconcile(orch: Facade) -> None:
    """End the runs whose runner process is gone (see Store.reconcile), before showing them."""
    orch.store.reconcile()


def get_run(orch: Facade, run_id: int) -> Run | None:
    return orch.store.get_run(run_id)


def get_usage(orch: Facade, run_id: int) -> Usage | None:
    return orch.store.get_usage(run_id)


def get_event(orch: Facade, event_id: int) -> Event | None:
    return orch.store.get_event(event_id)


def events(orch: Facade, agent_id: str, after: int = 0, tail: int | None = None) -> list[Event]:
    """The agent's events after the event id `after`, oldest first; `tail`: only the last."""
    return orch.store.events(agent_id, after=after, tail=tail)
