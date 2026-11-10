"""The orchestrator facade. The CLI, the MCP server and the dashboard all go through here."""

from __future__ import annotations

import contextlib
import json
import os
import re
import signal
import subprocess
import sys
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any

from . import config, git, notify, providers, roles, settings
from .models import Agent, Project, Run, RunStatus, Usage, running_total
from .store import MESSAGE, NOTIFICATION, NOTIFY_MODES, Store

LEAD_ROLE = "lead"
ORCHESTRATOR_ID = "orchestrator"
STOP_GRACE_SECONDS = 8
QUEUE_PREVIEW_CHARS = 120
QUEUE_LISTED = 20  # waiting runs in the detail view's `queue`; `queued_runs` has the count
WAIT_POLL_SECONDS = 1.0
# What starts a fresh prompt cache when it changes in a resumed session (the next turn
# re-reads the whole conversation uncached). Checked with real turns: Claude 2.x re-wrote
# the cache after a model switch on --resume; codex-cli 0.155.1 read 0 cached tokens after
# `-m` changed on `exec resume` but kept the cache (12k of 14k cached) when only
# model_reasoning_effort did. Claude's effort is assumed to reset it (thinking settings
# are part of the cached request).
CACHE_KEYS = {"codex": ("model",)}
CACHE_KEYS_DEFAULT = ("model", "effort")


class OrchestratorError(Exception):
    """A request that cannot be fulfilled; the message is meant for the caller."""


def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:32] or "agent"


def run_usage(usage: Usage | None) -> dict[str, Any]:
    """One run's own usage: tokens (short keys), cost and the model the CLI reported."""
    if usage is None:
        return {"tokens": None, "cost_usd": None, "model": None}
    return {"tokens": usage.tokens(), "cost_usd": usage.cost, "model": usage.model}


def result_file(run_id: int) -> str | None:
    """Where the runner wrote the run's full final answer, if it did."""
    path = config.result_file(run_id)
    return str(path) if path.is_file() else None


def agent_state(runs: list[Run]) -> tuple[str, list[Run], Run | None]:
    """An agent's status from its runs, its queued/running runs and its latest finished run."""
    active = [run for run in runs if run.status.active]
    finished = [run for run in runs if not run.status.active]
    if any(run.status is RunStatus.RUNNING for run in active):
        status = "running"
    elif active:
        status = "waiting"
    else:
        status = runs[-1].status.value if runs else "new"
    return status, active, finished[-1] if finished else None


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
        agent = self.agent(agent_id)
        self._check_notify(notify)
        if output_schema is None and role_schema:
            project = self.store.get_project(agent.project) if agent.project else None
            output_schema = self.role_schema(agent.role, project.path if project else None)
        run = self.store.add_run(
            agent.id,
            message,
            self._resolve_after(after),
            output_schema,
            kind=MESSAGE,
            sender=sender,
            notify=notify,
            cause_run=self._cause(sender),
        )
        self._launch(run)
        return run

    def _cause(self, sender: str | None) -> int | None:
        running = self.store.running_run(sender) if sender else None
        return running.id if running else None

    @staticmethod
    def _check_notify(notify: str) -> None:
        if notify not in NOTIFY_MODES:
            raise OrchestratorError(f"notify must be one of: {', '.join(NOTIFY_MODES)}")

    def spawn(self, project_name: str, task: str, **options: Any) -> tuple[Agent, Run]:
        after = options.pop("after", None)
        output_schema = options.pop("output_schema", None)
        sender = options.pop("sender", None)
        notify_mode = options.pop("notify", "each")
        self._check_notify(notify_mode)
        if options.pop("role_schema", False) and output_schema is None:
            # Resolved before the agent exists, so a broken schema leaves no agent behind.
            path = self.project(project_name).path
            output_schema = self.role_schema(options.get("role") or "worker", path)
        agent = self.create_agent(project_name, **options)
        return agent, self.send(
            agent.id, task, after, output_schema, sender=sender, notify=notify_mode
        )

    @staticmethod
    def role_schema(role: str, project_path: str | None) -> dict[str, Any] | None:
        """The role's default output schema for agent-to-agent handoff (None: free text)."""
        try:
            return roles.output_schema(roles.load(role, project_path), project_path)
        except KeyError as exc:
            raise OrchestratorError(str(exc.args[0])) from None
        except roles.SchemaError as exc:
            raise OrchestratorError(str(exc)) from None

    def lead(self, project_name: str, adopt: str | None = None) -> Agent:
        """The project's own agent: works in the main checkout and may run sub-agents.

        `adopt`: the headless orchestrator asking; a lead created before it existed (no
        parent) becomes its child, so that the orchestrator is notified about it.
        """
        for agent in self.store.list_agents(self.project(project_name).name):
            if agent.role == LEAD_ROLE and agent.parent_id in (None, ORCHESTRATOR_ID):
                if adopt == ORCHESTRATOR_ID and agent.parent_id is None:
                    self.store.set_parent(agent.id, ORCHESTRATOR_ID)
                    agent.parent_id = ORCHESTRATOR_ID
                return agent
        parent = ORCHESTRATOR_ID if self.store.get_agent(ORCHESTRATOR_ID) else None
        return self.create_agent(project_name, name="lead", role=LEAD_ROLE, parent_id=parent)

    def orchestrator(self) -> Agent:
        """The headless top-level orchestrator agent (the one the dashboard talks to).

        It belongs to no project, works in the orchestrator workspace and reaches every
        project through the MCP tools. `orch chat` is a separate, interactive session.
        """
        return self.store.get_agent(ORCHESTRATOR_ID) or self.store.add_agent(
            self._orchestrator_draft()
        )

    @staticmethod
    def _orchestrator_draft() -> Agent:
        """The orchestrator agent as it is created on first use (not stored)."""
        role = roles.load(ORCHESTRATOR_ID)
        return Agent(
            id=ORCHESTRATOR_ID,
            project=None,
            name=ORCHESTRATOR_ID,
            role=ORCHESTRATOR_ID,
            provider=role.provider or "claude",
            cwd=str(config.workspace_dir()),
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
                agent = self.agent(str(ref))
                meta = self.store.meta_of(agent.id)
                # Not a waiting notification: it is deleted when it has nothing to report.
                runs = [
                    run
                    for run in self.store.runs_of(agent.id)
                    if not (
                        run.status is RunStatus.WAITING
                        and run.id in meta
                        and meta[run.id].kind == NOTIFICATION
                    )
                ]
                if runs:
                    run_ids.append(runs[-1].id)
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

    def run_ended(self, run_id: int, agent_id: str, recheck: bool = False) -> None:
        """Every path that ends a run calls this once it has ended (or was deleted): it
        queues the notifications that are due (see notify.run_ended). Never raises: the
        caller is ending a run and must not fail because of this."""
        try:
            notify.run_ended(self.store, run_id, agent_id, self._launch, recheck)
        except Exception as exc:
            print(f"notifying the parents of {agent_id} failed: {exc!r}", file=sys.stderr)

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
        applies from the next one); `cache_keys`: which of the two start a fresh prompt cache
        when changed in an existing session. The orchestrator is shown before its first use
        too (`exists` false)."""
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
            "cache_keys": list(CACHE_KEYS.get(agent.provider, CACHE_KEYS_DEFAULT)),
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

    # -- observation ------------------------------------------------------

    def view(
        self, agent: Agent, result_limit: int | None = None, detail: bool = False
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
        """
        runs = self.store.runs_of(agent.id)
        usage = self.store.usage_of(agent.id)
        effective = self.effective(agent)
        status, active, shown = agent_state(runs)
        result = shown.result if shown else None
        if shown and shown.structured is not None and not detail:
            # The answer once: structured output as compact JSON instead of the text.
            result = json.dumps(shown.structured, ensure_ascii=False, separators=(",", ":"))
        if result and result_limit and len(result) > result_limit:
            result = result[:result_limit] + "… [truncated]"
        totals = running_total(runs, usage)
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
            "children_running": self.store.active_descendants(agent.id),
            "runs": len(runs),
            "cost_usd": totals.cost or 0,
            "tokens": totals.tokens(),
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
            view["model_override"] = agent.model
            view["effort_override"] = agent.effort
            if shown:
                view["last_run"]["structured"] = shown.structured
                view["last_run"]["usage"] = run_usage(usage.get(shown.id))
                view["last_run"]["result_file"] = result_file(shown.id)
            meta = self.store.meta_of(agent.id)
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

    def run_history(self, agent: Agent, limit: int = 10) -> list[dict[str, Any]]:
        """The agent's latest runs with their own tokens, cost and reported model."""
        usage = self.store.usage_of(agent.id)
        return [
            {
                "id": run.id,
                "status": run.status.value,
                "created_at": run.created_at,
                "started_at": run.started_at,
                "ended_at": run.ended_at,
                **run_usage(usage.get(run.id)),
            }
            for run in self.store.runs_of(agent.id)[-limit:]
        ]

    def overview(self, project: str | None = None) -> list[dict[str, Any]]:
        self.store.reconcile()
        return [self.view(agent, result_limit=400) for agent in self.store.list_agents(project)]

    def wait_idle(
        self,
        agent_ids: list[str],
        timeout: float = 600,
        interrupt: Callable[[], bool] | None = None,
    ) -> bool:
        """Block until the agents are settled: neither they nor any of their descendants
        has a queued or running turn, or an ended one whose parents were not checked yet
        for a notification (True); or until timeout, or until `interrupt()` is true
        (False). Only reads (and reconciles dead runners): waiting never queues a turn."""
        agents = [self.agent(agent_id) for agent_id in agent_ids]
        deadline = time.monotonic() + timeout
        while True:
            self.store.reconcile()
            idle = all(self.settled(agent.id) for agent in agents)
            if idle or time.monotonic() >= deadline or (interrupt and interrupt()):
                return idle
            time.sleep(WAIT_POLL_SECONDS)

    def settled(self, agent_id: str) -> bool:
        return notify.settled(self.store, agent_id)

    def wait(self, agent_ids: list[str], timeout: float = 600) -> tuple[bool, list[dict[str, Any]]]:
        """Block like wait_idle, then return whether they are idle and their detail views
        (the last run's full text in `result`, structured output in `structured`)."""
        idle = self.wait_idle(agent_ids, timeout)
        return idle, [self.view(self.agent(agent_id), detail=True) for agent_id in agent_ids]

    def changes(
        self, agent_id: str, max_commits: int | None = None, max_status: int | None = None
    ) -> str:
        """Commits and uncommitted changes; `max_commits`/`max_status` cap the lines."""
        agent = self.agent(agent_id)
        if agent.project is None:
            return ""
        if not Path(agent.cwd).exists():
            return "workspace not created yet"
        base = agent.base_ref or self.project(agent.project).base_branch
        return git.summary(agent.cwd, base, max_commits, max_status)

    # -- control ----------------------------------------------------------

    def stop(self, agent_id: str) -> int:
        """Stop every queued and running turn of an agent. Returns how many were stopped."""
        return len(self.stop_runs(agent_id))

    def stop_runs(self, agent_id: str) -> list[int]:
        """Stop every queued and running turn of an agent; returns their ids."""
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
            if run.started_at is None and self.store.meta(run.id).kind == NOTIFICATION:
                self.store.delete_run(run.id)  # never had a turn: leave no trace
            else:
                self.store.finish_run(run.id, RunStatus.STOPPED, error="stopped")
            self.run_ended(run.id, agent.id)  # a deliberate stop: no recheck of the agent
        return [run.id for run in active]

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
