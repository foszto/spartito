"""The end of a run (core/lifecycle): which steps run, in which order, around the store write."""

from __future__ import annotations

from typing import Any

import pytest

from orchestrator import desktop, notify, runner
from orchestrator.core import compaction, lifecycle
from orchestrator.models import COMPACT, Run, RunStatus
from orchestrator.service import Orchestrator
from orchestrator.store import Store


def record_steps(monkeypatch: pytest.MonkeyPatch, fail: tuple[str, ...] = ()) -> list[Any]:
    """Replace the steps run_ended takes with recorders; those named in `fail` raise after
    recording."""
    steps: list[Any] = []

    def recorder(name: str) -> Any:
        def step(store: Store, run_id: int, *args: Any) -> None:
            steps.append((name, run_id, *args))
            if name in fail:
                raise RuntimeError(name)

        return step

    monkeypatch.setattr(compaction, "compaction_ended", recorder("compaction_ended"))
    monkeypatch.setattr(notify, "run_ended", recorder("notify"))
    monkeypatch.setattr(desktop, "turn_ended", recorder("desktop"))
    return steps


def no_launch(run: Run) -> None:
    raise AssertionError("nothing is launched here")


def test_after_a_run_ended_the_parents_then_the_desktop(
    orch: Orchestrator, monkeypatch: pytest.MonkeyPatch
) -> None:
    steps = record_steps(monkeypatch)

    lifecycle.run_ended(orch.store, 7, "a", no_launch, recheck=True)

    assert steps == [("notify", 7, "a", no_launch, True), ("desktop", 7, "a")]


def test_a_compactions_runner_notes_its_outcome_first(
    orch: Orchestrator, monkeypatch: pytest.MonkeyPatch
) -> None:
    steps = record_steps(monkeypatch)

    lifecycle.run_ended(orch.store, 7, "a", no_launch, compacted=True)

    assert [step[0] for step in steps] == ["compaction_ended", "notify", "desktop"]


def test_a_failing_step_never_keeps_the_next_from_running(
    orch: Orchestrator, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    steps = record_steps(monkeypatch, fail=("compaction_ended", "notify"))

    lifecycle.run_ended(orch.store, 7, "a", no_launch, compacted=True)  # no exception

    assert [step[0] for step in steps] == ["compaction_ended", "notify", "desktop"]
    err = capsys.readouterr().err
    assert "checking the compaction's outcome failed" in err
    assert "notifying the parents of a failed" in err


def test_the_facade_and_reconcile_take_the_same_steps_without_the_compaction_note(
    orch: Orchestrator, monkeypatch: pytest.MonkeyPatch
) -> None:
    steps = record_steps(monkeypatch)

    orch.run_ended(7, "a")
    orch.store.on_run_end(8, "b", True)

    assert [step[:3] for step in steps] == [
        ("notify", 7, "a"),
        ("desktop", 7, "a"),
        ("notify", 8, "b"),
        ("desktop", 8, "b"),
    ]
    assert steps[0][3] == orch._launch and steps[2][4] is True


def test_a_runners_store_ends_reconciled_runs_through_run_ended(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = Store()
    steps = record_steps(monkeypatch)

    launch = lifecycle.attach(store)
    store.on_run_end(8, "b", True)

    assert steps == [("notify", 8, "b", launch, True), ("desktop", 8, "b")]


def runner_steps(monkeypatch: pytest.MonkeyPatch) -> list[tuple[Any, ...]]:
    """What runner.main does at the end of a run, with the run's status at each step."""
    seen: list[tuple[Any, ...]] = []
    finish = runner.Store.finish_run
    turn_done = lifecycle.turn_done

    def status(store: Store, run_id: int) -> str:
        return store.get_run(run_id).status.value

    def finishing(self: Store, run_id: int, run_status: RunStatus, **fields: Any) -> None:
        seen.append(("store", run_status.value))
        finish(self, run_id, run_status, **fields)

    def done(store: Store, run_id: int, launch: Any) -> Run | None:
        seen.append(("turn_done", status(store, run_id)))
        return turn_done(store, run_id, launch)

    def ended(store: Store, run_id: int, agent_id: str, launch: Any, **kw: Any) -> None:
        seen.append(("run_ended", status(store, run_id), kw))

    monkeypatch.setattr(runner.Store, "finish_run", finishing)
    monkeypatch.setattr(lifecycle, "turn_done", done)
    monkeypatch.setattr(lifecycle, "run_ended", ended)
    return seen


def test_a_succeeded_turn_checks_for_a_compaction_before_it_is_stored(
    orch: Orchestrator, project: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen = runner_steps(monkeypatch)
    agent = orch.create_agent(project, name="w", worktree=False)
    run = orch.store.add_run(agent.id, "hello")

    runner.main(run.id)

    assert seen == [
        ("turn_done", "running"),  # the turn still holds the agent
        ("store", "done"),
        ("run_ended", "done", {"compacted": False}),
    ]


def test_a_failed_turn_has_no_compaction_check(
    orch: Orchestrator, project: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen = runner_steps(monkeypatch)
    agent = orch.create_agent(project, name="w", worktree=False)
    run = orch.store.add_run(agent.id, "FAIL")

    runner.main(run.id)

    assert seen == [("store", "failed"), ("run_ended", "failed", {"compacted": False})]


def test_a_compaction_has_no_compaction_check_and_notes_its_outcome(
    orch: Orchestrator, project: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen = runner_steps(monkeypatch)
    agent = orch.create_agent(project, name="w", worktree=False)
    run = orch.store.add_run(agent.id, "/compact", kind=COMPACT)

    runner.main(run.id)

    assert [step[0] for step in seen] == ["store", "run_ended"]
    assert seen[-1][2] == {"compacted": True}


def test_a_run_that_is_no_longer_active_ends_nothing(
    orch: Orchestrator, project: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    agent = orch.create_agent(project, name="w", worktree=False)
    run = orch.store.add_run(agent.id, "hello")
    orch.store.finish_run(run.id, RunStatus.FAILED, error="gone")
    seen = runner_steps(monkeypatch)

    runner.main(run.id)
    runner.main(run.id + 100)  # missing

    assert seen == []
