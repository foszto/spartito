"""The orchestrator facade. The CLI, the MCP server and the dashboard all go through here."""

from __future__ import annotations

import contextlib
import fcntl
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
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from . import config, git, notify, providers, roles, settings
from .models import Agent, Project, Run, RunStatus, Usage, context_now, running_total
from .store import (
    ANYONE,
    ARCHIVE_AUTO,
    ARCHIVE_MANUAL,
    COMPACT,
    MESSAGE,
    NOTIFICATION,
    NOTIFY_MODES,
    Store,
)

LEAD_ROLE = "lead"
ORCHESTRATOR_ID = "orchestrator"
STOP_GRACE_SECONDS = 8
QUEUE_PREVIEW_CHARS = 120
QUEUE_LISTED = 20  # waiting runs in the detail view's `queue`; `queued_runs` has the count
WAIT_POLL_SECONDS = 1.0
WITHDRAWN_PREVIEW_CHARS = 80  # of a withdrawn message, in the agent's info event
# The automatic archive check runs from the listings (dashboard poll, `orch ps`,
# list_agents), at most once per AUTO_ARCHIVE_EVERY seconds across all processes, and stops
# checking further agents once it has spent AUTO_ARCHIVE_BUDGET seconds (the rest: next time).
AUTO_ARCHIVE_EVERY = 300.0
AUTO_ARCHIVE_BUDGET = 5.0
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


class OrchestratorError(Exception):
    """A request that cannot be fulfilled; the message is meant for the caller."""


class NeedsConfirmation(OrchestratorError):
    """Archiving would hide work that is not merged: the message says what; repeat the
    request with confirmation to archive anyway."""


def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:32] or "agent"


def run_usage(usage: Usage | None) -> dict[str, Any]:
    """One run's own usage: tokens (short keys), cost and the model the CLI reported."""
    if usage is None:
        return {"tokens": None, "cost_usd": None, "model": None}
    return {"tokens": usage.tokens(), "cost_usd": usage.cost, "model": usage.model}


def context_view(record: Usage | None) -> dict[str, Any] | None:
    """How full the conversation is (see models.context_now): `tokens` of `window`,
    `percent`; after a compaction no request followed yet, `compacted` with the size
    unknown. None when nothing is known."""
    if record is None:
        return None
    tokens, window = record.context_tokens, record.context_window
    percent = round(100 * tokens / window) if tokens is not None and window else None
    return {"tokens": tokens, "window": window, "percent": percent, "compacted": tokens is None}


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
        if model:
            try:
                settings.check_model(model)
            except ValueError as exc:
                raise OrchestratorError(str(exc)) from None
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
                model=model or None,
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

    def compact(self, agent_id: str, instructions: str | None = None) -> Run:
        """Queue a turn that compacts the agent's conversation with the CLI's own means
        (Claude: `/compact [instructions]`), in order with its other turns. The session
        stays the same; the run's result is a short summary (`compacted 48k -> 1k tokens`)
        that is never delivered to a parent and wakes no one."""
        agent = self.agent(agent_id)
        refusal = self.compact_refusal(agent)
        if refusal:
            raise OrchestratorError(f"cannot compact {agent.id}: {refusal}")
        compact_prompt = providers.get(agent.provider).compact_prompt  # type: ignore[attr-defined]
        try:
            prompt = compact_prompt(instructions)
        except ValueError as exc:
            raise OrchestratorError(f"cannot compact {agent.id}: {exc}") from None
        run = self.store.add_run(agent.id, prompt, kind=COMPACT)
        self._launch(run)
        return run

    def compact_refusal(self, agent: Agent) -> str | None:
        """Why the agent's conversation cannot be compacted now; None when it can."""
        try:
            provider = providers.get(agent.provider)
        except KeyError as exc:
            return str(exc.args[0])
        if not hasattr(provider, "compact_prompt"):
            return f"the {agent.provider} provider has no way to compact a conversation"
        if not agent.session_id:
            return "it has no conversation yet"
        if getattr(provider, "compact_needs_size", False):
            record = context_now(
                self.store.runs_of(agent.id), self.store.usage_of(agent.id), agent.session_id
            )
            if record is None or not record.context_tokens:
                return "its context size is not known yet (it is after its next turn)"
        return None

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
            replace(self._orchestrator_draft(), cwd=str(config.workspace_dir()))
        )

    @staticmethod
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
        `context`: how full the conversation is (see context_view), null when unknown.
        With `detail` also `last_run.kind` (`message`, `notification` or `compact`, a
        compaction turn) and `compact_refusal`: why `compact` is not possible now (null: it
        is).
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
            "context": context_view(context_now(runs, usage, agent.session_id)),
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
            view["compact_refusal"] = self.compact_refusal(agent)
            view["model_override"] = agent.model
            view["effort_override"] = agent.effort
            if mark := self.store.archived(agent.project).get(agent.id):
                view["archived"] = mark
            meta = self.store.meta_of(agent.id)
            if shown:
                view["last_run"]["kind"] = meta[shown.id].kind if shown.id in meta else MESSAGE
                view["last_run"]["structured"] = shown.structured
                view["last_run"]["usage"] = run_usage(usage.get(shown.id))
                view["last_run"]["result_file"] = result_file(shown.id)
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
        """The agent's latest runs with their kind, own tokens, cost and reported model."""
        usage = self.store.usage_of(agent.id)
        meta = self.store.meta_of(agent.id)
        return [
            {
                "id": run.id,
                "kind": meta[run.id].kind if run.id in meta else MESSAGE,
                "status": run.status.value,
                "created_at": run.created_at,
                "started_at": run.started_at,
                "ended_at": run.ended_at,
                **run_usage(usage.get(run.id)),
            }
            for run in self.store.runs_of(agent.id)[-limit:]
        ]

    def overview(self, project: str | None = None, archived: bool = False) -> list[dict[str, Any]]:
        """The agents (of one project) as listings show them: the ones not archived, or
        with `archived` only the archived ones (with `archived_at` and `archived_how`).
        The default listing first runs the automatic archive check when it is due."""
        self.store.reconcile()
        if not archived:
            self.tidy()
        marks = self.store.archived(project)
        views = []
        for agent in self.store.list_agents(project):
            if (agent.id in marks) != archived:
                continue
            view = self.view(agent, result_limit=400)
            if archived:
                view["archived_at"] = marks[agent.id]["archived_at"]
                view["archived_how"] = marks[agent.id]["how"]
            views.append(view)
        return views

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

    def withdraw(self, run_id: int, sender: Any = ANYONE) -> Run:
        """Withdraw a queued message that has not started: it never runs and leaves no run
        behind (runs waiting for it are skipped as for any vanished dependency); the
        agent's running turn and its other queued runs go on. `sender`: only a run that
        this agent queued. Returns the run as it was."""
        try:
            run = self.store.withdraw_queued(run_id, sender)
        except ValueError as exc:
            raise OrchestratorError(f"cannot withdraw: {exc}") from None
        preview = " ".join(run.prompt.split())[:WITHDRAWN_PREVIEW_CHARS]
        self.store.add_event(
            run.id, run.agent_id, "info", f"queued message {run.id} withdrawn: {preview}"
        )
        # Its runner sees the run gone at its next poll and exits (runner.wait_for_turn);
        # no signal: it may be in the middle of reconcile or a notification launch.
        # Not for the withdrawn run (it is gone), but the agent may now be settled.
        self.run_ended(run.id, run.agent_id)
        return run

    def replace_message(self, run_id: int, message: str, sender: Any = ANYONE) -> Run:
        """Replace the text of a queued message that has not started; the agent gets the
        new text when its turn starts. `sender` as in `withdraw`."""
        if not message.strip():
            raise OrchestratorError("the new text is empty (withdraw the message instead)")
        try:
            return self.store.replace_queued(run_id, message, sender)
        except ValueError as exc:
            raise OrchestratorError(f"cannot edit: {exc}") from None

    @staticmethod
    def _kill(run: Run, sig: int) -> None:
        if run.pid:
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(run.pid, sig)

    # -- archive ----------------------------------------------------------

    def is_lead(self, agent: Agent) -> bool:
        """A project lead (or any role that may run sub-agents); an unreadable role counts."""
        if agent.role == LEAD_ROLE:
            return True
        project = self.store.get_project(agent.project) if agent.project else None
        try:
            return roles.load(agent.role, project.path if project else None).lead
        except Exception:
            return True

    def unsaved(self, agent: Agent, deadline: float | None = None) -> str | None:
        """What archiving would hide that is not safe: commits not merged into the
        project's base branch (on its own branch, or at its worktree's HEAD: detached or on
        another branch), uncommitted changes in its own worktree. None: an agent without a
        worktree of its own, or merged and clean. Never raises; a git error (a timeout, or
        `deadline` passed: see git.unsaved_work) is reported as not safe."""
        if not agent.owns_worktree or agent.project is None:
            return None
        project = self.store.get_project(agent.project)
        if project is None:
            return "its project is no longer registered"
        try:
            commits, changes = git.unsaved_work(
                project.path, agent.cwd, agent.branch, project.base_branch, deadline
            )
        except (git.GitError, OSError, ValueError) as exc:
            return f"its work could not be checked ({exc})"
        parts = []
        if commits:
            plural = "commit" if commits == 1 else "commits"
            parts.append(
                f"{commits} {plural} not merged into {project.base_branch}"
                f" (on {agent.branch} or checked out in its worktree)"
            )
        if changes:
            plural = "change" if changes == 1 else "changes"
            parts.append(f"{changes} uncommitted {plural} in {agent.cwd}")
        return " and ".join(parts) or None

    def _held(self, agent: Agent, undelivered: bool) -> Callable[[Any], str | None]:
        """A check for Store.archive, inside its transaction: why the agent must not be
        archived now, or None. An agent owning a worktree in which another agent works is
        held; with `undelivered`, also one whose parent has not received a result of it
        yet (notify: deliverable runs; delivered by a notification or wait_for_agents)."""

        def held(conn: Any) -> str | None:
            if agent.owns_worktree:
                row = conn.execute(
                    "SELECT a.id FROM agents a JOIN runs r ON r.agent_id = a.id"
                    " WHERE a.cwd = ? AND a.id != ? AND r.status IN (?, ?) LIMIT 1",
                    (agent.cwd, agent.id, RunStatus.WAITING.value, RunStatus.RUNNING.value),
                ).fetchone()
                if row:
                    return f"{row['id']} works in its worktree"
            if undelivered and agent.parent_id:
                for kid in notify.children(conn, agent.parent_id):
                    if kid.id == agent.id and kid.deliverable:
                        return f"{agent.parent_id} has not received its result yet"
            return None

        return held

    def _check_held(self, agent: Agent, undelivered: bool) -> str | None:
        with self.store.connection() as conn:
            return self._held(agent, undelivered)(conn)

    def _archivable(self, agent: Agent) -> None:
        if agent.project is None:
            raise OrchestratorError("the orchestrator is not archived")
        if self.store.archived(agent.project).get(agent.id):
            raise OrchestratorError(f"{agent.id} is archived already")

    def archive(self, agent_id: str, confirm: bool = False) -> dict[str, Any]:
        """Archive an agent by hand: hidden from the default listings, everything kept
        (record, runs, logs, branch, worktree); a message to it or `restore` brings it
        back. Refused while it or a descendant has a queued or running turn. Work that is
        not merged needs `confirm` (else NeedsConfirmation says what it is)."""
        agent = self.agent(agent_id)
        self._archivable(agent)
        busy = f"{agent.id} or one of its agents has a queued or running turn"
        if self.store.active_runs(agent.id) or self.store.active_descendants(agent.id):
            raise OrchestratorError(busy)
        if held := self._check_held(agent, undelivered=False):
            raise OrchestratorError(f"{agent.id} cannot be archived now: {held}")
        unsaved = self.unsaved(agent)
        if unsaved and not confirm:
            raise NeedsConfirmation(f"{agent.id} has {unsaved}")
        refused = self.store.archive(agent.id, ARCHIVE_MANUAL, held=self._held(agent, False))
        if refused:
            raise OrchestratorError(busy if refused is True else f"{agent.id}: {refused}")
        return {"archived": agent.id, "unsaved": unsaved}

    def restore(self, agent_id: str) -> dict[str, Any]:
        agent = self.agent(agent_id)
        if not self.store.restore(agent.id):
            raise OrchestratorError(f"{agent.id} is not archived")
        return {"restored": agent.id}

    def archive_safe(self, project_name: str | None = None) -> dict[str, Any]:
        """Archive by hand every agent (of one project) whose work is safe and which is not
        working: no worktree of its own, or its branch merged and its worktree clean.
        Project leads and the orchestrator are left alone, and so is an agent whose parent
        has not received its result yet or in whose worktree another agent works. Returns
        the ids archived and, for the others that are idle, why they were kept."""
        if project_name:
            self.project(project_name)
        archived, kept = [], {}
        marks = self.store.archived(project_name)
        for agent in self.store.list_agents(project_name):
            if agent.project is None or agent.id in marks or self.is_lead(agent):
                continue
            if self.store.active_runs(agent.id) or self.store.active_descendants(agent.id):
                continue
            reason = self._check_held(agent, undelivered=True) or self.unsaved(agent)
            if not reason:
                refused = self.store.archive(agent.id, ARCHIVE_MANUAL, held=self._held(agent, True))
                reason = refused if isinstance(refused, str) else None
                if refused is None:
                    archived.append(agent.id)
            if reason:
                kept[agent.id] = reason
        return {"archived": archived, "kept": kept}

    def auto_archive(self) -> list[str]:
        """Archive every agent that is idle and safe for long enough (see `tidy`): no turn
        queued or running in it or below it (or in its worktree), last activity older than
        the project's `archive_after_hours`, its work safe (`unsaved`), its results
        received by its parent, not a lead, not the orchestrator. The whole pass, git
        included, ends within AUTO_ARCHIVE_BUDGET (plus git.KILL_GRACE when a git call had
        to be killed); agents not checked by then are checked next time. Returns the ids
        archived."""
        deadline = time.monotonic() + AUTO_ARCHIVE_BUDGET
        marks = self.store.archived()
        activity = self.store.last_activity()
        hours: dict[str, float] = {}
        candidates = []
        for agent in self.store.list_agents():
            if agent.project is None or agent.id in marks:
                continue
            if agent.project not in hours:
                hours[agent.project] = settings.archive_after(self.store, agent.project)
            if hours[agent.project] <= 0:
                continue
            cutoff = (datetime.now(UTC) - timedelta(hours=hours[agent.project])).isoformat(
                timespec="seconds"
            )
            if (activity.get(agent.id) or "") <= cutoff:
                candidates.append((activity.get(agent.id) or "", agent, cutoff))
        archived = []
        for _, agent, cutoff in sorted(candidates, key=lambda item: item[0]):
            if time.monotonic() >= deadline:
                break
            if self.is_lead(agent) or self.store.active_runs(agent.id):
                continue
            if self.store.active_descendants(agent.id) or self._check_held(agent, True):
                continue
            if self.unsaved(agent, deadline) or time.monotonic() >= deadline:
                continue
            held = self._held(agent, undelivered=True)
            if self.store.archive(agent.id, ARCHIVE_AUTO, idle_since=cutoff, held=held) is None:
                archived.append(agent.id)
        return archived

    def tidy(self) -> list[str]:
        """Run `auto_archive` if no process ran it in the last AUTO_ARCHIVE_EVERY seconds
        (a stamp file under ORCH_HOME, taken under a lock, so concurrent callers skip).
        Never raises; returns the ids archived."""
        try:
            with open(config.home() / "auto-archive.stamp", "a+") as stamp:
                fcntl.flock(stamp, fcntl.LOCK_EX | fcntl.LOCK_NB)  # held: another one checks
                stamp.seek(0)
                try:
                    elapsed = time.time() - float(stamp.read().strip() or 0)
                except ValueError:  # unreadable: check now and write a fresh one
                    elapsed = AUTO_ARCHIVE_EVERY
                if abs(elapsed) < AUTO_ARCHIVE_EVERY:  # a clock set back far: check
                    return []
                stamp.seek(0)
                stamp.truncate()
                stamp.write(f"{time.time():.3f}\n")
        except OSError:  # BlockingIOError too: another process is checking right now
            return []
        try:
            return self.auto_archive()
        except Exception as exc:
            print(f"automatic archiving failed: {exc!r}", file=sys.stderr)
            return []

    def remove(self, agent_id: str, delete_branch: bool = False) -> None:
        """Delete an archived agent together with the worktree it owns (stopped first).
        Only archived agents are deleted: archive one first."""
        agent = self.agent(agent_id)
        if not self.store.archived(agent.project).get(agent.id):
            raise OrchestratorError(f"{agent.id} is not archived; only archived agents are deleted")
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
