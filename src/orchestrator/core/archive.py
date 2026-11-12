"""Archiving agents: by hand (one, or all whose work is safe), restoring, deleting an
archived one, and the automatic archive check of idle agents (budget and stamp)."""

from __future__ import annotations

import fcntl
import sys
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

from .. import config, decisions, git, notify, roles, settings
from ..errors import NeedsConfirmation, OrchestratorError
from ..models import Agent, Project, RunStatus
from ..store import ARCHIVE_AUTO, ARCHIVE_MANUAL, Store
from .dispatch import LEAD_ROLE


class Facade(Protocol):
    """What these functions use of the facade (they are its methods)."""

    store: Store

    def project(self, name: str) -> Project: ...

    def agent(self, agent_id: str) -> Agent: ...

    def is_lead(self, agent: Agent) -> bool: ...

    def unsaved(self, agent: Agent, deadline: float | None = None) -> str | None: ...

    def _held(self, agent: Agent, undelivered: bool) -> Callable[[Any], str | None]: ...

    def _check_held(self, agent: Agent, undelivered: bool) -> str | None: ...

    def _archivable(self, agent: Agent) -> None: ...

    def auto_archive(self) -> list[str]: ...

    def stop(self, agent_id: str) -> int: ...


# The automatic archive check runs from the listings (dashboard poll, `spartito ps`,
# list_agents), at most once per AUTO_ARCHIVE_EVERY seconds across all processes, and stops
# checking further agents once it has spent AUTO_ARCHIVE_BUDGET seconds (the rest: next time).
AUTO_ARCHIVE_EVERY = 300.0
AUTO_ARCHIVE_BUDGET = 5.0


def is_lead(orch: Facade, agent: Agent) -> bool:
    """A project lead (or any role that may run sub-agents); an unreadable role counts."""
    if agent.role == LEAD_ROLE:
        return True
    project = orch.store.get_project(agent.project) if agent.project else None
    try:
        return roles.load(agent.role, project.path if project else None).lead
    except Exception:
        return True


def unsaved(orch: Facade, agent: Agent, deadline: float | None = None) -> str | None:
    """What archiving would hide that is not safe: commits not merged into the
    project's base branch (on its own branch, or at its worktree's HEAD: detached or on
    another branch), uncommitted changes in its own worktree. None: an agent without a
    worktree of its own, or merged and clean. Never raises; a git error (a timeout, or
    `deadline` passed: see git.unsaved_work) is reported as not safe."""
    if not agent.owns_worktree or agent.project is None:
        return None
    project = orch.store.get_project(agent.project)
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


def _held(orch: Facade, agent: Agent, undelivered: bool) -> Callable[[Any], str | None]:
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


def _check_held(orch: Facade, agent: Agent, undelivered: bool) -> str | None:
    with orch.store.connection() as conn:
        return orch._held(agent, undelivered)(conn)


def _archivable(orch: Facade, agent: Agent) -> None:
    if agent.project is None:
        raise OrchestratorError("the orchestrator is not archived")
    if orch.store.archived(agent.project).get(agent.id):
        raise OrchestratorError(f"{agent.id} is archived already")


def archive(orch: Facade, agent_id: str, confirm: bool = False) -> dict[str, Any]:
    """Archive an agent by hand: hidden from the default listings, everything kept
    (record, runs, logs, branch, worktree); a message to it or `restore` brings it
    back. Refused while it or a descendant has a queued or running turn. Work that is
    not merged needs `confirm` (else NeedsConfirmation says what it is)."""
    agent = orch.agent(agent_id)
    orch._archivable(agent)
    busy = f"{agent.id} or one of its agents has a queued or running turn"
    if orch.store.active_runs(agent.id) or orch.store.active_descendants(agent.id):
        raise OrchestratorError(busy)
    if held := orch._check_held(agent, undelivered=False):
        raise OrchestratorError(f"{agent.id} cannot be archived now: {held}")
    unsaved = orch.unsaved(agent)
    if unsaved and not confirm:
        raise NeedsConfirmation(f"{agent.id} has {unsaved}")
    refused = orch.store.archive(agent.id, ARCHIVE_MANUAL, held=orch._held(agent, False))
    if refused:
        raise OrchestratorError(busy if refused is True else f"{agent.id}: {refused}")
    return {"archived": agent.id, "unsaved": unsaved}


def restore(orch: Facade, agent_id: str) -> dict[str, Any]:
    agent = orch.agent(agent_id)
    if not orch.store.restore(agent.id):
        raise OrchestratorError(f"{agent.id} is not archived")
    return {"restored": agent.id}


def archive_safe(orch: Facade, project_name: str | None = None) -> dict[str, Any]:
    """Archive by hand every agent (of one project) whose work is safe and which is not
    working: no worktree of its own, or its branch merged and its worktree clean.
    Project leads and the orchestrator are left alone, and so is an agent whose parent
    has not received its result yet or in whose worktree another agent works. Returns
    the ids archived and, for the others that are idle, why they were kept."""
    if project_name:
        orch.project(project_name)
    archived, kept = [], {}
    marks = orch.store.archived(project_name)
    for agent in orch.store.list_agents(project_name):
        if agent.project is None or agent.id in marks or orch.is_lead(agent):
            continue
        if orch.store.active_runs(agent.id) or orch.store.active_descendants(agent.id):
            continue
        reason = orch._check_held(agent, undelivered=True) or orch.unsaved(agent)
        if not reason:
            refused = orch.store.archive(agent.id, ARCHIVE_MANUAL, held=orch._held(agent, True))
            reason = refused if isinstance(refused, str) else None
            if refused is None:
                archived.append(agent.id)
        if reason:
            kept[agent.id] = reason
    return {"archived": archived, "kept": kept}


def auto_archive(orch: Facade) -> list[str]:
    """Archive every agent that is idle and safe for long enough (see `tidy`): no turn
    queued or running in it or below it (or in its worktree), last activity older than
    the project's `archive_after_hours`, its work safe (`unsaved`), its results
    received by its parent, not a lead, not the orchestrator. The whole pass, git
    included, ends within AUTO_ARCHIVE_BUDGET (plus git.KILL_GRACE when a git call had
    to be killed); agents not checked by then are checked next time. Returns the ids
    archived."""
    deadline = time.monotonic() + AUTO_ARCHIVE_BUDGET
    marks = orch.store.archived()
    activity = orch.store.last_activity()
    hours: dict[str, float] = {}
    candidates = []
    for agent in orch.store.list_agents():
        if agent.project is None or agent.id in marks:
            continue
        if agent.project not in hours:
            hours[agent.project] = settings.archive_after(orch.store, agent.project)
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
        if orch.is_lead(agent) or orch.store.active_runs(agent.id):
            continue
        if orch.store.active_descendants(agent.id) or orch._check_held(agent, True):
            continue
        if orch.unsaved(agent, deadline) or time.monotonic() >= deadline:
            continue
        held = orch._held(agent, undelivered=True)
        if orch.store.archive(agent.id, ARCHIVE_AUTO, idle_since=cutoff, held=held) is None:
            archived.append(agent.id)
    return archived


def due(name: str, every: float) -> bool:
    """Whether a periodic pass is due: True (and the stamp file `name` under ORCH_HOME
    renewed) when no process stamped it in the last `every` seconds; False while another
    process holds its lock (it is stamping right now). Never raises."""
    try:
        with open(config.home() / name, "a+") as stamp:
            fcntl.flock(stamp, fcntl.LOCK_EX | fcntl.LOCK_NB)  # held: another one checks
            stamp.seek(0)
            try:
                elapsed = time.time() - float(stamp.read().strip() or 0)
            except ValueError:  # unreadable: check now and write a fresh one
                elapsed = every
            if abs(elapsed) < every:  # a clock set back far: check
                return False
            stamp.seek(0)
            stamp.truncate()
            stamp.write(f"{time.time():.3f}\n")
    except OSError:  # BlockingIOError too: another process is checking right now
        return False
    return True


def tidy(orch: Facade) -> list[str]:
    """Run `auto_archive` if no process ran it in the last AUTO_ARCHIVE_EVERY seconds
    (a stamp file under ORCH_HOME, taken under a lock, so concurrent callers skip).
    Never raises; returns the ids archived."""
    if not due("auto-archive.stamp", AUTO_ARCHIVE_EVERY):
        return []
    try:
        return orch.auto_archive()
    except Exception as exc:
        print(f"automatic archiving failed: {exc!r}", file=sys.stderr)
        return []


def remove(orch: Facade, agent_id: str, delete_branch: bool = False) -> None:
    """Delete an archived agent together with the worktree it owns (stopped first).
    Only archived agents are deleted: archive one first."""
    agent = orch.agent(agent_id)
    if not orch.store.archived(agent.project).get(agent.id):
        raise OrchestratorError(f"{agent.id} is not archived; only archived agents are deleted")
    orch.stop(agent.id)
    if agent.owns_worktree:
        project = orch.project(agent.project)
        sharing = [
            other.id
            for other in orch.store.list_agents(agent.project)
            if other.id != agent.id and other.cwd == agent.cwd
        ]
        if sharing:
            raise OrchestratorError(
                f"worktree is shared with {', '.join(sharing)}; remove those agents first"
            )
        try:
            git.remove_worktree(project.path, agent.cwd, agent.branch if delete_branch else None)
        except git.GitError as exc:
            raise OrchestratorError(str(exc)) from None
    decisions.withdraw_all(orch.store, agent.id, decisions.REMOVED)
    orch.store.delete_agent(agent.id)
