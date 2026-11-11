"""The orchestrator facade. The CLI, the MCP server and the dashboard all go through here.

Most methods are functions of the core modules bound here (see core/__init__.py)."""

from __future__ import annotations

import sys
from dataclasses import replace
from pathlib import Path
from typing import Any

from . import board, decisions, desktop, git, notify, providers, roles, settings
from .core import archive as archiving
from .core import compaction, dispatch, launcher, views
from .core.dispatch import ORCHESTRATOR_ID as ORCHESTRATOR_ID  # old import path
from .core.dispatch import slug as slug  # old import path
from .core.views import context_view as context_view  # old import path
from .core.views import result_file as result_file  # old import path
from .core.views import run_usage as run_usage  # old import path
from .errors import NeedsConfirmation as NeedsConfirmation  # old import path
from .errors import OrchestratorError
from .models import Agent, Project
from .store import Store
from .timeline import agent_state as agent_state  # old import path
from .timeline import compaction_left as compaction_left  # old import path

# What a model or effort switch in a resumed session does to the prompt cache. Measured
# with real turns: a model switch re-wrote Claude's cache on --resume, and codex-cli
# 0.155.1 read 0 cached tokens after `-m` changed on `exec resume`; an effort switch alone
# kept Codex's cache (12k of 14k tokens cached). Claude's effort switch was not measured.
RESTARTS, MAY_RESTART, KEEPS = "restarts", "may restart", "keeps"
CACHE_EFFECT = {
    "claude": {"model": RESTARTS, "effort": MAY_RESTART},
    "codex": {"model": RESTARTS, "effort": KEEPS},
}
CACHE_EFFECT_UNKNOWN = {"model": MAY_RESTART, "effort": MAY_RESTART}
CACHE_WORDS = {
    RESTARTS: "starts a fresh prompt cache",
    MAY_RESTART: "may start a fresh prompt cache",
    KEEPS: "keeps the prompt cache",
}


def cache_effect(provider: str) -> dict[str, str]:
    return CACHE_EFFECT.get(provider, CACHE_EFFECT_UNKNOWN)


def cache_hint(provider: str, keys: tuple[str, ...] = ("model", "effort")) -> str:
    """What switching these settings in an existing session does to the prompt cache."""
    effect = cache_effect(provider)
    text = "; ".join(f"switching the {key} {CACHE_WORDS[effect[key]]}" for key in keys)
    text = text[0].upper() + text[1:] + "."
    if any(effect[key] != KEEPS for key in keys):
        text += " With a fresh cache the next turn re-reads the whole conversation at full price."
    return text


class Orchestrator:
    def __init__(self, store: Store | None = None) -> None:
        self.store = store or Store()
        self.store.on_run_end = self.run_ended

    # -- projects ---------------------------------------------------------

    def add_project(
        self, path: str, name: str | None = None, default_provider: str = "claude"
    ) -> Project:
        try:
            root = git.repo_root(Path(path).expanduser().resolve())
        except git.GitError, FileNotFoundError, NotADirectoryError:
            raise OrchestratorError(f"{path} is not inside a git repository") from None
        providers.get(default_provider)
        name = slug(name or root.name)
        if self.store.get_project(name):
            raise OrchestratorError(f"project '{name}' already exists")
        project = Project(name, str(root), git.current_branch(root), default_provider)
        return self.store.add_project(project)

    def project_note(self, project: Project) -> str | None:
        """What a person should know about a project right after registering it."""
        if git.has_commits(project.path):
            return None
        return (
            "This repository has no commits yet. The first agent that needs its own worktree "
            "creates an empty initial commit; worktree agents only see committed files, so "
            "commit existing work first, or let the project lead work in the main checkout."
        )

    def project(self, name: str) -> Project:
        project = self.store.get_project(name)
        if project is None:
            known = ", ".join(p.name for p in self.store.list_projects()) or "none"
            raise OrchestratorError(f"unknown project '{name}' (registered: {known})")
        return project

    def remove_project(self, name: str) -> None:
        self.project(name)
        if self.store.list_agents(name):
            raise OrchestratorError(
                f"project '{name}' still has agents (archived ones too); archive and delete them first"
            )
        self.store.delete_project(name)

    # -- agents -----------------------------------------------------------

    def agent(self, agent_id: str) -> Agent:
        agent = self.store.get_agent(agent_id)
        if agent is None:
            raise OrchestratorError(f"unknown agent '{agent_id}'")
        return agent

    # -- dispatch (core/dispatch.py) --------------------------------------

    create_agent = dispatch.create_agent
    send = dispatch.send
    spawn = dispatch.spawn
    lead = dispatch.lead
    orchestrator = dispatch.orchestrator
    withdraw = dispatch.withdraw
    replace_message = dispatch.replace_message
    role_schema = staticmethod(dispatch.role_schema)
    _cause = dispatch._cause
    _check_notify = staticmethod(dispatch._check_notify)
    _orchestrator_draft = staticmethod(dispatch._orchestrator_draft)
    _resolve_after = dispatch._resolve_after

    # -- compaction (core/compaction.py) ----------------------------------

    compact = compaction.compact
    compact_prompt = compaction.compact_prompt
    compact_refusal = compaction.compact_refusal
    auto_compact = compaction.auto_compact
    compaction_paused = compaction.compaction_paused
    compaction_ended = compaction.compaction_ended
    _is_lead = compaction._is_lead
    _note_paused = compaction._note_paused

    # -- runner processes (core/launcher.py) ------------------------------

    _launch = launcher._launch
    stop = launcher.stop
    stop_runs = launcher.stop_runs
    _kill = staticmethod(launcher._kill)

    def run_ended(self, run_id: int, agent_id: str, recheck: bool = False) -> None:
        """Every path that ends a run calls this once it has ended (or was deleted): it
        queues the notifications that are due (see notify.run_ended). Never raises: the
        caller is ending a run and must not fail because of this."""
        try:
            notify.run_ended(self.store, run_id, agent_id, self._launch, recheck)
        except Exception as exc:
            print(f"notifying the parents of {agent_id} failed: {exc!r}", file=sys.stderr)
        desktop.turn_ended(self.store, run_id, agent_id)  # never raises

    # -- decisions (decisions.py) -----------------------------------------

    def request_decision(
        self, agent_id: str, question: Any, options: Any = None, recommendation: Any = None
    ) -> decisions.Decision:
        try:
            return decisions.record(
                self.store, self.agent(agent_id), question, options, recommendation
            )
        except ValueError as exc:
            raise OrchestratorError(str(exc)) from None

    def withdraw_decision(self, decision_id: int, agent_id: str) -> decisions.Decision:
        try:
            return decisions.withdraw(self.store, decision_id, agent_id)
        except ValueError as exc:
            raise OrchestratorError(str(exc)) from None

    def list_decisions(self, closed: bool = False) -> list[dict[str, Any]]:
        """Open decisions (with `closed` all), each with the requesting agent's display
        name and whether it is archived (an answer restores it) or gone."""
        archived = self.store.archived()
        listed = []
        for decision in decisions.listing(self.store, closed):
            agent = self.store.get_agent(decision.agent_id)
            state = "gone" if agent is None else "archived" if agent.id in archived else None
            listed.append(
                {
                    **decision.to_dict(),
                    "agent_name": desktop.display_name(self.store, decision.agent_id),
                    "agent_state": state,
                }
            )
        return listed

    def answer_decision(self, decision_id: int, answer: Any) -> dict[str, Any]:
        """Answer an open decision (an option number picks that option); the answer goes
        to the agent as a user message."""
        try:
            decision, run_id = decisions.answer(
                self.store, decision_id, answer, lambda agent_id, text: self.send(agent_id, text).id
            )
        except ValueError as exc:
            raise OrchestratorError(str(exc)) from None
        return {"decision": decision.to_dict(), "agent_id": decision.agent_id, "run_id": run_id}

    # -- task board (board.py) --------------------------------------------

    def board_items(self, project: str | None = None, state: str | None = None) -> list[board.Item]:
        """A project's items (None: every registered project's), optionally of one state.
        A removed project's items are kept: registering it again brings them back."""
        try:
            items = board.listing(self.store, project and self.project(project).name, state)
        except ValueError as exc:
            raise OrchestratorError(str(exc)) from None
        if project:
            return items
        known = {p.name for p in self.store.list_projects()}
        return [item for item in items if item.project in known]

    def board_item(self, item_id: int) -> board.Item:
        try:
            return board.get(self.store, item_id)
        except ValueError as exc:
            raise OrchestratorError(str(exc)) from None

    def add_board_item(
        self,
        project: str,
        title: Any,
        description: Any = "",
        state: Any = board.IDEA,
        agent_id: Any = None,
        branch: Any = None,
    ) -> board.Item:
        """A new item, with its optional links; all of it is checked before it is stored."""
        name = self.project(project).name
        try:
            return board.add(self.store, name, title, description, state, agent_id, branch)
        except ValueError as exc:
            raise OrchestratorError(str(exc)) from None

    def update_board_item(self, item_id: int, changes: dict[str, Any]) -> board.Item:
        """Change title, description, state, agent_id or branch ("" clears a link)."""
        try:
            return board.update(self.store, item_id, changes)
        except ValueError as exc:
            raise OrchestratorError(str(exc)) from None

    def move_board_item(self, item_id: int, direction: str) -> board.Item:
        try:
            return board.move(self.store, item_id, direction)
        except ValueError as exc:
            raise OrchestratorError(str(exc)) from None

    def dispatch_board(self, project: str, ids: Any = None) -> dict[str, Any]:
        """Send the project's ready items (with `ids`: only those of them that are still
        ready) to its lead as one user message (none: nothing is sent); they become
        in_progress and linked to the lead."""
        name = self.project(project).name
        if ids is not None and (not isinstance(ids, list) or not all(type(i) is int for i in ids)):
            raise OrchestratorError("ids must be a list of board item ids")
        ready = [item.id for item in board.listing(self.store, name, board.READY)]
        if not [i for i in ready if ids is None or i in ids]:
            return {"items": [], "agent_id": None, "run_id": None}
        lead = self.lead(name)
        items, run_id = board.dispatch(
            self.store, name, lead.id, lambda agent_id, text: self.send(agent_id, text).id, ids
        )
        return {"items": [item.to_dict() for item in items], "agent_id": lead.id, "run_id": run_id}

    # -- settings ---------------------------------------------------------

    def settings(self, project: str | None = None) -> dict[str, Any]:
        return self.store.get_settings(self.project(project).name if project else settings.GLOBAL)

    def update_settings(self, data: Any, project: str | None = None) -> dict[str, Any]:
        """Replace the model/effort settings globally or, with `project`, for one project."""
        try:
            cleaned = settings.clean(data)
        except ValueError as exc:
            raise OrchestratorError(str(exc)) from None
        if project and "orchestrator" in cleaned:
            raise OrchestratorError("the orchestrator tier can only be set globally")
        for key in settings.SWITCHES:
            if project and key in cleaned:
                raise OrchestratorError(f"{key} can only be set globally")
        scope = self.project(project).name if project else settings.GLOBAL
        # Kept when not named (the settings dialog sends only its own fields; a page from
        # before a key existed does not send that one).
        kept = (settings.ARCHIVE_AFTER, settings.COMPACT_AT)
        for key in kept if project else kept + settings.SWITCHES:
            if isinstance(data, dict) and key not in data:
                stored = self.store.get_settings(scope).get(key)
                cleaned.update({} if stored is None else {key: stored})
        self.store.set_settings(scope, cleaned)
        return cleaned

    def effective(self, agent: Agent) -> dict[str, str | None]:
        """Model and effort the agent's next turn will use."""
        project = self.store.get_project(agent.project) if agent.project else None
        try:
            role = roles.load(agent.role, project.path if project else None)
        except KeyError:  # the role file was removed; the next turn would fail anyway
            return {"model": agent.model, "effort": agent.effort}
        return settings.resolve(self.store, agent, role)

    def inherited(self, agent: Agent) -> dict[str, str | None]:
        """Model and effort the agent's next turn would use without its own values."""
        return self.effective(replace(agent, model=None, effort=None))

    def setup(self, agent_id: str) -> dict[str, Any]:
        """An agent's model and effort: what its next turn uses (`effective`), the values
        set on the agent itself (`model_override`/`effort_override`; null: inherited) and
        what applies without them (`inherited`); `running`: a turn is running (a change
        applies from the next one); `cache_effect`: per setting, what switching it in an
        existing session does to the prompt cache (`restarts`, `may restart`, `keeps`) and
        `cache_hint` says it in words. The orchestrator is shown before its first use too
        (`exists` false), without creating anything."""
        agent = self._agent_or_orchestrator_draft(agent_id)
        return {
            "agent_id": agent.id,
            "provider": agent.provider,
            "exists": self.store.get_agent(agent.id) is not None,
            "effective": self.effective(agent),
            "model_override": agent.model,
            "effort_override": agent.effort,
            "inherited": self.inherited(agent),
            "session_id": agent.session_id,
            "running": self.store.running_run(agent.id) is not None,
            "cache_effect": cache_effect(agent.provider),
            "cache_hint": cache_hint(agent.provider),
        }

    def set_overrides(self, agent_id: str, changes: dict[str, Any]) -> dict[str, Any]:
        """Set or clear an agent's own model and/or effort; returns its `setup`.

        `changes` holds `model` and/or `effort`; a string sets the value, None clears it
        (back to the tier default), a missing key keeps it. The values are resolved when a
        turn starts, so a running turn is not affected; the next one uses them. The
        orchestrator is created on first use, like a message to it does.
        """
        if not isinstance(changes, dict):
            raise OrchestratorError("expected an object with 'model' and/or 'effort'")
        unknown = sorted(set(changes) - set(settings.FIELDS))
        if unknown:
            raise OrchestratorError(f"cannot set {', '.join(unknown)} (use: model, effort)")
        if not changes:
            raise OrchestratorError("nothing to change: give 'model' and/or 'effort'")
        agent = self._agent_or_orchestrator_draft(agent_id)
        if changes.get("model") is not None:
            try:
                settings.check_model(changes["model"])
            except ValueError as exc:
                raise OrchestratorError(str(exc)) from None
        if "effort" in changes:
            effort = changes["effort"]
            try:
                allowed = providers.get(agent.provider).efforts
            except KeyError as exc:
                raise OrchestratorError(str(exc.args[0])) from None
            if effort is not None and effort not in allowed:
                raise OrchestratorError(
                    f"{agent.provider} has no effort {effort!r} (use: {', '.join(allowed)})"
                )
        if agent.id == ORCHESTRATOR_ID:
            self.orchestrator()
        self.store.set_overrides(agent.id, changes)
        return self.setup(agent.id)

    def _agent_or_orchestrator_draft(self, agent_id: str) -> Agent:
        if agent_id == ORCHESTRATOR_ID:
            return self.store.get_agent(ORCHESTRATOR_ID) or self._orchestrator_draft()
        return self.agent(agent_id)

    # -- observation (core/views.py) --------------------------------------

    view = views.view
    _context = views._context
    run_history = views.run_history
    overview = views.overview
    wait_idle = views.wait_idle
    settled = views.settled
    wait = views.wait
    changes = views.changes

    # -- archive (core/archive.py) ----------------------------------------

    is_lead = archiving.is_lead
    unsaved = archiving.unsaved
    _held = archiving._held
    _check_held = archiving._check_held
    _archivable = archiving._archivable
    archive = archiving.archive
    restore = archiving.restore
    archive_safe = archiving.archive_safe
    auto_archive = archiving.auto_archive
    tidy = archiving.tidy
    remove = archiving.remove
