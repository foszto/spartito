"""Compacting an agent's conversation: by hand, the refusal, the automatic compaction
policy (threshold and pause) and what a lead's compaction keeps."""

from __future__ import annotations

from typing import Protocol

from .. import providers, roles, settings
from ..errors import OrchestratorError
from ..models import COMPACT, Agent, Run
from ..store import Store
from ..textfmt import kilo
from ..timeline import AgentRuns


class Facade(Protocol):
    """What these functions use of the facade (they are its methods)."""

    store: Store

    def agent(self, agent_id: str) -> Agent: ...

    def compact_prompt(self, agent: Agent, instructions: str | None) -> str: ...

    def _is_lead(self, agent: Agent) -> bool: ...

    def compaction_paused(
        self, agent: Agent, threshold: int | None = None
    ) -> tuple[int | None, int] | None: ...

    def _note_paused(self, run_id: int, agent: Agent, left: int, threshold: int) -> None: ...

    def compact_refusal(self, agent: Agent) -> str | None: ...

    def _launch(self, run: Run) -> None: ...


# What the summary of a lead role's compaction (the orchestrator, project leads) keeps when
# no instructions are given: by an automatic compaction or `orch compact` without any.
LEAD_KEEP = (
    "Keep: the owner's standing rules and preferences; the current state; open decisions and"
    " open items; work in flight with its agent ids, branches and run ids."
)


def compact(orch: Facade, agent_id: str, instructions: str | None = None) -> Run:
    """Queue a turn that compacts the agent's conversation with the CLI's own means
    (Claude: `/compact [instructions]`), in order with its other turns. The session
    stays the same; the run's result is a short summary (`compacted 48k -> 1k tokens`)
    that is never delivered to a parent and wakes no one."""
    agent = orch.agent(agent_id)
    refusal = orch.compact_refusal(agent)
    if refusal:
        raise OrchestratorError(f"cannot compact {agent.id}: {refusal}")
    try:
        prompt = orch.compact_prompt(agent, instructions)
    except ValueError as exc:
        raise OrchestratorError(f"cannot compact {agent.id}: {exc}") from None
    run = orch.store.add_run(agent.id, prompt, kind=COMPACT)
    orch._launch(run)
    return run


def compact_prompt(orch: Facade, agent: Agent, instructions: str | None) -> str:
    """The prompt of a compaction of the agent. Without instructions a lead role gets
    LEAD_KEEP, where its provider takes instructions. Raises ValueError."""
    compact_prompt = providers.get(agent.provider).compact_prompt  # type: ignore[attr-defined]
    if (instructions or "").strip() or not orch._is_lead(agent):
        return compact_prompt(instructions)
    try:
        return compact_prompt(LEAD_KEEP)
    except ValueError:  # e.g. Codex, which takes no instructions
        return compact_prompt(None)


def _is_lead(orch: Facade, agent: Agent) -> bool:
    project = orch.store.get_project(agent.project) if agent.project else None
    try:
        return roles.load(agent.role, project.path if project else None).lead
    except KeyError:
        return False


def auto_compact(orch: Facade, run_id: int) -> Run | None:
    """At the end of a turn that succeeded (its runner calls this before it marks the
    run done, so no queued run can take the turn first): queue an automatic compaction
    when the context is above the threshold (settings.compact_at); it goes ahead of the
    agent's queued messages (Store.may_start). Not after a compaction, not while one is
    queued or running, not while paused (compaction_paused: the last compaction left
    the context above the threshold; the turn that measured it says so in the log), and
    quietly not when the agent cannot be compacted (compact_refusal)."""
    run = orch.store.get_run(run_id)
    if run is None or orch.store.meta(run_id).kind == COMPACT:
        return None
    agent = orch.agent(run.agent_id)
    threshold = settings.compact_at(orch.store, agent.project)
    if not threshold:
        return None
    timeline = AgentRuns.read(orch.store, agent.id)
    record = timeline.context_now(agent.session_id)
    tokens = record.context_tokens if record else None
    if tokens is None or tokens <= threshold:
        return None
    left = timeline.compaction_left(agent.session_id)
    if left is not None and left[0] is None:
        return None  # nothing measured since the last compaction: the size is not known
    paused = orch.compaction_paused(agent, threshold)
    if paused is not None:
        if paused[1] == run_id:  # this turn measured it: say so once
            orch._note_paused(run_id, agent, paused[0], threshold)
        return None
    if orch.compact_refusal(agent):
        return None
    try:
        prompt = orch.compact_prompt(agent, None)
    except ValueError:
        return None
    queued = orch.store.add_auto_compaction(agent.id, prompt)
    if queued is None:
        return None
    try:  # also a stop of the finishing turn meanwhile leaves no compaction behind
        orch.store.add_event(
            queued.id,
            agent.id,
            "info",
            f"automatic compaction: context {kilo(tokens)} is above the threshold of"
            f" {kilo(threshold)}",
        )
        orch._launch(queued)
    except BaseException:
        orch.store.delete_run(queued.id)
        raise
    return queued


def compaction_paused(
    orch: Facade, agent: Agent, threshold: int | None = None
) -> tuple[int | None, int] | None:
    """What the agent's most recent compaction left (compaction_left: tokens, measuring
    run) when that is above the threshold: then no automatic compaction is queued for
    the agent until a compaction gets it below, or the threshold is raised above it.
    None otherwise."""
    if threshold is None:
        threshold = settings.compact_at(orch.store, agent.project)
    left = AgentRuns.read(orch.store, agent.id).compaction_left(agent.session_id)
    paused = threshold and left is not None and left[0] is not None and left[0] > threshold
    return left if paused else None


def compaction_ended(orch: Facade, run_id: int) -> None:
    """After a compaction run ended: when it measured the size it left itself and that
    pauses automatic compaction, say so in the agent's log (otherwise the turn that
    measures it does, see auto_compact)."""
    run = orch.store.get_run(run_id)
    agent = orch.store.get_agent(run.agent_id) if run and not run.status.active else None
    if agent is None:
        return
    threshold = settings.compact_at(orch.store, agent.project)
    paused = orch.compaction_paused(agent, threshold)
    if paused is not None and paused[1] == run_id:
        orch._note_paused(run_id, agent, paused[0], threshold)


def _note_paused(orch: Facade, run_id: int, agent: Agent, left: int, threshold: int) -> None:
    orch.store.add_event(
        run_id,
        agent.id,
        "info",
        f"automatic compaction paused: the last compaction left the context at"
        f" {kilo(left)}, above the threshold of {kilo(threshold)}; a compaction that gets"
        f" it below (orch compact {agent.id}) or a higher threshold resumes it",
    )


def compact_refusal(orch: Facade, agent: Agent) -> str | None:
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
        record = AgentRuns.read(orch.store, agent.id).context_now(agent.session_id)
        if record is None or not record.context_tokens:
            return "its context size is not known yet (it is after its next turn)"
    return None
