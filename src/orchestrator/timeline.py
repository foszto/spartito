"""One agent's runs as one consistent read: the runs with their metadata, usage records and
compaction ids (Store.snapshot), and what the callers derive from them."""

from __future__ import annotations

from collections.abc import Collection
from dataclasses import dataclass
from typing import TYPE_CHECKING

from .models import Run, RunStatus, Usage, answers, context_now, running_total, turn_order

if TYPE_CHECKING:
    from .store import RunMeta, Store


def compaction_left(
    runs: list[Run], usage: dict[int, Usage], compactions: Collection[int], session_id: str | None
) -> tuple[int | None, int] | None:
    """What the agent's most recent compaction really left, as measured: (tokens, the run
    that measured it); (None, the compaction's run) while nothing measured it yet; None
    without a compaction. Runs in the order they ran (models.turn_order).

    The most recent compaction is the latest compaction run that started and ended (in any
    way: one that never started compacted nothing) or turn in which the CLI compacted on its
    own. The CLI's own figure after compacting is not used (Claude's leaves out the system
    prompt and tools; Codex has none). A compaction run that measured the size after
    compacting tells it itself; otherwise the first later run that measured anything: the
    size at its first model request (Claude: what the compaction left, prompt and tools
    included), or where that is not reported (Codex) the size at its end, which can only
    be larger: conservative."""
    ordered = sorted(runs, key=turn_order)
    # Only the current session: from after the last run recorded in another one.
    for index in range(len(ordered) - 1, -1, -1):
        other = usage.get(ordered[index].id)
        if other is not None and other.session_id not in (None, session_id):
            ordered = ordered[index + 1 :]
            break

    def measured(run: Run) -> Usage | None:
        record = usage.get(run.id)
        return record if record is not None and record.session_id == session_id else None

    last = None
    for index, run in enumerate(ordered):
        if run.id in compactions:
            if run.started_at and not run.status.active:
                last = index
        elif (record := measured(run)) and record.compact_trigger:
            last = index
    if last is None or not session_id:
        return None
    compaction = ordered[last]
    record = measured(compaction)
    if compaction.id in compactions and record and record.context_tokens is not None:
        return record.context_tokens, compaction.id
    for run in ordered[last + 1 :]:
        record = measured(run)
        if run.id in compactions or record is None:
            continue
        first = record.first_context_tokens
        size = first if first is not None else record.context_tokens
        if size is not None:
            return size, run.id
    return None, compaction.id


def agent_state(
    runs: list[Run], compactions: Collection[int] = ()
) -> tuple[str, list[Run], Run | None]:
    """An agent's status from its runs, its queued/running runs and its latest finished run.

    Compaction runs (`compactions`) count as work while they are queued or running, but
    never as the latest finished run nor for the status after it (models.answers)."""
    active = [run for run in runs if run.status.active]
    finished = [run for run in answers(runs, compactions) if not run.status.active]
    if any(run.status is RunStatus.RUNNING for run in active):
        status = "running"
    elif active:
        status = "waiting"
    else:
        status = finished[-1].status.value if finished else "new"
    return status, active, finished[-1] if finished else None


@dataclass(frozen=True)
class AgentRuns:
    """An agent's runs (in id order), their metadata and usage records by run id (runs
    without a row are absent) and the ids of its compaction runs, all from one read
    (Store.snapshot). A snapshot: callers do not modify it, and read anew after a write."""

    agent_id: str
    runs: list[Run]
    meta: dict[int, RunMeta]
    usage: dict[int, Usage]
    compactions: set[int]

    @classmethod
    def read(cls, store: Store, agent_id: str) -> AgentRuns:
        return cls(agent_id, *store.snapshot(agent_id))

    def state(self) -> tuple[str, list[Run], Run | None]:
        """The status, the queued/running runs and the latest finished run (agent_state)."""
        return agent_state(self.runs, self.compactions)

    def answers(self) -> list[Run]:
        """The runs that are not compactions (models.answers)."""
        return answers(self.runs, self.compactions)

    def context_now(self, session_id: str | None) -> Usage | None:
        """The usage record that tells how full the conversation is (models.context_now)."""
        return context_now(self.runs, self.usage, session_id)

    def compaction_left(self, session_id: str | None) -> tuple[int | None, int] | None:
        """What the most recent compaction left, as measured (compaction_left)."""
        return compaction_left(self.runs, self.usage, self.compactions, session_id)

    def totals(self) -> Usage:
        """Tokens and cost of all the runs (models.running_total)."""
        return running_total(self.runs, self.usage)
