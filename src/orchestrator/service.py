"""The orchestrator facade. The CLI, the MCP server and the dashboard all go through here.

Most methods are functions of the core modules bound here (see core/__init__.py)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from . import app_open, board, decisions, desktop, git, providers
from .core import access, boards, compaction, delivery, dispatch, launcher, lifecycle, reads, views
from .core import archive as archiving
from .core import code_view as code_views
from .core import notices as notice_reads
from .core import setup as agent_setup
from .core import state as state_backups
from .core.dispatch import ORCHESTRATOR_ID as ORCHESTRATOR_ID  # old import path
from .core.dispatch import slug as slug  # old import path
from .core.setup import cache_effect as cache_effect  # old import path
from .core.setup import cache_hint as cache_hint  # old import path
from .core.views import context_view as context_view  # old import path
from .core.views import result_file as result_file  # old import path
from .core.views import run_usage as run_usage  # old import path
from .errors import NeedsConfirmation as NeedsConfirmation  # old import path
from .errors import OrchestratorError
from .models import Agent, Project, Run
from .store import Store
from .timeline import agent_state as agent_state  # old import path
from .timeline import compaction_left as compaction_left  # old import path

# isort: split
# Kept for old import paths: the service no longer uses these itself.
from .core.dispatch import LEAD_ROLE as LEAD_ROLE
from .models import RunStatus as RunStatus
from .models import Usage as Usage
from .textfmt import kilo as kilo
from .timeline import AgentRuns as AgentRuns


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

    def compact_prompt(self, agent: Agent, instructions: str | None) -> str:
        return compaction.compact_prompt(self.store, agent, instructions)

    def compact_refusal(self, agent: Agent) -> str | None:
        return compaction.compact_refusal(self.store, agent)

    def auto_compact(self, run_id: int) -> Run | None:
        return compaction.auto_compact(self.store, run_id, self._launch)

    def compaction_paused(
        self, agent: Agent, threshold: int | None = None
    ) -> tuple[int | None, int] | None:
        return compaction.compaction_paused(self.store, agent, threshold)

    def compaction_ended(self, run_id: int) -> None:
        compaction.compaction_ended(self.store, run_id)

    # -- runner processes (core/launcher.py) ------------------------------

    def _launch(self, run: Run) -> None:
        launcher.launch(self.store, run)

    stop = launcher.stop
    stop_runs = launcher.stop_runs
    _kill = staticmethod(launcher._kill)

    def run_ended(self, run_id: int, agent_id: str, recheck: bool = False) -> None:
        """Every path that ends a run calls this once it has ended (or was deleted): it
        queues the notifications that are due (see core/lifecycle.run_ended). Never raises:
        the caller is ending a run and must not fail because of this."""
        lifecycle.run_ended(self.store, run_id, agent_id, self._launch, recheck)

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

    def open_decisions(self) -> set[int]:
        """The numbers of the open decisions (one query)."""
        return {decision.id for decision in decisions.listing(self.store)}

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

    board_view = boards.linked_view  # every project's board at once (core/boards.py)

    def board_item(self, item_id: int) -> board.Item:
        try:
            return board.get(self.store, item_id)
        except ValueError as exc:
            raise OrchestratorError(str(exc)) from None

    board_detail = boards.linked_detail  # an item with its history and runs

    def add_board_item(
        self,
        project: str,
        title: Any,
        description: Any = "",
        state: Any = board.IDEA,
        agent_id: Any = None,
        branch: Any = None,
        actor: board.Actor | None = None,
    ) -> board.Item:
        """A new item, with its optional links; all of it is checked before it is stored.
        `actor` (here and below): who makes the change; None is the owner."""
        name = self.project(project).name
        try:
            return board.add(
                self.store, name, title, description, state, agent_id, branch, actor or board.OWNER
            )
        except ValueError as exc:
            raise OrchestratorError(str(exc)) from None

    def update_board_item(
        self, item_id: int, changes: dict[str, Any], actor: board.Actor | None = None
    ) -> board.Item:
        """Change title, description, state, agent_id or branch ("" clears a link)."""
        try:
            return board.update(self.store, item_id, changes, actor or board.OWNER)
        except ValueError as exc:
            raise OrchestratorError(str(exc)) from None

    def move_board_item(
        self, item_id: int, direction: str, actor: board.Actor | None = None
    ) -> board.Item:
        try:
            return board.move(self.store, item_id, direction, actor or board.OWNER)
        except ValueError as exc:
            raise OrchestratorError(str(exc)) from None

    place_board_item = boards.place_board_item  # into a state, before an item
    add_item_from_message = boards.add_item_from_message  # linked to its message
    link_decision = boards.link_decision  # a decision that blocks an item

    def dispatch_board(
        self, project: str, ids: Any = None, actor: board.Actor | None = None
    ) -> dict[str, Any]:
        """Send the project's ready items (with `ids`: only those of them that are still
        ready) to its lead as one user message (none: nothing is sent); they become
        in_progress and linked to the lead. An agent `actor` sends it as itself."""
        name = self.project(project).name
        if ids is not None and (not isinstance(ids, list) or not all(type(i) is int for i in ids)):
            raise OrchestratorError("ids must be a list of board item ids")
        ready = [item.id for item in board.listing(self.store, name, board.READY)]
        if not [i for i in ready if ids is None or i in ids]:
            return {"items": [], "agent_id": None, "run_id": None}
        items, lead, run_id = boards.to_lead(self, name, ids, actor or board.OWNER)
        return {"items": [item.to_dict() for item in items], "agent_id": lead.id, "run_id": run_id}

    dispatch_items = boards.dispatch_items  # named items, any projects

    board_sync = boards.board_sync  # the throttled git pass; a merged item moves to done
    board_live = boards.board_live  # the items' agent and branch state, from the database

    # -- settings (core/setup.py) ----------------------------------------

    settings = agent_setup.stored_settings
    update_settings = agent_setup.update_settings
    effective = agent_setup.effective
    inherited = agent_setup.inherited
    setup = agent_setup.setup
    set_overrides = agent_setup.set_overrides
    _agent_or_orchestrator_draft = agent_setup._agent_or_orchestrator_draft

    # -- observation (core/views.py) --------------------------------------

    view = views.view
    _context = views._context
    run_history = views.run_history
    overview = views.overview
    wait_idle = views.wait_idle
    settled = views.settled
    wait = views.wait
    changes = views.changes

    # -- the code view of a worktree, read-only (core/code_view.py) -------

    code_changes = code_views.code_changes
    code_patch = code_views.code_patch
    code_tree = code_views.code_tree
    code_file = code_views.code_file

    # -- plain reads (core/reads.py) --------------------------------------

    projects = reads.projects
    agents = reads.agents
    get_agent = reads.get_agent
    agent_count = reads.agent_count
    archived_ids = reads.archived_ids
    reconcile = reads.reconcile
    active_runs = reads.active_runs
    running_count = reads.running_count
    get_run = reads.get_run
    run_kind = reads.run_kind
    get_usage = reads.get_usage
    total_spent = reads.total_spent
    get_event = reads.get_event
    events = reads.events

    # -- notices: pages, Spartito's window and posting process (core/notices.py) --

    notices = notice_reads.notices
    page_notices = notice_reads.page_notices
    notice_cursor = notice_reads.notice_cursor
    claim_notice = notice_reads.claim_notice
    notice_step = notice_reads.notice_step
    release_notice = notice_reads.release_notice
    leave_waiting = notice_reads.leave_waiting
    hand_over_notice = notice_reads.hand_over_notice
    notice_backlog = notice_reads.notice_backlog
    worth_showing = notice_reads.worth_showing
    ingest_notices = notice_reads.ingest_notices
    notice_trace = notice_reads.notice_trace
    page_present = notice_reads.page_present

    # -- the dashboard window of `spartito app` (app_open.py) ----------------

    # A second `spartito app` asks the open window to come forward (and show a target); its page
    # gets the request with /api/state. No store: callable on the class.
    request_app_open = staticmethod(app_open.request)
    app_open_request = staticmethod(app_open.latest)

    # -- delivery (core/delivery.py, core/access.py) ----------------------

    wait_for_agents = delivery.wait_for_agents
    delivery_floor = delivery.delivery_floor
    mark_delivered = delivery.mark_delivered
    lineage = access.lineage

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

    # -- state: backup, list, restore (core/state.py) ---------------------

    backup_state = state_backups.backup
    list_backups = state_backups.listing
    restore_state = state_backups.restore
