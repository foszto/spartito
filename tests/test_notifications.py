"""Parents stay reachable while their agents work and are notified when they finish.

Timing is driven by handshakes, not by sleeps that must outlast something: a *gate* is a
run that is never launched, so an agent queued `after` it stays active until the test
finishes the gate; a long `SLEEP` turn is ended by signalling its runner.
"""

from __future__ import annotations

import functools
import json
import os
import signal
import sqlite3
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import anyio
import pytest
from test_store import OLD_RUN_COLUMNS, OLD_SCHEMA

from orchestrator import mcp_server, notify, service
from orchestrator.models import Run, RunStatus
from orchestrator.service import ORCHESTRATOR_ID, Orchestrator, OrchestratorError
from orchestrator.store import NOTIFICATION, Store


def until(predicate: Callable[[], Any], timeout: float = 30, what: str = "condition") -> Any:
    """Poll a read-only predicate (no reconcile, nothing that could queue a turn)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if value := predicate():
            return value
        time.sleep(0.05)
    raise AssertionError(f"timed out waiting for {what}")


def wait(*agent_ids: str, **options: Any) -> dict[str, Any]:
    return anyio.run(functools.partial(mcp_server.wait_for_agents, list(agent_ids), **options))


def lead(orch: Orchestrator, project: str, name: str = "lead", parent: str | None = None) -> str:
    return orch.create_agent(project, name=name, role="lead", worktree=False, parent_id=parent).id


def notifications(orch: Orchestrator, agent_id: str) -> list[Run]:
    meta = orch.store.meta_of(agent_id)
    return [
        run
        for run in orch.store.runs_of(agent_id)
        if run.id in meta and meta[run.id].kind == NOTIFICATION
    ]


def finished_notes(orch: Orchestrator, agent_id: str) -> list[Run]:
    return [run for run in notifications(orch, agent_id) if not run.status.active]


def body(run: Run) -> dict[str, Any]:
    assert run.prompt.startswith(notify.HEADER)
    return json.loads(run.prompt[len(notify.HEADER) :])


def reported(run: Run) -> list[str]:
    return [agent["id"] for agent in body(run)["agents"]]


def settle(orch: Orchestrator, *agent_ids: str, timeout: float = 30) -> None:
    assert orch.wait_idle(list(agent_ids), timeout), "agents did not settle in time"


def ended(orch: Orchestrator, run_id: int) -> bool:
    """The run has ended and its parents were checked."""
    run = orch.store.get_run(run_id)
    return bool(run and not run.status.active and orch.store.meta(run_id).checked)


def running(orch: Orchestrator, run_id: int) -> bool:
    return orch.store.get_run(run_id).status is RunStatus.RUNNING


def gate(orch: Orchestrator, project: str) -> int:
    agent = orch.create_agent(project, name="gate", worktree=False).id
    return orch.store.add_run(agent, "never launched").id


def open_gate(orch: Orchestrator, gate_id: int) -> None:
    orch.store.finish_run(gate_id, RunStatus.DONE, result="open")


def end_turn(orch: Orchestrator, run_id: int) -> None:
    """End a running turn (its runner records it as stopped), nothing else of the agent."""
    orch._kill(orch.store.get_run(run_id), signal.SIGTERM)
    until(lambda: ended(orch, run_id), what="the turn to end")


def dead_pid() -> int:
    proc = subprocess.Popen(["true"])
    proc.wait()
    return proc.pid


@pytest.fixture
def as_agent(monkeypatch: pytest.MonkeyPatch) -> Callable[[str | None], None]:
    """Make the MCP tools act for an agent (None: the interactive session)."""

    def act(agent_id: str | None) -> None:
        if agent_id:
            monkeypatch.setenv("ORCH_AGENT_ID", agent_id)
        else:
            monkeypatch.delenv("ORCH_AGENT_ID", raising=False)

    return act


def spawn(as_agent: Callable, parent: str, task: str, **options: Any) -> str:
    as_agent(parent)
    try:
        reply = mcp_server.spawn_agent(task, worktree=False, **options)
    finally:
        as_agent(None)
    return reply["id"]


def message(as_agent: Callable, sender: str, agent_id: str, text: str) -> int:
    as_agent(sender)
    try:
        return mcp_server.send_message(agent_id, text)["run_id"]
    finally:
        as_agent(None)


def finished_child(
    orch: Orchestrator, project: str, parent: str, result: str = "the answer", **kw: Any
) -> tuple[str, int]:
    """A child with a run the parent sent, finished without a runner (and checked)."""
    child = orch.create_agent(project, name="c", worktree=False, parent_id=parent).id
    run = orch.store.add_run(child, "task", sender=parent, **kw)
    orch.store.finish_run(run.id, RunStatus.DONE, result=result)
    orch.store.mark_checked(run.id)  # as its runner would have
    return child, run.id


def busy_agent(orch: Orchestrator, project: str, monkeypatch: pytest.MonkeyPatch) -> str:
    """An agent of nobody (stopping it notifies no one) that stays queued behind a gate.
    Its runner may not handle SIGTERM yet when the test stops it: keep the grace short."""
    monkeypatch.setattr(service, "STOP_GRACE_SECONDS", 0.3)
    return orch.spawn(project, "slow", worktree=False, after=[gate(orch, project)])[0].id


# -- the reported situation --------------------------------------------------


def test_a_user_message_runs_while_the_child_works_and_the_result_follows(
    orch: Orchestrator, project: str, as_agent: Callable
) -> None:
    parent = lead(orch, project)
    gate_id = gate(orch, project)
    child = spawn(as_agent, parent, "child-work", after=[str(gate_id)])

    user = orch.send(parent, "hello from the user")
    until(lambda: ended(orch, user.id), what="the user message")
    child_run = orch.store.last_run(child)
    assert child_run.status.active and notifications(orch, parent) == []
    open_gate(orch, gate_id)
    # Only reads from here on: the notification must come from the child's runner.
    (note,) = until(lambda: finished_notes(orch, parent), what="the notification")

    child_run = orch.store.get_run(child_run.id)
    assert orch.store.meta(child_run.id).checked
    assert orch.store.get_run(user.id).ended_at <= child_run.ended_at <= note.started_at
    assert note.status is RunStatus.DONE and note.output_schema is None
    assert note.prompt.count("\n") <= 3
    (entry,) = body(note)["agents"]
    assert (entry["id"], entry["status"]) == (child, "done")
    assert entry["last_run"] == {
        "id": child_run.id,
        "status": "done",
        "result": "echo[new]: child-work",
    }
    kinds = [event.kind for event in orch.store.events(parent) if event.run_id == note.id]
    assert kinds[0] == "notification"
    until(lambda: ended(orch, note.id), what="the notification's check")
    assert len(notifications(orch, parent)) == 1 and not orch.store.active_runs(parent)


def test_the_dispatch_reply_says_in_words_what_happens_next() -> None:
    assert mcp_server.NEXT == {
        "each": "end your turn; an [orch notification] wakes you when this agent is done",
        "all": "end your turn; an [orch notification] wakes you when all your agents are done",
    }
    assert mcp_server.NEXT_UNNOTIFIED == "nothing will wake you: call wait_for_agents"


def test_the_dispatch_reply_says_whether_the_caller_will_be_woken(
    orch: Orchestrator, project: str, as_agent: Callable
) -> None:
    parent, other = lead(orch, project), lead(orch, project, "other")
    as_agent(parent)
    mine = mcp_server.spawn_agent("x", worktree=False, notify="all")
    to_other = mcp_server.send_message(other, "y")
    as_agent(None)
    interactive = mcp_server.spawn_agent("z", project=project, worktree=False)

    assert next(iter(mine)) == "next" and "notify" not in mine
    assert mine["next"] == mcp_server.NEXT["all"]
    assert to_other["next"] == interactive["next"] == mcp_server.NEXT_UNNOTIFIED
    assert orch.store.meta(mine["run_id"]).sender == parent
    assert orch.store.meta(interactive["run_id"]).sender is None
    settle(orch, parent, other, interactive["id"])


# -- coalescing --------------------------------------------------------------


def test_children_finishing_within_the_settle_delay_share_one_notification(
    orch: Orchestrator, project: str, as_agent: Callable, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ORCH_NOTIFY_DELAY", "2")
    parent = lead(orch, project)
    gate_id = gate(orch, project)
    first = spawn(as_agent, parent, "a", after=[str(gate_id)])
    second = spawn(as_agent, parent, "b", after=[str(gate_id)])
    open_gate(orch, gate_id)

    settle(orch, parent)

    (note,) = notifications(orch, parent)
    assert reported(note) == [first, second]


def test_completions_while_the_parent_is_busy_arrive_as_one_notification(
    orch: Orchestrator, project: str, as_agent: Callable
) -> None:
    parent = lead(orch, project)
    busy = orch.send(parent, "SLEEP 30 busy")
    until(lambda: running(orch, busy.id), what="the busy turn")
    first = spawn(as_agent, parent, "one")
    second = spawn(as_agent, parent, "two")
    until(lambda: all(ended(orch, orch.store.last_run(a).id) for a in (first, second)))
    (queued,) = notifications(orch, parent)
    assert queued.status is RunStatus.WAITING and running(orch, busy.id)

    end_turn(orch, busy.id)
    settle(orch, parent)

    (note,) = notifications(orch, parent)
    assert reported(note) == [first, second]
    assert note.started_at >= orch.store.get_run(busy.id).ended_at


def test_notify_all_waits_until_every_child_is_settled(
    orch: Orchestrator, project: str, as_agent: Callable
) -> None:
    parent = lead(orch, project)
    gate_id = gate(orch, project)
    fast = spawn(as_agent, parent, "fast", notify="all")
    slow = spawn(as_agent, parent, "slow", notify="all", after=[str(gate_id)])
    until(lambda: ended(orch, orch.store.last_run(fast).id), what="the fast child")
    assert notifications(orch, parent) == []

    open_gate(orch, gate_id)
    settle(orch, parent)

    (note,) = notifications(orch, parent)
    assert reported(note) == [fast, slow]


def test_notify_all_ignores_children_the_parent_never_sent_work(
    orch: Orchestrator, project: str
) -> None:
    parent = lead(orch, project)
    finished_child(orch, project, parent, notify="all")
    by_user = orch.create_agent(project, name="u", worktree=False, parent_id=parent).id
    orch.store.add_run(by_user, "started by the user, still queued")

    assert notify.queue_if_due(orch.store, parent) is not None


def test_notify_each_wakes_the_parent_after_the_first_child(
    orch: Orchestrator, project: str, as_agent: Callable
) -> None:
    parent = lead(orch, project)
    gate_id = gate(orch, project)
    fast = spawn(as_agent, parent, "fast")
    slow = spawn(as_agent, parent, "slow", after=[str(gate_id)])

    (first,) = until(lambda: finished_notes(orch, parent), what="the first notification")
    assert reported(first) == [fast] and body(first)["still_running"] == [slow]
    open_gate(orch, gate_id)
    settle(orch, parent)

    first, second = notifications(orch, parent)
    assert reported(second) == [slow]


# -- no duplicates -------------------------------------------------------------


def test_a_result_claimed_by_wait_for_agents_is_not_notified(
    orch: Orchestrator, project: str, as_agent: Callable
) -> None:
    parent = lead(orch, project)
    busy = orch.send(parent, "SLEEP 30 busy")
    until(lambda: running(orch, busy.id), what="the busy turn")
    child = spawn(as_agent, parent, "quick")

    as_agent(parent)
    reply = wait(child, timeout_seconds=30)
    as_agent(None)
    (queued,) = until(lambda: notifications(orch, parent), what="the queued notification")
    assert reply["finished"] and reply["agents"][0]["last_run"]["result"] == "echo[new]: quick"
    assert queued.status is RunStatus.WAITING

    end_turn(orch, busy.id)
    settle(orch, parent)

    assert [run.id for run in orch.store.runs_of(parent)] == [busy.id]  # deleted, no turn


def test_a_notified_result_is_already_delivered_to_a_later_wait(
    orch: Orchestrator, project: str, as_agent: Callable
) -> None:
    parent = lead(orch, project)
    child = spawn(as_agent, parent, "quick")
    settle(orch, parent)
    assert len(notifications(orch, parent)) == 1

    as_agent(parent)
    (outcome,) = wait(child)["agents"]

    assert "already_delivered" in outcome["last_run"]


def test_runs_without_metadata_or_from_the_user_are_never_notified(
    orch: Orchestrator, project: str
) -> None:
    parent = lead(orch, project)
    child = orch.create_agent(project, name="c", worktree=False, parent_id=parent).id
    with sqlite3.connect(orch.store.path) as conn:  # as code from before run metadata
        cursor = conn.execute(
            "INSERT INTO runs (agent_id, prompt, status, created_at) VALUES (?, 'old', 'done', 't')",
            (child,),
        )
    conn.close()
    by_user = orch.store.add_run(child, "from the user")
    orch.store.finish_run(by_user.id, RunStatus.DONE, result="r")

    orch.run_ended(cursor.lastrowid, child)
    orch.run_ended(by_user.id, child)

    assert orch.store.runs_of(parent) == []
    assert orch.wait_idle([parent], 2)


def test_stop_agent_by_the_parent_counts_as_delivered(
    orch: Orchestrator, project: str, as_agent: Callable
) -> None:
    parent = lead(orch, project)
    busy = orch.send(parent, "SLEEP 30 busy")
    child = spawn(as_agent, parent, "SLEEP 30")
    until(lambda: running(orch, orch.store.last_run(child).id), what="the child")

    as_agent(parent)
    assert mcp_server.stop_agent(child) == {"stopped_runs": 1}
    as_agent(None)
    end_turn(orch, busy.id)
    settle(orch, parent)

    assert [run.id for run in orch.store.runs_of(parent)] == [busy.id]


# -- hierarchy and origin -------------------------------------------------------


def test_a_grandchild_wakes_the_lead_and_then_the_lead_wakes_its_parent(
    orch: Orchestrator, project: str, as_agent: Callable
) -> None:
    top = lead(orch, project, "top")
    middle = lead(orch, project, "middle", parent=top)
    gate_id = gate(orch, project)
    brief = message(as_agent, top, middle, "SLEEP 1 brief")
    until(lambda: running(orch, brief), what="the brief")
    worker = spawn(as_agent, middle, "work", after=[str(gate_id)])  # during the brief's turn
    until(lambda: ended(orch, brief), what="the brief to end")
    assert orch.store.runs_of(top) == []  # the lead is not settled: its worker is queued

    open_gate(orch, gate_id)
    settle(orch, top)

    (lead_note,) = notifications(orch, middle)
    (top_note,) = notifications(orch, top)
    assert reported(lead_note) == [worker]
    (entry,) = body(top_note)["agents"]
    assert entry["id"] == middle and entry["last_run"]["id"] == lead_note.id
    assert entry["last_run"]["result"].startswith("echo[resumed]: [orch notification]")
    assert entry["earlier_run_ids"] == [brief]
    assert orch.store.meta(lead_note.id).origin == top


def test_a_lead_briefed_by_the_user_does_not_wake_its_parent(
    orch: Orchestrator, project: str, as_agent: Callable
) -> None:
    top = lead(orch, project, "top")
    middle = lead(orch, project, "middle", parent=top)
    brief = orch.send(middle, "SLEEP 1 from the user")
    until(lambda: running(orch, brief.id), what="the brief")
    spawn(as_agent, middle, "work")

    settle(orch, top)

    (lead_note,) = notifications(orch, middle)
    assert lead_note.status is RunStatus.DONE and orch.store.meta(lead_note.id).origin is None
    assert orch.store.runs_of(top) == []


def test_work_for_the_parent_wakes_it_even_after_a_user_message_in_between(
    orch: Orchestrator, project: str, as_agent: Callable
) -> None:
    top = lead(orch, project, "top")
    middle = lead(orch, project, "middle", parent=top)
    gate_id = gate(orch, project)
    brief = message(as_agent, top, middle, "SLEEP 1 brief")
    until(lambda: running(orch, brief), what="the brief")
    spawn(as_agent, middle, "work", after=[str(gate_id)])
    user = orch.send(middle, "a user message")
    until(lambda: ended(orch, user.id), what="the user message")

    open_gate(orch, gate_id)
    settle(orch, top)

    (top_note,) = notifications(orch, top)
    assert reported(top_note) == [middle]


def test_failed_skipped_and_stopped_child_runs_are_notified(
    orch: Orchestrator, project: str, as_agent: Callable
) -> None:
    parent = lead(orch, project)
    failing = spawn(as_agent, parent, "FAIL", notify="all")
    skipped = spawn(as_agent, parent, "never", after=[failing], notify="all")
    stopped = spawn(as_agent, parent, "SLEEP 30", notify="all")
    until(lambda: running(orch, orch.store.last_run(stopped).id), what="the sleeper")
    orch.stop(stopped)

    settle(orch, parent)

    (note,) = notifications(orch, parent)
    runs = {agent["id"]: agent["last_run"] for agent in body(note)["agents"]}
    assert {key: run["status"] for key, run in runs.items()} == {
        failing: "failed",
        skipped: "skipped",
        stopped: "stopped",
    }
    assert runs[failing]["error"] == "boom" and runs[stopped]["error"] == "stopped"


# -- when a notification itself goes wrong ---------------------------------------


def test_a_failing_notification_turn_releases_its_claims_and_tells_the_grandparent(
    orch: Orchestrator, project: str, as_agent: Callable
) -> None:
    top = lead(orch, project, "top")
    middle = lead(orch, project, "middle", parent=top)
    brief = orch.store.add_run(middle, "brief", sender=top)
    orch.store.finish_run(brief.id, RunStatus.DONE, result="ok")
    orch.store.mark_checked(brief.id)
    # The fake CLI fails on the word FAIL, which the notification text will carry.
    child, run_id = finished_child(orch, project, middle, "x FAIL y", cause_run=brief.id)

    orch.run_ended(run_id, child)
    settle(orch, top)

    (failed,) = notifications(orch, middle)  # no automatic retry
    assert failed.status is RunStatus.FAILED
    assert orch.store.last_delivered(middle, child) is None  # released
    (top_note,) = notifications(orch, top)
    (entry,) = body(top_note)["agents"]
    assert (entry["id"], entry["last_run"]["status"]) == (middle, "failed")
    assert entry["last_run"]["error"] == "boom" and entry["earlier_run_ids"] == [brief.id]
    as_agent(middle)
    assert wait(child)["agents"][0]["last_run"]["result"] == "x FAIL y"


def test_reconcile_deletes_a_dead_pending_notification_and_checks_once_more(
    orch: Orchestrator, project: str
) -> None:
    top = lead(orch, project, "top")
    parent = lead(orch, project, parent=top)
    finished_child(orch, project, parent)
    lost = notify.queue_if_due(orch.store, parent)
    orch.store.update_run(lost.id, pid=dead_pid())

    orch.store.reconcile()
    settle(orch, top)

    assert orch.store.get_run(lost.id) is None  # not left as a failed run
    (note,) = notifications(orch, parent)
    assert note.id != lost.id and note.status is RunStatus.DONE
    assert orch.store.runs_of(top) == []  # its work was not for top


def test_reconcile_fails_a_dead_started_notification_and_releases_its_claims(
    orch: Orchestrator, project: str
) -> None:
    top = lead(orch, project, "top")
    parent = lead(orch, project, parent=top)
    brief = orch.store.add_run(parent, "brief", sender=top)
    orch.store.finish_run(brief.id, RunStatus.DONE, result="ok")
    orch.store.mark_checked(brief.id)
    child, run_id = finished_child(orch, project, parent, cause_run=brief.id)
    orch.store.mark_checked(run_id)
    started = notify.start(orch.store, notify.queue_if_due(orch.store, parent))
    assert orch.store.last_delivered(parent, child) == run_id
    orch.store.update_run(started.id, pid=dead_pid())

    orch.store.reconcile()
    settle(orch, top)

    assert orch.store.get_run(started.id).error == "runner process died"
    assert orch.store.last_delivered(parent, child) is None
    assert notifications(orch, parent) == [orch.store.get_run(started.id)]  # no retry
    (top_note,) = notifications(orch, top)
    assert body(top_note)["agents"][0]["last_run"]["status"] == "failed"


def test_the_kill_branch_of_stop_notifies_and_drops_a_pending_notification(
    orch: Orchestrator, project: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(service, "STOP_GRACE_SECONDS", 0.2)
    parent = lead(orch, project)
    child = orch.create_agent(project, name="c", worktree=False, parent_id=parent).id
    queued = orch.store.add_run(child, "never launched", sender=parent)

    assert orch.stop(child) == 1  # no runner answers SIGTERM: the kill branch ends it
    (note,) = until(lambda: finished_notes(orch, parent), what="the notification")
    assert reported(note) == [child] and orch.store.meta(queued.id).checked

    other = lead(orch, project, "other")
    finished_child(orch, project, other)
    pending = notify.queue_if_due(orch.store, other)
    assert orch.stop(other) == 1
    assert orch.store.get_run(pending.id) is None and orch.store.runs_of(other) == []


def test_a_stop_stays_quiet_while_someone_waits(
    orch: Orchestrator, project: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(service, "STOP_GRACE_SECONDS", 0.5)  # if SIGTERM came too early
    parent = lead(orch, project)
    child, run_id = finished_child(orch, project, parent)
    monkeypatch.setenv("ORCH_NOTIFY_DELAY", "30")
    orch.run_ended(run_id, child)  # the notification's runner sits in its settle delay
    (pending,) = notifications(orch, parent)
    until(lambda: orch.store.get_run(pending.id).pid, what="the runner")
    waiting = threading.Thread(target=orch.wait_idle, args=([parent], 20))
    waiting.start()

    orch.stop(parent)
    waiting.join()
    orch.store.reconcile()

    assert orch.wait_idle([parent], 1) and orch.store.runs_of(parent) == []


# -- turn order ---------------------------------------------------------------


def test_a_user_message_goes_ahead_of_a_waiting_notification(
    orch: Orchestrator, project: str, as_agent: Callable
) -> None:
    parent = lead(orch, project)
    busy = orch.send(parent, "SLEEP 30 busy")
    until(lambda: running(orch, busy.id), what="the busy turn")
    spawn(as_agent, parent, "quick")
    (queued,) = until(lambda: notifications(orch, parent), what="the queued notification")
    user = orch.send(parent, "from the user")
    assert queued.id < user.id

    end_turn(orch, busy.id)
    settle(orch, parent)

    starts = {
        event.run_id: event.id
        for event in orch.store.events(parent)
        if event.kind in ("prompt", "notification")
    }
    assert starts[busy.id] < starts[user.id] < starts[queued.id]


def test_a_message_blocked_on_a_dependency_does_not_hold_a_notification_back(
    orch: Orchestrator, project: str, as_agent: Callable
) -> None:
    parent = lead(orch, project)
    gate_id = gate(orch, project)
    blocked = orch.send(parent, "after the gate", after=[gate_id])
    spawn(as_agent, parent, "quick")

    (note,) = until(lambda: finished_notes(orch, parent), what="the notification")

    assert orch.store.get_run(blocked.id).status is RunStatus.WAITING
    assert note.id > blocked.id
    open_gate(orch, gate_id)
    settle(orch, parent)


def test_runners_started_at_once_never_run_one_agent_twice(
    orch: Orchestrator, project: str
) -> None:
    agent = orch.create_agent(project, name="w", worktree=False).id
    runs = [orch.store.add_run(agent, f"run-{n}") for n in range(4)]
    for run in runs:
        orch._launch(run)

    settle(orch, agent)

    order = [event.run_id for event in orch.store.events(agent)]
    grouped = [run_id for n, run_id in enumerate(order) if n == 0 or order[n - 1] != run_id]
    assert grouped == [run.id for run in runs]  # one after the other, in order
    assert all(run.status is RunStatus.DONE for run in orch.store.runs_of(agent))


def _take(barrier: threading.Barrier, results: dict, run: Run, kind: str, store: Store) -> None:
    barrier.wait()
    results[run.id] = store.take_turn(run, kind)


def test_taking_the_turn_is_atomic(orch: Orchestrator, project: str) -> None:
    store = orch.store
    other = orch.create_agent(project, name="dep", worktree=False).id
    dependency = store.add_run(other, "running elsewhere")
    store.update_run(dependency.id, status=RunStatus.RUNNING)
    for _ in range(10):
        agent = orch.create_agent(project, name="w", worktree=False).id
        # Each may start on its own: the message (its runner checks dependencies before
        # taking the turn) and the notification (the message is not ready).
        message_run = store.add_run(agent, "m", depends_on=[dependency.id])
        note = store.add_run(agent, "n", kind=NOTIFICATION)
        barrier, results = threading.Barrier(2), {}
        threads = [
            threading.Thread(target=_take, args=(barrier, results, run, kind, Store(store.path)))
            for run, kind in ((message_run, "message"), (note, NOTIFICATION))
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        assert sorted(results.values()) == [False, True]
        running_runs = [r for r in store.active_runs(agent) if r.status is RunStatus.RUNNING]
        assert len(running_runs) == 1


def test_runs_queued_by_older_code_keep_old_runners_free_of_deadlock(
    orch: Orchestrator, project: str
) -> None:
    agent = orch.create_agent(project, name="w", worktree=False).id
    store = orch.store

    def legacy() -> int:
        with sqlite3.connect(store.path) as conn:
            cursor = conn.execute(
                "INSERT INTO runs (agent_id, prompt, status, created_at)"
                " VALUES (?, 'old', 'waiting', 't')",
                (agent,),
            )
        conn.close()
        return cursor.lastrowid

    earlier = legacy()
    note = store.add_run(agent, "n", kind=NOTIFICATION)
    with store.connection() as conn:
        # An old runner starts an earlier run without checking: the notification lets it.
        assert not store.may_start(conn, note.id, agent, NOTIFICATION)
    store.finish_run(earlier, RunStatus.DONE)
    legacy()  # its old runner waits for the notification: the notification must not wait
    with store.connection() as conn:
        assert store.may_start(conn, note.id, agent, NOTIFICATION)


# -- wait_for_agents -----------------------------------------------------------


def test_wait_returns_at_once_when_a_message_for_the_caller_is_waiting(
    orch: Orchestrator, project: str, as_agent: Callable, monkeypatch: pytest.MonkeyPatch
) -> None:
    parent = lead(orch, project)
    child = busy_agent(orch, project, monkeypatch)
    orch.store.add_run(parent, "a queued message")  # never launched: it stays waiting

    as_agent(parent)
    started = time.monotonic()
    reply = wait(child, timeout_seconds=30)

    assert time.monotonic() - started < 2
    assert reply["finished"] is False and reply["note"] == mcp_server.MESSAGE_NOTE
    assert reply["agents"][0]["id"] == child and "last_run" not in reply["agents"][0]
    as_agent(None)
    orch.stop(child)


def test_wait_returns_when_a_message_arrives_during_it(
    orch: Orchestrator, project: str, as_agent: Callable, monkeypatch: pytest.MonkeyPatch
) -> None:
    parent = lead(orch, project)
    child = busy_agent(orch, project, monkeypatch)
    timer = threading.Timer(0.5, lambda: orch.store.add_run(parent, "arrives meanwhile"))
    timer.start()

    as_agent(parent)
    started = time.monotonic()
    reply = wait(child, timeout_seconds=30)
    timer.join()

    assert 0.5 <= time.monotonic() - started < 3
    assert reply["finished"] is False and reply["note"] == mcp_server.MESSAGE_NOTE
    as_agent(None)
    orch.stop(child)


def test_a_message_blocked_on_a_dependency_does_not_end_the_wait(
    orch: Orchestrator, project: str, as_agent: Callable, monkeypatch: pytest.MonkeyPatch
) -> None:
    parent = lead(orch, project)
    gate_id = gate(orch, project)
    child = busy_agent(orch, project, monkeypatch)
    orch.store.add_run(parent, "blocked message", depends_on=[gate_id])

    as_agent(parent)
    started = time.monotonic()
    reply = wait(child, timeout_seconds=1)

    assert time.monotonic() - started >= 1
    assert reply["finished"] is False and "note" not in reply
    as_agent(None)
    orch.stop(child)


def test_wait_waits_for_the_descendants(
    orch: Orchestrator, project: str, as_agent: Callable
) -> None:
    parent = lead(orch, project)
    brief = orch.send(parent, "brief")
    gate_id = gate(orch, project)
    worker = spawn(as_agent, parent, "work", after=[str(gate_id)])
    until(lambda: ended(orch, brief.id), what="the lead's turn")

    early = wait(parent, timeout_seconds=1)
    open_gate(orch, gate_id)
    reply = wait(parent, timeout_seconds=30)

    assert early["finished"] is False
    assert early["agents"][0]["status"] == "done" and early["agents"][0]["children_running"] == 1
    assert reply["finished"] and not orch.store.active_runs(worker)
    assert reply["agents"][0]["last_run"]["id"] == notifications(orch, parent)[0].id


def test_an_agent_cannot_wait_for_itself_or_those_above_it(
    orch: Orchestrator, project: str, as_agent: Callable
) -> None:
    top = lead(orch, project, "top")
    middle = lead(orch, project, "middle", parent=top)
    as_agent(middle)

    for agent_id in (middle, top):
        with pytest.raises(OrchestratorError, match="an agent above you"):
            wait(agent_id, timeout_seconds=1)


# -- the text -----------------------------------------------------------------


def test_the_text_stays_within_its_limits(orch: Orchestrator, project: str) -> None:
    store = orch.store
    parent = lead(orch, project)
    kids = []
    for n in range(6):
        kid = orch.create_agent(project, name=f"k{n}", worktree=False, parent_id=parent).id
        for m in range(2):
            run = store.add_run(kid, "task", sender=parent)
            store.finish_run(run.id, RunStatus.DONE, result=f"{n}{m}" + "x" * 9000)
        kids.append(kid)

    queued = notify.queue_if_due(store, parent)
    again = notify.queue_if_due(store, parent)
    started = notify.start(store, queued)

    assert again is None and started.status is RunStatus.RUNNING
    assert len(started.prompt) <= notify.MESSAGE_CHARS
    data = body(started)
    shown = data["agents"]
    assert shown and data["not_shown"]
    assert [a["id"] for a in shown] + [a["id"] for a in data["not_shown"]] == kids
    first = shown[0]["last_run"]
    assert len(first["result"]) < notify.AGENT_CHARS + 200
    assert f"get_agent run_id={first['id']}" in first["result"]
    assert len(shown[0]["earlier_run_ids"]) == 1
    assert "count as delivered without being shown" in data["note"]
    # Everything listed counts as delivered: no second notification.
    store.finish_run(started.id, RunStatus.DONE)
    assert notify.queue_if_due(store, parent) is None


def test_a_large_fan_out_stays_within_the_limit(orch: Orchestrator, project: str) -> None:
    store = orch.store
    parent = lead(orch, project)
    with store.connection() as conn:  # 400 children: half finished, half still queued
        for n in range(400):
            kid = f"kid-with-a-long-name-{n:04d}"
            conn.execute(
                "INSERT INTO agents (id, project, parent_id, name, role, provider, cwd,"
                " created_at) VALUES (?, ?, ?, 'k', 'worker', 'fake', '/w', 't')",
                (kid, project, parent),
            )
            run_id = store.insert_run(conn, kid, "task", sender=parent)
            if n % 2 == 0:
                conn.execute(
                    "UPDATE runs SET status = 'done', result = ? WHERE id = ?",
                    ("r" * 3000, run_id),
                )

    started = notify.start(store, notify.queue_if_due(store, parent))

    assert len(started.prompt) <= notify.MESSAGE_CHARS
    data = body(started)
    assert len(data["still_running"]) == notify.LISTED_AGENTS
    assert data["still_running_more"] == 200 - notify.LISTED_AGENTS
    assert len(data["agents"]) + len(data["not_shown"]) + data["not_shown_more"] == 200
    assert "list_agents" in data["note"]


def test_a_notification_with_nothing_left_is_deleted_without_a_turn(
    orch: Orchestrator, project: str
) -> None:
    store = orch.store
    parent = lead(orch, project)
    _, run_id = finished_child(orch, project, parent)
    queued = notify.queue_if_due(store, parent)
    store.claim(parent, [run_id])  # e.g. by wait_for_agents

    assert notify.start(store, queued) is None
    assert store.runs_of(parent) == [] and store.meta_of(parent) == {}


def test_result_references_in_a_notification_are_left_alone(
    orch: Orchestrator, project: str
) -> None:
    parent = lead(orch, project)
    other = orch.create_agent(project, name="o", worktree=False).id
    secret = orch.store.add_run(other, "x")
    orch.store.finish_run(secret.id, RunStatus.DONE, result="SECRET")
    child, run_id = finished_child(orch, project, parent, f"see {{{{result:{other}}}}}")

    orch.run_ended(run_id, child)
    (note,) = until(lambda: finished_notes(orch, parent), what="the notification")

    assert f"{{{{result:{other}}}}}" in note.result and "SECRET" not in note.result


# -- leads, views and old databases ------------------------------------------------


def test_the_headless_orchestrator_adopts_a_lead_without_parent(
    orch: Orchestrator, project: str, as_agent: Callable, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(Orchestrator, "_launch", lambda self, run: None)
    old_lead = orch.lead(project)  # created before the orchestrator agent existed
    orch.orchestrator()

    as_agent(None)
    mcp_server.ask_project_lead(project, "from the interactive session")
    assert orch.agent(old_lead.id).parent_id is None
    as_agent(ORCHESTRATOR_ID)
    reply = mcp_server.ask_project_lead(project, "from the orchestrator")

    assert orch.agent(old_lead.id).parent_id == ORCHESTRATOR_ID
    assert reply["id"] == old_lead.id and reply["next"] == mcp_server.NEXT["each"]


def test_views_show_running_descendants_and_a_capped_queue(
    orch: Orchestrator, project: str
) -> None:
    top = lead(orch, project, "top")
    middle = lead(orch, project, "middle", parent=top)
    orch.store.add_run(middle, "never launched")
    for _ in range(24):
        orch.store.add_run(middle, "y" * 300, sender=top)

    top_view = orch.view(orch.agent(top))
    detail = orch.view(orch.agent(middle), detail=True)

    assert top_view["children_running"] == 1 and "queue" not in top_view
    assert detail["children_running"] == 0 and detail["queued_runs"] == 25
    assert len(detail["queue"]) == service.QUEUE_LISTED
    first, second = detail["queue"][:2]
    assert (first["kind"], first["sender"], first["prompt"]) == ("message", None, "never launched")
    assert (second["sender"], len(second["prompt"])) == (top, 120)
    assert set(first) == {"id", "kind", "sender", "created_at", "prompt"}


def test_an_old_database_gets_run_metadata(tmp_path: Path) -> None:
    path = tmp_path / "old.db"
    with sqlite3.connect(path) as conn:
        conn.executescript(OLD_SCHEMA)
    conn.close()

    store = Store(path)
    new = store.add_run("a-1", "three", sender="a-1")

    assert store.meta(1).known is False and store.meta(1).kind == "message"
    assert (store.meta(new.id).sender, store.meta(new.id).origin) == ("a-1", "a-1")
    view = Orchestrator(store).view(store.get_agent("a-1"), detail=True)
    assert view["children_running"] == 0 and [q["id"] for q in view["queue"]] == [new.id]
    with sqlite3.connect(path) as conn:
        assert [row[1] for row in conn.execute("PRAGMA table_info(runs)")] == OLD_RUN_COLUMNS
    conn.close()


# -- follow-ups of a failed notification, lost triggers, first-version databases -----------


def test_a_released_result_comes_back_after_the_agents_next_successful_message(
    orch: Orchestrator, project: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    launched: list[int] = []
    monkeypatch.setattr(Orchestrator, "_launch", lambda self, run: launched.append(run.id))
    parent = lead(orch, project)
    child, run_id = finished_child(orch, project, parent)
    failed = notify.start(orch.store, notify.queue_if_due(orch.store, parent))
    orch.store.finish_run(failed.id, RunStatus.FAILED, error="rate limited")

    orch.run_ended(failed.id, parent)  # its own end never checks the agent again
    assert notifications(orch, parent) == [orch.store.get_run(failed.id)] and launched == []
    later = orch.store.add_run(parent, "a later message from the user")
    orch.store.finish_run(later.id, RunStatus.DONE, result="ok")
    orch.run_ended(later.id, parent)

    (again,) = [run for run in notifications(orch, parent) if run.id != failed.id]
    assert launched == [again.id]
    assert [kid.id for kid in notify.reportable(_children(orch, parent))] == [child]


def _children(orch: Orchestrator, parent: str) -> list[notify.Child]:
    with orch.store.connection() as conn:
        return notify.children(conn, parent)


def test_an_unchecked_run_with_a_reused_pid_is_repaired_after_a_while(
    orch: Orchestrator, project: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(Orchestrator, "_launch", lambda self, run: None)
    parent = lead(orch, project)
    child = orch.create_agent(project, name="c", worktree=False, parent_id=parent).id
    recent, old = (orch.store.add_run(child, "task", sender=parent) for _ in range(2))
    alive = os.getpid()  # a live process that is not the run's runner
    orch.store.finish_run(recent.id, RunStatus.DONE, result="r", pid=alive)
    orch.store.update_run(old.id, status="done", pid=alive, ended_at="2000-01-01T00:00:00+00:00")

    orch.store.reconcile()

    assert not orch.store.meta(recent.id).checked  # its runner may still be at it
    assert orch.store.meta(old.id).checked and len(notifications(orch, parent)) == 1


FIRST_RUN_META = """
CREATE TABLE run_meta (run_id INTEGER PRIMARY KEY, agent_id TEXT NOT NULL, kind TEXT NOT NULL,
    sender TEXT, notify TEXT NOT NULL DEFAULT 'each');
INSERT INTO agents (id, project, parent_id, name, role, provider, cwd, created_at)
    VALUES ('lead-1', 'p', NULL, 'lead', 'lead', 'claude', '/w', 't');
UPDATE agents SET parent_id = 'lead-1' WHERE id = 'a-1';
INSERT INTO run_meta VALUES (1, 'a-1', 'message', 'lead-1', 'each'),
    (2, 'a-1', 'message', 'lead-1', 'each');
"""


def test_a_first_version_run_meta_is_completed_without_waking_anyone(tmp_path: Path) -> None:
    path = tmp_path / "old.db"
    with sqlite3.connect(path) as conn:
        conn.executescript(OLD_SCHEMA + FIRST_RUN_META)
    conn.close()
    script = "import sys; from orchestrator.store import Store; Store(sys.argv[1])"
    procs = [subprocess.Popen([sys.executable, "-c", script, str(path)]) for _ in range(4)]
    assert [proc.wait() for proc in procs] == [0] * 4

    store = Store(path)
    launched: list[Run] = []
    orch = Orchestrator(store)
    orch._launch = launched.append  # type: ignore[method-assign]
    store.reconcile()
    new = store.add_run("a-1", "three", sender="lead-1")

    assert launched == [] and store.runs_of("lead-1") == []
    assert [store.meta(run_id).checked for run_id in (1, 2, new.id)] == [1, 1, 0]
    assert store.meta(new.id).origin == "lead-1"
    with sqlite3.connect(path) as conn:
        names = [row[1] for row in conn.execute("PRAGMA table_info(run_meta)")]
        indexes = [row[1] for row in conn.execute("PRAGMA index_list(run_meta)")]
    conn.close()
    assert names[-4:] == ["origin", "cause_run", "claimed", "checked"]
    assert "run_meta_unchecked" in indexes
