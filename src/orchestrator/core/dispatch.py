"""Dispatching work: creating agents (the project lead, the orchestrator), queueing their
turns with dependencies, and withdrawing or editing a queued message."""

from __future__ import annotations

import re
import uuid
from dataclasses import replace
from typing import Any, Protocol

from .. import config, providers, roles, settings
from ..errors import OrchestratorError
from ..models import COMPACT, MESSAGE, NOTIFICATION, NOTIFY_MODES, Agent, Project, Run, RunStatus
from ..store import ANYONE, Store
from ..timeline import AgentRuns


class Facade(Protocol):
    """What these functions use of the facade (they are its methods)."""

    store: Store

    def project(self, name: str) -> Project: ...

    def agent(self, agent_id: str) -> Agent: ...

    def create_agent(
        self,
        project_name: str,
        *,
        name: str | None = None,
        role: str = "worker",
        provider: str | None = None,
        model: str | None = None,
        effort: str | None = None,
        worktree: bool | None = None,
        share_with: str | None = None,
        base: str | None = None,
        parent_id: str | None = None,
        permission: str | None = None,
    ) -> Agent: ...

    def send(
        self,
        agent_id: str,
        message: str,
        after: list[str | int] | None = None,
        output_schema: dict[str, Any] | None = None,
        role_schema: bool = False,
        sender: str | None = None,
        notify: str = "each",
    ) -> Run: ...

    def _cause(self, sender: str | None) -> int | None: ...

    @staticmethod
    def _check_notify(notify: str) -> None: ...

    @staticmethod
    def role_schema(role: str, project_path: str | None) -> dict[str, Any] | None: ...

    @staticmethod
    def _orchestrator_draft() -> Agent: ...

    def _resolve_after(self, after: list[str | int] | None) -> list[int]: ...

    def run_ended(self, run_id: int, agent_id: str, recheck: bool = False) -> None: ...

    def _launch(self, run: Run) -> None: ...


LEAD_ROLE = "lead"
ORCHESTRATOR_ID = "orchestrator"
WITHDRAWN_PREVIEW_CHARS = 80  # of a withdrawn message, in the agent's info event


def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:32] or "agent"


def create_agent(
    orch: Facade,
    project_name: str,
    *,
    name: str | None = None,
    role: str = "worker",
    provider: str | None = None,
    model: str | None = None,
    effort: str | None = None,
    worktree: bool | None = None,
    share_with: str | None = None,
    base: str | None = None,
    parent_id: str | None = None,
    permission: str | None = None,
) -> Agent:
    """Register an agent. Its worktree is created lazily, when its first run starts."""
    project = orch.project(project_name)
    try:
        role_def = roles.load(role, project.path)
        provider_name = provider or role_def.provider or project.default_provider
        providers.get(provider_name)
    except KeyError as exc:
        raise OrchestratorError(str(exc.args[0])) from None
    if model:
        try:
            settings.check_model(model)
        except ValueError as exc:
            raise OrchestratorError(str(exc)) from None
    if effort and effort not in providers.get(provider_name).efforts:
        allowed = ", ".join(providers.get(provider_name).efforts)
        raise OrchestratorError(f"{provider_name} has no effort '{effort}' (use: {allowed})")
    if parent_id:
        orch.agent(parent_id)

    agent_id = f"{slug(name or role)}-{uuid.uuid4().hex[:4]}"
    branch = base_ref = None
    if share_with:
        cwd = orch.agent(share_with).cwd
    elif role_def.worktree if worktree is None else worktree:
        branch = f"orch/{agent_id}"
        base_agent = orch.store.get_agent(base) if base else None
        base_ref = (base_agent.branch if base_agent else base) or project.base_branch
        cwd = str(config.worktrees_dir(project.name) / agent_id)
    else:
        cwd = project.path
    return orch.store.add_agent(
        Agent(
            id=agent_id,
            project=project.name,
            name=name or role,
            role=role,
            provider=provider_name,
            cwd=cwd,
            parent_id=parent_id,
            model=model or None,
            effort=effort,
            branch=branch,
            base_ref=base_ref,
            permission=permission,
        )
    )


def send(
    orch: Facade,
    agent_id: str,
    message: str,
    after: list[str | int] | None = None,
    output_schema: dict[str, Any] | None = None,
    role_schema: bool = False,
    sender: str | None = None,
    notify: str = "each",
) -> Run:
    """Queue a turn for an agent and start its runner. Returns immediately.

    With `role_schema` and no `output_schema` the agent's role default schema applies.
    `sender` is the agent that sends it (None: the user); when that is the agent's
    parent, `notify` says when it is woken: `each` once this agent is done, `all` once
    all its agents are. The sender's running turn is recorded as the run's cause.
    """
    agent = orch.agent(agent_id)
    orch._check_notify(notify)
    if output_schema is None and role_schema:
        project = orch.store.get_project(agent.project) if agent.project else None
        output_schema = orch.role_schema(agent.role, project.path if project else None)
    run = orch.store.add_run(
        agent.id,
        message,
        orch._resolve_after(after),
        output_schema,
        kind=MESSAGE,
        sender=sender,
        notify=notify,
        cause_run=orch._cause(sender),
    )
    orch._launch(run)
    return run


def _cause(orch: Facade, sender: str | None) -> int | None:
    running = orch.store.running_run(sender) if sender else None
    return running.id if running else None


def _check_notify(notify: str) -> None:
    if notify not in NOTIFY_MODES:
        raise OrchestratorError(f"notify must be one of: {', '.join(NOTIFY_MODES)}")


def spawn(orch: Facade, project_name: str, task: str, **options: Any) -> tuple[Agent, Run]:
    after = options.pop("after", None)
    output_schema = options.pop("output_schema", None)
    sender = options.pop("sender", None)
    notify_mode = options.pop("notify", "each")
    orch._check_notify(notify_mode)
    if options.pop("role_schema", False) and output_schema is None:
        # Resolved before the agent exists, so a broken schema leaves no agent behind.
        path = orch.project(project_name).path
        output_schema = orch.role_schema(options.get("role") or "worker", path)
    agent = orch.create_agent(project_name, **options)
    return agent, orch.send(agent.id, task, after, output_schema, sender=sender, notify=notify_mode)


def role_schema(role: str, project_path: str | None) -> dict[str, Any] | None:
    """The role's default output schema for agent-to-agent handoff (None: free text)."""
    try:
        return roles.output_schema(roles.load(role, project_path), project_path)
    except KeyError as exc:
        raise OrchestratorError(str(exc.args[0])) from None
    except roles.SchemaError as exc:
        raise OrchestratorError(str(exc)) from None


def lead(orch: Facade, project_name: str, adopt: str | None = None) -> Agent:
    """The project's own agent: works in the main checkout and may run sub-agents.

    `adopt`: the headless orchestrator asking; a lead created before it existed (no
    parent) becomes its child, so that the orchestrator is notified about it.
    """
    for agent in orch.store.list_agents(orch.project(project_name).name):
        if agent.role == LEAD_ROLE and agent.parent_id in (None, ORCHESTRATOR_ID):
            if adopt == ORCHESTRATOR_ID and agent.parent_id is None:
                orch.store.set_parent(agent.id, ORCHESTRATOR_ID)
                agent.parent_id = ORCHESTRATOR_ID
            return agent
    parent = ORCHESTRATOR_ID if orch.store.get_agent(ORCHESTRATOR_ID) else None
    return orch.create_agent(project_name, name="lead", role=LEAD_ROLE, parent_id=parent)


def orchestrator(orch: Facade) -> Agent:
    """The headless top-level orchestrator agent (the one the dashboard talks to).

    It belongs to no project, works in the orchestrator workspace and reaches every
    project through the MCP tools. `orch chat` is a separate, interactive session.
    """
    return orch.store.get_agent(ORCHESTRATOR_ID) or orch.store.add_agent(
        replace(orch._orchestrator_draft(), cwd=str(config.workspace_dir()))
    )


def _orchestrator_draft() -> Agent:
    """The orchestrator agent as it is created on first use, without creating anything
    (not stored; its workspace is made when it is created)."""
    role = roles.load(ORCHESTRATOR_ID)
    return Agent(
        id=ORCHESTRATOR_ID,
        project=None,
        name=ORCHESTRATOR_ID,
        role=ORCHESTRATOR_ID,
        provider=role.provider or "claude",
        cwd="",
    )


def _resolve_after(orch: Facade, after: list[str | int] | None) -> list[int]:
    """Dependencies may name a run id or an agent (meaning its latest run)."""
    run_ids = []
    for ref in after or []:
        if isinstance(ref, int) or str(ref).isdigit():
            if orch.store.get_run(int(ref)) is None:
                raise OrchestratorError(f"unknown run {ref}")
            run_ids.append(int(ref))
        else:
            agent = orch.agent(str(ref))
            timeline = AgentRuns.read(orch.store, agent.id)
            meta = timeline.meta
            # Not a waiting notification: it is deleted when it has nothing to report.
            # Not a compaction either: it is no work of the agent's.
            runs = [
                run
                for run in timeline.answers()
                if not (
                    run.status is RunStatus.WAITING
                    and run.id in meta
                    and meta[run.id].kind == NOTIFICATION
                )
            ]
            if runs:
                run_ids.append(runs[-1].id)
    return run_ids


def withdraw(orch: Facade, run_id: int, sender: Any = ANYONE) -> Run:
    """Withdraw a queued message or compaction that has not started: it never runs and
    leaves no run behind (runs waiting for it are skipped as for any vanished
    dependency); the agent's running turn and its other queued runs go on. `sender`:
    only a run that this agent queued. Returns the run as it was."""
    what = "compaction" if orch.store.meta(run_id).kind == COMPACT else "message"
    try:
        run = orch.store.withdraw_queued(run_id, sender)
    except ValueError as exc:
        raise OrchestratorError(f"cannot withdraw: {exc}") from None
    preview = " ".join(run.prompt.split())[:WITHDRAWN_PREVIEW_CHARS]
    orch.store.add_event(
        run.id, run.agent_id, "info", f"queued {what} {run.id} withdrawn: {preview}"
    )
    # Its runner sees the run gone at its next poll and exits (runner.wait_for_turn);
    # no signal: it may be in the middle of reconcile or a notification launch.
    # Not for the withdrawn run (it is gone), but the agent may now be settled.
    orch.run_ended(run.id, run.agent_id)
    return run


def replace_message(orch: Facade, run_id: int, message: str, sender: Any = ANYONE) -> Run:
    """Replace the text of a queued message that has not started; the agent gets the
    new text when its turn starts. `sender` as in `withdraw`."""
    if not message.strip():
        raise OrchestratorError("the new text is empty (withdraw the message instead)")
    try:
        return orch.store.replace_queued(run_id, message, sender)
    except ValueError as exc:
        raise OrchestratorError(f"cannot edit: {exc}") from None
