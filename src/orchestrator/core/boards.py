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
    orch: Facade,
    project: str,
    ids: list[int] | None,
    actor: board.Actor,
    states: tuple[str, ...] = (board.READY,),
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
        states,
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


def _skip(item: board.Item | None, project: str | None, known: set[str]) -> str | None:
    """Why an item named for dispatch is not sent (None: it is)."""
    if item is None:
        return "no such item"
    if project is not None and item.project != project:
        return f"in project {item.project}, not {project}"
    if item.project not in known:
        return f"project {item.project} is not registered"
    if item.state not in (board.IDEA, board.READY):
        return f"already {item.state}"
    return None


def dispatch_items(
    orch: Facade, ids: Any, project: str | None = None, actor: board.Actor | None = None
) -> dict[str, Any]:
    """Send the named items (idea or ready, in any project; `project`: only that one's) to
    their project's lead, one message per project; they become in_progress and linked to
    the lead. {results: [{project, agent_id, run_id, items}] per lead sent to, skipped:
    [{id, reason}] for the others (one project's failed send skips its items)}."""
    if not isinstance(ids, list) or not ids or not all(type(i) is int for i in ids):
        raise OrchestratorError("ids must be a non-empty list of board item ids")
    project = project and orch.project(project).name
    known = {p.name for p in orch.store.list_projects()}
    skipped: list[dict[str, Any]] = []
    groups: dict[str, list[int]] = {}
    for item_id in dict.fromkeys(ids):
        try:
            item: board.Item | None = board.get(orch.store, item_id)
        except ValueError:
            item = None
        if reason := _skip(item, project, known):
            skipped.append({"id": item_id, "reason": reason})
        else:
            assert item is not None
            groups.setdefault(item.project, []).append(item_id)
    results = []
    for name in sorted(groups):
        try:
            items, lead, run_id = to_lead(
                orch, name, groups[name], actor or board.OWNER, (board.IDEA, board.READY)
            )
        except OrchestratorError as exc:
            skipped += [{"id": item_id, "reason": str(exc)} for item_id in groups[name]]
            continue
        sent = {item.id for item in items}
        skipped += [
            {"id": item_id, "reason": "no longer idea or ready"}
            for item_id in groups[name]
            if item_id not in sent
        ]
        if items:
            results.append(
                {
                    "project": name,
                    "agent_id": lead.id,
                    "run_id": run_id,
                    "items": [item.to_dict() for item in items],
                }
            )
    return {"results": results, "skipped": skipped}


# -- an item's history -----------------------------------------------------------------

AGENT_RUNS = 20  # of the item's agent's runs while it was in_progress, in its detail


def _in_progress(entries: list[dict[str, Any]]) -> list[tuple[str, str | None]]:
    """The recorded spans [start, end) in which the item was in_progress (end None: it
    still is)."""
    spans: list[tuple[str, str | None]] = []
    start = None
    for entry in entries:
        change = entry["changes"].get("state")
        if not isinstance(change, list):
            continue
        if change[1] == board.IN_PROGRESS and start is None:
            start = entry["ts"]
        elif change[0] == board.IN_PROGRESS and start is not None:
            spans.append((start, entry["ts"]))
            start = None
    if start is not None:
        spans.append((start, None))
    return spans


def _runs(orch: Facade, item: board.Item, entries: list[dict[str, Any]]) -> list[Run]:
    """The runs that worked on the item: those its history names (a dispatch's, an acting
    agent's), and the last AGENT_RUNS runs of its agent started while it was in_progress."""
    named = {entry["run_id"] for entry in entries if entry["run_id"]}
    runs = {run.id: run for run in map(orch.store.get_run, named) if run}
    spans = _in_progress(entries)
    if item.agent_id and spans:

        def during(run: Run) -> bool:
            began = run.started_at or run.created_at
            return any(start <= began and (end is None or began < end) for start, end in spans)

        during_runs = [run for run in orch.store.runs_of(item.agent_id) if during(run)]
        runs.update((run.id, run) for run in during_runs[-AGENT_RUNS:])
    return [runs[run_id] for run_id in sorted(runs)]


def board_detail(orch: Facade, item_id: int) -> dict[str, Any]:
    """One item with what happened to it: {item, history: [{ts, actor_kind, actor, action,
    changes, run_id}] oldest first, runs: [{id, agent_id, status, created_at, ended_at}] that
    worked on it, unrecorded: it changed after its last recorded change (or has none)}."""
    item = _checked(lambda: board.get(orch.store, item_id))
    entries = board.history(orch.store, item_id)
    runs = [
        {
            "id": run.id,
            "agent_id": run.agent_id,
            "status": run.status.value,
            "created_at": run.created_at,
            "ended_at": run.ended_at,
        }
        for run in _runs(orch, item, entries)
    ]
    return {
        "item": item.to_dict(),
        "history": entries,
        "runs": runs,
        "unrecorded": board.unrecorded(item, entries),
    }
