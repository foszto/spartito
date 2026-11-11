"""The task board across projects (board.py holds one item's rules): the joined view, placing
an item, dispatching named items and an item's history. The facade's per-project board
methods stay in service.py."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Protocol

from .. import board
from ..errors import OrchestratorError
from ..models import Agent, Project, Run
from ..store import Store


class Facade(Protocol):
    """What these functions use of the facade (they are its methods)."""

    store: Store

    def board_items(
        self, project: str | None = None, state: str | None = None
    ) -> list[board.Item]: ...

    def project(self, name: str) -> Project: ...

    def lead(self, project_name: str, adopt: str | None = None) -> Agent: ...

    def send(self, agent_id: str, message: str, *args: Any, **kwargs: Any) -> Run: ...


def _checked[T](action: Callable[[], T]) -> T:
    """A board.py call, its ValueError raised as an OrchestratorError."""
    try:
        return action()
    except ValueError as exc:
        raise OrchestratorError(str(exc)) from None


def to_lead(
    orch: Facade, project: str, ids: list[int] | None, actor: board.Actor
) -> tuple[list[board.Item], Agent, int | None]:
    """board.dispatch to the project's lead (created on first use): the owner's message
    comes from the user, an agent's from that agent (as ask_project_lead's does). Returns
    the items sent, the lead and the run id."""
    sender = actor.name if actor.kind == board.AGENT_KIND else None
    lead = orch.lead(project, adopt=sender)
    items, run_id = board.dispatch(
        orch.store,
        project,
        lead.id,
        lambda agent_id, text: orch.send(agent_id, text, sender=sender).id,
        ids,
        actor,
    )
    return items, lead, run_id


# -- the board across projects ---------------------------------------------------------


def board_view(
    orch: Facade, project: str | None = None, state: str | None = None
) -> dict[str, Any]:
    """The board of every registered project (`project`: of that one; `state`: that state
    only) as {projects: every registered project's name, synced_at: when the live state was
    last refreshed (None: not yet), items: the items in board_items' order}. Later parts may
    add keys to an item (live state, blockers, source); readers treat each as optional."""
    items = orch.board_items(project, state)
    return {
        "projects": [p.name for p in orch.store.list_projects()],
        "synced_at": None,
        "items": [item.to_dict() for item in items],
    }


# -- placing an item -------------------------------------------------------------------


def place_board_item(
    orch: Facade,
    item_id: int,
    state: str,
    before: int | None = None,
    actor: board.Actor | None = None,
) -> board.Item:
    """Move an item to `state` (or within its own) just before the item `before` of that
    state and project (None: at the end); see board.place."""
    return _checked(lambda: board.place(orch.store, item_id, state, before, actor or board.OWNER))


# -- dispatching named items -----------------------------------------------------------


# -- an item's history -----------------------------------------------------------------
