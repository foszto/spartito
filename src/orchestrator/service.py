"""The orchestrator facade. The CLI, the MCP server and the dashboard all go through here."""

from __future__ import annotations

import contextlib
import os
import re
import signal
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from . import config, git, providers, roles, settings
from .models import Agent, Project, Run, RunStatus
from .store import Store

LEAD_ROLE = "lead"
ORCHESTRATOR_ID = "orchestrator"
STOP_GRACE_SECONDS = 8


class OrchestratorError(Exception):
    """A request that cannot be fulfilled; the message is meant for the caller."""


def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:32] or "agent"


class Orchestrator:
    def __init__(self, store: Store | None = None) -> None:
        self.store = store or Store()

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
            raise OrchestratorError(f"project '{name}' still has agents; remove them first")
        self.store.delete_project(name)

    # -- agents -----------------------------------------------------------

    def agent(self, agent_id: str) -> Agent:
        agent = self.store.get_agent(agent_id)
        if agent is None:
            raise OrchestratorError(f"unknown agent '{agent_id}'")
        return agent

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
    ) -> Agent:
        """Register an agent. Its worktree is created lazily, when its first run starts."""
        project = self.project(project_name)
        try:
            role_def = roles.load(role, project.path)
            provider_name = provider or role_def.provider or project.default_provider
            providers.get(provider_name)
        except KeyError as exc:
            raise OrchestratorError(str(exc.args[0])) from None
        if effort and effort not in providers.get(provider_name).efforts:
            allowed = ", ".join(providers.get(provider_name).efforts)
            raise OrchestratorError(f"{provider_name} has no effort '{effort}' (use: {allowed})")
        if parent_id:
            self.agent(parent_id)

        agent_id = f"{slug(name or role)}-{uuid.uuid4().hex[:4]}"
        branch = base_ref = None
        if share_with:
            cwd = self.agent(share_with).cwd
        elif role_def.worktree if worktree is None else worktree:
            branch = f"orch/{agent_id}"
            base_agent = self.store.get_agent(base) if base else None
            base_ref = (base_agent.branch if base_agent else base) or project.base_branch
            cwd = str(config.worktrees_dir(project.name) / agent_id)
        else:
            cwd = project.path
        return self.store.add_agent(
            Agent(
                id=agent_id,
                project=project.name,
                name=name or role,
                role=role,
                provider=provider_name,
                cwd=cwd,
                parent_id=parent_id,
                model=model,
                effort=effort,
                branch=branch,
                base_ref=base_ref,
                permission=permission,
            )
        )

    def send(
        self,
        agent_id: str,
        message: str,
        after: list[str | int] | None = None,
        output_schema: dict[str, Any] | None = None,
    ) -> Run:
        """Queue a turn for an agent and start its runner. Returns immediately."""
        agent = self.agent(agent_id)
        run = self.store.add_run(agent.id, message, self._resolve_after(after), output_schema)
        self._launch(run)
        return run

    def spawn(self, project_name: str, task: str, **options: Any) -> tuple[Agent, Run]:
        after = options.pop("after", None)
        output_schema = options.pop("output_schema", None)
        agent = self.create_agent(project_name, **options)
        return agent, self.send(agent.id, task, after, output_schema)

    def lead(self, project_name: str) -> Agent:
        """The project's own agent: works in the main checkout and may run sub-agents."""
        for agent in self.store.list_agents(self.project(project_name).name):
            if agent.role == LEAD_ROLE and agent.parent_id in (None, ORCHESTRATOR_ID):
                return agent
        parent = ORCHESTRATOR_ID if self.store.get_agent(ORCHESTRATOR_ID) else None
        return self.create_agent(project_name, name="lead", role=LEAD_ROLE, parent_id=parent)

    def orchestrator(self) -> Agent:
        """The headless top-level orchestrator agent (the one the dashboard talks to).

        It belongs to no project, works in the orchestrator workspace and reaches every
        project through the MCP tools. `orch chat` is a separate, interactive session.
        """
        agent = self.store.get_agent(ORCHESTRATOR_ID)
        if agent:
            return agent
        role = roles.load(ORCHESTRATOR_ID)
        return self.store.add_agent(
            Agent(
                id=ORCHESTRATOR_ID,
                project=None,
                name=ORCHESTRATOR_ID,
                role=ORCHESTRATOR_ID,
                provider=role.provider or "claude",
                cwd=str(config.workspace_dir()),
            )
        )

    def _resolve_after(self, after: list[str | int] | None) -> list[int]:
        """Dependencies may name a run id or an agent (meaning its latest run)."""
        run_ids = []
        for ref in after or []:
            if isinstance(ref, int) or str(ref).isdigit():
                if self.store.get_run(int(ref)) is None:
                    raise OrchestratorError(f"unknown run {ref}")
                run_ids.append(int(ref))
            else:
                last = self.store.last_run(self.agent(str(ref)).id)
                if last:
                    run_ids.append(last.id)
        return run_ids

    def _launch(self, run: Run) -> None:
        log = open(config.logs_dir() / f"run-{run.id}.runner.log", "w")  # noqa: SIM115
        env = {**os.environ, "ORCH_HOME": str(config.home())}
        proc = subprocess.Popen(
            [sys.executable, "-m", "orchestrator.runner", str(run.id)],
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=log,
            env=env,
            start_new_session=True,  # survives the caller; also makes a killable group
        )
        log.close()
        self.store.update_run(run.id, pid=proc.pid)
        # Reap the runner if this process outlives it, so a dead runner is not a zombie
        # that still looks alive to Store.reconcile().
        threading.Thread(target=proc.wait, daemon=True).start()

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
        self.store.set_settings(self.project(project).name if project else settings.GLOBAL, cleaned)
        return cleaned

    def effective(self, agent: Agent) -> dict[str, str | None]:
        """Model and effort the agent's next turn will use."""
        project = self.store.get_project(agent.project) if agent.project else None
        role = roles.load(agent.role, project.path if project else None)
        return settings.resolve(self.store, agent, role)

    # -- observation ------------------------------------------------------

    def view(self, agent: Agent, result_limit: int | None = None) -> dict[str, Any]:
        runs = self.store.runs_of(agent.id)
        active = [run for run in runs if run.status.active]
        last = runs[-1] if runs else None
        finished = [run for run in runs if not run.status.active]
        if any(run.status is RunStatus.RUNNING for run in active):
            status = "running"
        elif active:
            status = "waiting"
        else:
            status = last.status.value if last else "new"
        shown = finished[-1] if finished else None
        result = shown.result if shown else None
        if result and result_limit and len(result) > result_limit:
            result = result[:result_limit] + "… [truncated; use agent_log for more]"
        return {
            "id": agent.id,
            "project": agent.project,
            "name": agent.name,
            "role": agent.role,
            "provider": agent.provider,
            "model": agent.model,
            "effort": agent.effort,
            "parent_id": agent.parent_id,
            "status": status,
            "branch": agent.branch,
            "cwd": agent.cwd,
            "session_id": agent.session_id,
            "queued_runs": len(active),
            "runs": len(runs),
            "cost_usd": round(sum(run.cost_usd or 0 for run in runs), 4),
            "last_run": shown
            and {
                "id": shown.id,
                "status": shown.status.value,
                "result": result,
                "structured": shown.structured,
                "error": shown.error,
                "started_at": shown.started_at,
                "ended_at": shown.ended_at,
            },
        }

    def overview(self, project: str | None = None) -> list[dict[str, Any]]:
        self.store.reconcile()
        return [self.view(agent, result_limit=400) for agent in self.store.list_agents(project)]

    def wait(self, agent_ids: list[str], timeout: float = 600) -> tuple[bool, list[dict[str, Any]]]:
        """Block until none of the agents has a queued or running turn, or until timeout."""
        agents = [self.agent(agent_id) for agent_id in agent_ids]
        deadline = time.monotonic() + timeout
        while True:
            self.store.reconcile()
            idle = not any(self.store.active_runs(agent.id) for agent in agents)
            if idle or time.monotonic() >= deadline:
                return idle, [self.view(agent) for agent in agents]
            time.sleep(1)

    def changes(self, agent_id: str) -> str:
        agent = self.agent(agent_id)
        if agent.project is None:
            return ""
        if not Path(agent.cwd).exists():
            return "workspace not created yet"
        return git.summary(agent.cwd, agent.base_ref or self.project(agent.project).base_branch)

    # -- control ----------------------------------------------------------

    def stop(self, agent_id: str) -> int:
        """Stop every queued and running turn of an agent. Returns how many were stopped."""
        agent = self.agent(agent_id)
        # Newest first, so a queued turn cannot start while the running one is going down.
        active = sorted(self.store.active_runs(agent.id), key=lambda run: -run.id)
        for run in active:
            self._kill(run, signal.SIGTERM)
        deadline = time.monotonic() + STOP_GRACE_SECONDS
        while self.store.active_runs(agent.id) and time.monotonic() < deadline:
            time.sleep(0.2)
        for run in self.store.active_runs(agent.id):
            self._kill(run, signal.SIGKILL)
            self.store.finish_run(run.id, RunStatus.STOPPED, error="stopped")
        return len(active)

    @staticmethod
    def _kill(run: Run, sig: int) -> None:
        if run.pid:
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(run.pid, sig)

    def remove(self, agent_id: str, delete_branch: bool = False) -> None:
        """Stop an agent and delete it together with the worktree it owns."""
        agent = self.agent(agent_id)
        self.stop(agent.id)
        if agent.owns_worktree:
            project = self.project(agent.project)
            sharing = [
                other.id
                for other in self.store.list_agents(agent.project)
                if other.id != agent.id and other.cwd == agent.cwd
            ]
            if sharing:
                raise OrchestratorError(
                    f"worktree is shared with {', '.join(sharing)}; remove those agents first"
                )
            try:
                git.remove_worktree(
                    project.path, agent.cwd, agent.branch if delete_branch else None
                )
            except git.GitError as exc:
                raise OrchestratorError(str(exc)) from None
        self.store.delete_agent(agent.id)
