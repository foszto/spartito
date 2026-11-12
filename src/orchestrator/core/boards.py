"""The task board across projects (board.py holds one item's rules): the joined view, placing
an item, dispatching named items and an item's history. The facade's per-project board
methods stay in service.py."""

from __future__ import annotations

import sqlite3
import sys
from collections.abc import Callable
from typing import Any, Protocol

from .. import board, decisions
from ..errors import OrchestratorError
from ..models import Agent, Project, Run
from ..store import Store
from ..timeline import AgentRuns
from . import board_git


class Facade(Protocol):
    """What these functions use of the facade (they are its methods)."""

    store: Store

    def board_items(
        self, project: str | None = None, state: str | None = None
    ) -> list[board.Item]: ...

    def project(self, name: str) -> Project: ...

    def agent(self, agent_id: str) -> Agent: ...

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
    last refreshed (None: not yet), items: the items in board_items' order, each with its
    `live` state (board_live)}, after the throttled board_sync. Later parts may add keys to
    an item (blockers, source); readers treat each as optional."""
    board_sync(orch)
    items = orch.board_items(project, state)
    view = {
        "projects": [p.name for p in orch.store.list_projects()],
        "synced_at": board_git.last_sync(),
        "items": [item.to_dict() for item in items],
    }
    lives = board_live(orch, items)
    for shown in view["items"]:
        shown["live"] = lives[shown["id"]]

    return view


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


# -- where items came from, what blocks them -----------------------------------------


def _blockers(orch: Facade, items: list[dict[str, Any]]) -> dict[int, list[dict[str, Any]]]:
    """The open decisions that block each item, by id: one linked to items blocks only
    those; one without links blocks every item of its agent that is not done. Answered and
    withdrawn decisions block nothing. [{id, question, agent_id, linked}] oldest first."""
    found: dict[int, list[dict[str, Any]]] = {item["id"]: [] for item in items}
    open_ = decisions.listing(orch.store)
    linked: dict[int, set[int]] = {}
    for item_id, decision_id in board.links(orch.store, [d.id for d in open_]):
        linked.setdefault(decision_id, set()).add(item_id)
    for decision in open_:
        named = linked.get(decision.id)
        for item in items:
            if named is not None:
                blocks = item["id"] in named
            else:
                blocks = item["agent_id"] == decision.agent_id and item["state"] != board.DONE
            if blocks:
                found[item["id"]].append(
                    {
                        "id": decision.id,
                        "question": decision.question,
                        "agent_id": decision.agent_id,
                        "linked": named is not None,
                    }
                )
    return found


def _with_links(orch: Facade, items: list[dict[str, Any]]) -> None:
    """Add `source` (the message it was made from: {agent_id, event_id, run_id}, or None)
    and `blockers` (_blockers) to each shown item."""
    found = board.sources(orch.store, [item["id"] for item in items])
    blockers = _blockers(orch, items)
    for item in items:
        item["source"] = found.get(item["id"])
        item["blockers"] = blockers[item["id"]]


def linked_view(
    orch: Facade, project: str | None = None, state: str | None = None
) -> dict[str, Any]:
    """board_view, each item with its `source` and `blockers` (_with_links)."""
    view = board_view(orch, project, state)
    _with_links(orch, view["items"])
    return view


def linked_detail(orch: Facade, item_id: int) -> dict[str, Any]:
    """board_detail with the item's `source` and `blockers` (_with_links) beside `item`."""
    detail = board_detail(orch, item_id)
    shown = dict(detail["item"])
    _with_links(orch, [shown])
    return {**detail, "source": shown["source"], "blockers": shown["blockers"]}


def _fit(text: Any, limit: int) -> Any:
    """A string cut to `limit` chars (the last one "…"); anything else as it is (board.add
    refuses it)."""
    if not isinstance(text, str):
        return text
    text = text.strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


def add_item_from_message(
    orch: Facade,
    agent_id: Any,
    event_id: Any,
    title: Any = None,
    description: Any = None,
    state: Any = board.IDEA,
    project: Any = None,
    actor: board.Actor | None = None,
) -> board.Item:
    """A new item made from a message (event) of an agent's conversation, linked back to
    it (board_sources; its `add` entry names it). The item is in the agent's project
    (`project`: required for the orchestrator's conversation, which has none); `title`
    (default: the message's first line) and `description` (default: the message) are cut to
    the board's limits."""
    if not isinstance(agent_id, str) or type(event_id) is not int:
        raise OrchestratorError("agent_id (a string) and event_id (a number) are required")
    agent = orch.agent(agent_id)
    event = orch.store.get_event(event_id) if event_id in board.SQLITE_IDS else None
    if event is None or event.agent_id != agent.id:
        raise OrchestratorError(f"{agent.id} has no message #{event_id}")
    if not (project or agent.project):
        raise OrchestratorError("name the project: this conversation belongs to none")
    name = orch.project(project or agent.project).name
    text = event.text.strip()
    first = next((line for line in text.splitlines() if line.strip()), "")
    return _checked(
        lambda: board.add(
            orch.store,
            name,
            _fit(title or first, board.TITLE_CHARS),
            _fit(text if description is None else description, board.DESCRIPTION_CHARS),
            state,
            actor=actor or board.OWNER,
            extra=lambda conn, item: board.save_source(
                conn, item.id, agent.id, event.id, event.run_id
            ),
        )
    )


def link_decision(
    orch: Facade,
    item_id: Any,
    decision_id: Any,
    linked: Any = True,
    actor: board.Actor | None = None,
) -> list[dict[str, Any]]:
    """Link an existing decision to the item as one that blocks it (`linked` False: unlink
    it); a decision of one project only to that project's items. The item's open blockers
    afterwards (_blockers)."""
    if type(item_id) is not int or type(decision_id) is not int or type(linked) is not bool:
        raise OrchestratorError("item and decision ids must be numbers, linked true or false")
    item = _checked(lambda: board.get(orch.store, item_id))
    decision = decisions.get(orch.store, decision_id) if decision_id in board.SQLITE_IDS else None
    if decision is None:
        raise OrchestratorError(f"there is no decision #{decision_id}")
    if linked and decision.project not in (None, item.project):
        raise OrchestratorError(f"decision #{decision_id} is about project {decision.project}")
    _checked(lambda: board.link(orch.store, item_id, decision_id, linked, actor or board.OWNER))
    return _blockers(orch, [item.to_dict()])[item_id]


# -- an item's history -----------------------------------------------------------------


def _runs(orch: Facade, entries: list[dict[str, Any]]) -> list[Run]:
    """The runs its history names: a dispatch's, and the turns of agents that changed it."""
    named = sorted({entry["run_id"] for entry in entries if entry["run_id"]})
    return [run for run in map(orch.store.get_run, named) if run]


def board_detail(orch: Facade, item_id: int) -> dict[str, Any]:
    """One item with what happened to it: {item, history: [{ts, actor_kind, actor, action,
    changes, run_id}] oldest first, runs: [{id, agent_id, status, created_at, ended_at}] that
    the history names, unrecorded: it changed after its last recorded change (or has none),
    live: as in board_live}, after the throttled board_sync. Later parts may add keys."""
    board_sync(orch)
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
        for run in _runs(orch, entries)
    ]
    detail = {
        "item": item.to_dict(),
        "history": entries,
        "runs": runs,
        "unrecorded": board.unrecorded(item, entries),
    }
    detail["live"] = board_live(orch, [item])[item.id]

    return detail


# -- live state ------------------------------------------------------------------------

MERGED = board.Actor(board.AUTO_KIND, board_git.AUTO_RULE)  # who moves a merged item to done


def _record_merged(conn: sqlite3.Connection, before: board.Item, tip: str | None) -> None:
    """board_git's recorder: the move to done, by `auto`/merged, with the merged commit."""
    board.record_moved(conn, before, MERGED, {"commit": tip[:7]} if tip else {})


def board_sync(orch: Facade) -> dict[str, Any]:
    """The throttled pass over the items' branches (board_git.sync: at most one every
    SYNC_EVERY seconds across processes, within SYNC_BUDGET), a merged item moved to done
    with its history entry. Never raises: {"checked": [ids], "done": [ids]}."""
    try:
        return board_git.sync(orch.store, _record_merged)
    except Exception as exc:
        print(f"board sync failed: {exc!r}", file=sys.stderr)
        return {"checked": [], "done": []}


def _agent_status(orch: Facade, agent_id: str, archived: set[str]) -> str:
    """As the listings show it (running, waiting, new, or the last run's status), archived,
    or unknown (no such agent)."""
    if orch.store.get_agent(agent_id) is None:
        return "unknown"
    if agent_id in archived:
        return "archived"
    return AgentRuns.read(orch.store, agent_id).state()[0]


def board_live(orch: Facade, items: list[board.Item]) -> dict[int, dict[str, Any]]:
    """The live state of the items by id, from the database and the git cache (nothing is
    computed here): {agent: {id, status} | None, git: board_git.states' entry | None (not
    checked yet, or no branch), pr: None (reserved)}."""
    linked = {item.agent_id for item in items if item.agent_id}
    archived = set(orch.store.archived()) if linked else set()
    agents = {agent_id: _agent_status(orch, agent_id, archived) for agent_id in sorted(linked)}
    gits = board_git.states(orch.store, items)
    return {
        item.id: {
            "agent": {"id": item.agent_id, "status": agents[item.agent_id]}
            if item.agent_id
            else None,
            "git": gits.get(item.id),
            "pr": None,
        }
        for item in items
    }
