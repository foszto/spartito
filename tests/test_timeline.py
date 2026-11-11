"""The read model of one agent's runs (timeline.AgentRuns) and the read behind it
(Store.snapshot)."""

from __future__ import annotations

import pytest

from orchestrator.models import COMPACT, Run, RunStatus, Usage, context_now, running_total
from orchestrator.service import Orchestrator
from orchestrator.store import Store
from orchestrator.timeline import AgentRuns, agent_state, compaction_left


def agent_with_runs(orch: Orchestrator, project: str) -> str:
    agent_id = orch.create_agent(project, name="w", worktree=False).id
    first = orch.store.add_run(agent_id, "one")
    orch.store.set_usage(first.id, agent_id, Usage(input_tokens=10, context_tokens=100))
    orch.store.finish_run(first.id, RunStatus.DONE, result="ONE")
    compaction = orch.store.add_run(agent_id, "/compact", kind=COMPACT)
    orch.store.finish_run(compaction.id, RunStatus.DONE, result="compacted")
    orch.store.add_run(agent_id, "two", sender="someone")
    return agent_id


def test_a_snapshot_holds_what_the_four_reads_return(orch: Orchestrator, project: str) -> None:
    agent_id = agent_with_runs(orch, project)
    store = orch.store

    runs, meta, usage, compactions = store.snapshot(agent_id)

    assert runs == store.runs_of(agent_id)
    assert meta == store.meta_of(agent_id)
    assert usage == store.usage_of(agent_id)
    assert compactions == store.compactions(agent_id)
    assert len(runs) == 3 and len(compactions) == 1 and list(usage) == [runs[0].id]


def test_a_snapshot_of_an_unknown_agent_is_empty(orch: Orchestrator) -> None:
    assert orch.store.snapshot("nobody") == ([], {}, {}, set())


def test_a_snapshot_reads_in_one_transaction(
    orch: Orchestrator, project: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A compaction queued while the snapshot is read is in none of its parts."""
    agent_id = agent_with_runs(orch, project)
    meta_of = Store._meta_of
    queued: list[Run] = []

    def queue_meanwhile(conn, agent: str):  # after the runs, before the compaction ids
        queued.append(orch.store.add_run(agent, "/compact", kind=COMPACT))
        return meta_of(conn, agent)

    monkeypatch.setattr(Store, "_meta_of", staticmethod(queue_meanwhile))
    runs, meta, _, compactions = orch.store.snapshot(agent_id)

    assert queued and queued[0].id not in compactions | set(meta) | {run.id for run in runs}
    assert queued[0].id in orch.store.compactions(agent_id)


def test_agent_runs_derive_what_the_functions_do(orch: Orchestrator, project: str) -> None:
    agent_id = agent_with_runs(orch, project)
    store = orch.store
    store.set_session(agent_id, "s")
    runs, usage, compactions = (
        store.runs_of(agent_id),
        store.usage_of(agent_id),
        store.compactions(agent_id),
    )

    timeline = AgentRuns.read(store, agent_id)

    assert timeline.agent_id == agent_id and timeline.meta == store.meta_of(agent_id)
    assert timeline.state() == agent_state(runs, compactions)
    assert timeline.state()[0] == "waiting"
    assert [run.prompt for run in timeline.answers()] == ["one", "two"]
    assert timeline.totals() == running_total(runs, usage)
    assert timeline.totals().input_tokens == 10
    for session in ("s", None):
        assert timeline.context_now(session) == context_now(runs, usage, session)
        assert timeline.compaction_left(session) == compaction_left(
            runs, usage, compactions, session
        )


def test_agent_runs_without_meta_or_usage_rows() -> None:
    """Runs queued before run metadata, and runs without a usage record."""
    runs = [
        Run(id=1, agent_id="a", prompt="x", status=RunStatus.DONE, cost_usd=0.5, started_at="1"),
        Run(id=2, agent_id="a", prompt="y", status=RunStatus.FAILED, started_at="2"),
    ]
    timeline = AgentRuns("a", runs, {}, {}, set())

    assert timeline.state() == ("failed", [], runs[1])
    assert timeline.answers() == runs
    assert timeline.totals().cost_usd == 0.5
    assert timeline.context_now("s") is None
    assert timeline.compaction_left("s") is None
