"""Parents stay reachable while their agents work and are notified when they finish."""

from __future__ import annotations

import functools
import json
import sqlite3
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import anyio
import pytest
from test_store import OLD_RUN_COLUMNS, OLD_SCHEMA

from orchestrator import mcp_server, notify
from orchestrator.models import Run, RunStatus
from orchestrator.service import Orchestrator
from orchestrator.store import NOTIFICATION, Store


def until(predicate: Callable[[], Any], timeout: float = 40, what: str = "condition") -> Any:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if value := predicate():
            return value
        time.sleep(0.1)
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


def body(run: Run) -> dict[str, Any]:
    assert run.prompt.startswith(notify.HEADER)
    return json.loads(run.prompt[len(notify.HEADER) :])


def settle(orch: Orchestrator, *agent_ids: str, timeout: float = 60) -> None:
    assert orch.wait_idle(list(agent_ids), timeout), "agents did not settle in time"


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


# -- the reported situation --------------------------------------------------


def test_a_user_message_runs_while_the_child_works_and_the_result_follows(
    orch: Orchestrator, project: str, as_agent: Callable
) -> None:
    parent = lead(orch, project)
    child = spawn(as_agent, parent, "SLEEP 4 child-work")

    message = orch.send(parent, "hello from the user")
    until(lambda: not orch.store.get_run(message.id).status.active, what="the user message")
    child_run = orch.store.last_run(child)
    assert child_run.status.active, "the child finished before the user message"
    settle(orch, parent)

    (note,) = notifications(orch, parent)
    child_run = orch.store.get_run(child_run.id)
    assert orch.store.get_run(message.id).ended_at <= child_run.ended_at <= note.started_at
    assert note.status is RunStatus.DONE and note.output_schema is None
    assert len(note.prompt.splitlines()[0]) and note.prompt.count("\n") <= 3
    (entry,) = body(note)["agents"]
    assert (entry["id"], entry["status"]) == (child, "done")
    assert entry["last_run"] == {
        "id": child_run.id,
        "status": "done",
        "result": "echo[new]: SLEEP 4 child-work",
    }
    events = [event.kind for event in orch.store.events(parent) if event.run_id == note.id]
    assert events[0] == "notification"
    time.sleep(1)  # the finished notification itself wakes nobody
    assert len(notifications(orch, parent)) == 1 and not orch.store.active_runs(parent)


def test_the_dispatch_reply_says_whether_the_caller_will_be_woken(
    orch: Orchestrator, project: str, as_agent: Callable
) -> None:
    parent, other = lead(orch, project), lead(orch, project, "other")
    as_agent(parent)
    mine = mcp_server.spawn_agent("x", worktree=False, notify="all")
    to_other = mcp_server.send_message(other, "y")
    as_agent(None)
    interactive = mcp_server.spawn_agent("z", project=project, worktree=False)

    assert mine["notify"] == "all"
    assert "notify" not in to_other and "notify" not in interactive
    assert orch.store.meta(mine["run_id"]).sender == parent
    assert orch.store.meta(interactive["run_id"]).sender is None
    settle(orch, parent, other, interactive["id"])


# -- coalescing --------------------------------------------------------------


def test_children_finishing_within_the_settle_delay_share_one_notification(
    orch: Orchestrator, project: str, as_agent: Callable, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ORCH_NOTIFY_DELAY", "4")
    parent = lead(orch, project)
    first = spawn(as_agent, parent, "SLEEP 0.2 a")
    second = spawn(as_agent, parent, "SLEEP 1 b")

    settle(orch, parent)

    (note,) = notifications(orch, parent)
    assert [agent["id"] for agent in body(note)["agents"]] == [first, second]


def test_completions_while_the_parent_is_busy_arrive_as_one_notification(
    orch: Orchestrator, project: str, as_agent: Callable
) -> None:
    parent = lead(orch, project)
    busy = orch.send(parent, "SLEEP 3 busy")
    until(lambda: orch.store.get_run(busy.id).status is RunStatus.RUNNING, what="the busy turn")
    first = spawn(as_agent, parent, "one")
    second = spawn(as_agent, parent, "two")
    until(lambda: not orch.store.active_runs(first) and not orch.store.active_runs(second))
    assert orch.store.get_run(busy.id).status is RunStatus.RUNNING

    settle(orch, parent)

    (note,) = notifications(orch, parent)
    assert [agent["id"] for agent in body(note)["agents"]] == [first, second]
    assert note.started_at >= orch.store.get_run(busy.id).ended_at


def test_notify_all_waits_until_every_child_is_settled(
    orch: Orchestrator, project: str, as_agent: Callable
) -> None:
    parent = lead(orch, project)
    fast = spawn(as_agent, parent, "fast", notify="all")
    slow = spawn(as_agent, parent, "SLEEP 3 slow", notify="all")
    until(lambda: not orch.store.active_runs(fast), what="the fast child")
    time.sleep(1)
    assert notifications(orch, parent) == [] and orch.store.active_runs(slow)

    settle(orch, parent)

    (note,) = notifications(orch, parent)
    assert [agent["id"] for agent in body(note)["agents"]] == [fast, slow]


def test_notify_each_wakes_the_parent_after_the_first_child(
    orch: Orchestrator, project: str, as_agent: Callable
) -> None:
    parent = lead(orch, project)
    fast = spawn(as_agent, parent, "fast")
    slow = spawn(as_agent, parent, "SLEEP 4 slow")

    first = until(lambda: notifications(orch, parent), what="the first notification")[0]
    assert orch.store.active_runs(slow)
    settle(orch, parent)

    first, second = notifications(orch, parent)
    assert [agent["id"] for agent in body(first)["agents"]] == [fast]
    assert body(first)["still_running"] == [slow]
    assert [agent["id"] for agent in body(second)["agents"]] == [slow]


# -- no duplicates -------------------------------------------------------------


def test_a_result_claimed_by_wait_for_agents_is_not_notified(
    orch: Orchestrator, project: str, as_agent: Callable
) -> None:
    parent = lead(orch, project)
    busy = orch.send(parent, "SLEEP 4 busy")
    until(lambda: orch.store.get_run(busy.id).status is RunStatus.RUNNING, what="the busy turn")
    child = spawn(as_agent, parent, "quick")

    as_agent(parent)
    reply = wait(child, timeout_seconds=30)
    as_agent(None)
    queued = until(lambda: notifications(orch, parent), what="the queued notification")

    assert reply["finished"] and reply["agents"][0]["last_run"]["result"] == "echo[new]: quick"
    assert queued[0].status is RunStatus.WAITING
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
        conn.execute(
            "INSERT INTO runs (agent_id, prompt, status, created_at) VALUES (?, 'old', 'done', 't')",
            (child,),
        )
    conn.close()
    by_user = orch.store.add_run(child, "from the user")
    orch.store.finish_run(by_user.id, RunStatus.DONE, result="r")

    orch.notify_parents(child)

    assert orch.store.runs_of(parent) == []
    assert orch.wait_idle([parent], 2)


def test_stop_agent_by_the_parent_counts_as_delivered(
    orch: Orchestrator, project: str, as_agent: Callable
) -> None:
    parent = lead(orch, project)
    busy = orch.send(parent, "SLEEP 3 busy")
    child = spawn(as_agent, parent, "SLEEP 60")
    until(lambda: orch.store.last_run(child).status is RunStatus.RUNNING, what="the child")

    as_agent(parent)
    assert mcp_server.stop_agent(child) == {"stopped_runs": 1}
    as_agent(None)
    settle(orch, parent)

    assert [run.id for run in orch.store.runs_of(parent)] == [busy.id]


# -- hierarchy ----------------------------------------------------------------


def test_a_grandchild_wakes_the_lead_and_then_the_lead_wakes_its_parent(
    orch: Orchestrator, project: str, as_agent: Callable
) -> None:
    top = lead(orch, project, "top")
    middle = lead(orch, project, "middle", parent=top)
    as_agent(top)
    brief = mcp_server.send_message(middle, "SLEEP 0.3 brief")
    as_agent(None)
    worker = spawn(as_agent, middle, "SLEEP 3 work")
    until(lambda: not orch.store.get_run(brief["run_id"]).status.active, what="the brief")
    time.sleep(0.5)
    assert orch.store.runs_of(top) == []  # the lead is not settled: its worker runs

    settle(orch, top)

    (lead_note,) = notifications(orch, middle)
    (top_note,) = notifications(orch, top)
    assert [agent["id"] for agent in body(lead_note)["agents"]] == [worker]
    (entry,) = body(top_note)["agents"]
    assert entry["id"] == middle and entry["last_run"]["id"] == lead_note.id
    assert entry["last_run"]["result"].startswith("echo[resumed]: [orch notification]")
    assert entry["earlier_run_ids"] == [brief["run_id"]]
    assert top_note.started_at >= lead_note.ended_at


def test_failed_skipped_and_stopped_child_runs_are_notified(
    orch: Orchestrator, project: str, as_agent: Callable
) -> None:
    parent = lead(orch, project)
    failing = spawn(as_agent, parent, "FAIL", notify="all")
    skipped = spawn(as_agent, parent, "never", after=[failing], notify="all")
    stopped = spawn(as_agent, parent, "SLEEP 60", notify="all")
    until(lambda: orch.store.last_run(stopped).status is RunStatus.RUNNING, what="the sleeper")
    orch.stop(stopped)

    settle(orch, parent)

    (note,) = notifications(orch, parent)
    statuses = {agent["id"]: agent["last_run"]["status"] for agent in body(note)["agents"]}
    assert statuses == {failing: "failed", skipped: "skipped", stopped: "stopped"}
    errors = {agent["id"]: agent["last_run"].get("error") for agent in body(note)["agents"]}
    assert errors[failing] == "boom" and errors[stopped] == "stopped"


# -- turn order ---------------------------------------------------------------


def test_a_user_message_goes_ahead_of_a_waiting_notification(
    orch: Orchestrator, project: str, as_agent: Callable
) -> None:
    parent = lead(orch, project)
    busy = orch.send(parent, "SLEEP 3 busy")
    until(lambda: orch.store.get_run(busy.id).status is RunStatus.RUNNING, what="the busy turn")
    spawn(as_agent, parent, "quick")
    (queued,) = until(lambda: notifications(orch, parent), what="the queued notification")
    message = orch.send(parent, "from the user")
    assert queued.id < message.id

    settle(orch, parent)

    starts = {
        event.run_id: event.id
        for event in orch.store.events(parent)
        if event.kind in ("prompt", "notification")
    }
    assert starts[busy.id] < starts[message.id] < starts[queued.id]


def test_a_message_blocked_on_a_dependency_does_not_hold_a_notification_back(
    orch: Orchestrator, project: str, as_agent: Callable
) -> None:
    parent = lead(orch, project)
    blocker = orch.spawn(project, "SLEEP 6 blocker", worktree=False)[0].id
    blocked = orch.send(parent, "after the blocker", after=[blocker])
    spawn(as_agent, parent, "quick")

    note = until(
        lambda: [n for n in notifications(orch, parent) if n.status is RunStatus.DONE],
        what="the notification",
    )[0]

    assert orch.store.active_runs(blocker)
    assert orch.store.get_run(blocked.id).status is RunStatus.WAITING
    assert note.id > blocked.id
    settle(orch, parent, blocker)


def test_runners_started_at_once_never_run_one_agent_twice(
    orch: Orchestrator, project: str
) -> None:
    agent = orch.create_agent(project, name="w", worktree=False).id
    runs = [orch.store.add_run(agent, f"SLEEP 0.3 run-{n}") for n in range(5)]
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
        message = store.add_run(agent, "m", depends_on=[dependency.id])
        note = store.add_run(agent, "n", kind=NOTIFICATION)
        barrier, results = threading.Barrier(2), {}
        threads = [
            threading.Thread(target=_take, args=(barrier, results, run, kind, Store(store.path)))
            for run, kind in ((message, "message"), (note, NOTIFICATION))
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        assert sorted(results.values()) == [False, True]
        running = [r for r in store.active_runs(agent) if r.status is RunStatus.RUNNING]
        assert len(running) == 1


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
    orch: Orchestrator, project: str, as_agent: Callable
) -> None:
    parent = lead(orch, project)
    child = spawn(as_agent, parent, "SLEEP 8 slow")
    orch.store.add_run(parent, "a queued message")  # never launched: it stays waiting

    as_agent(parent)
    started = time.monotonic()
    reply = wait(child, timeout_seconds=30)

    assert time.monotonic() - started < 3
    assert reply["finished"] is False and reply["note"] == mcp_server.MESSAGE_NOTE
    assert reply["agents"][0]["id"] == child and "last_run" not in reply["agents"][0]
    as_agent(None)
    orch.stop(child)


def test_wait_returns_when_a_message_arrives_during_it(
    orch: Orchestrator, project: str, as_agent: Callable
) -> None:
    parent = lead(orch, project)
    child = spawn(as_agent, parent, "SLEEP 15 slow")
    timer = threading.Timer(1.5, lambda: orch.store.add_run(parent, "arrives meanwhile"))
    timer.start()

    as_agent(parent)
    started = time.monotonic()
    reply = wait(child, timeout_seconds=30)
    timer.join()

    assert 1.5 <= time.monotonic() - started < 5
    assert reply["finished"] is False and reply["note"] == mcp_server.MESSAGE_NOTE
    as_agent(None)
    orch.stop(child)


def test_wait_waits_for_the_descendants(
    orch: Orchestrator, project: str, as_agent: Callable
) -> None:
    parent = lead(orch, project)
    brief = orch.send(parent, "brief")
    worker = spawn(as_agent, parent, "SLEEP 3 work")
    until(lambda: not orch.store.get_run(brief.id).status.active, what="the lead's turn")

    early = wait(parent, timeout_seconds=1)
    reply = wait(parent, timeout_seconds=60)

    assert early["finished"] is False
    assert early["agents"][0]["status"] == "done" and early["agents"][0]["children_running"] == 1
    assert reply["finished"] and not orch.store.active_runs(worker)
    assert reply["agents"][0]["last_run"]["id"] == notifications(orch, parent)[0].id


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
    # Everything listed counts as delivered: no second notification.
    store.finish_run(started.id, RunStatus.DONE)
    assert notify.queue_if_due(store, parent) is None


def test_a_notification_with_nothing_left_is_deleted_without_a_turn(
    orch: Orchestrator, project: str
) -> None:
    store = orch.store
    parent = lead(orch, project)
    kid = orch.create_agent(project, name="k", worktree=False, parent_id=parent).id
    run = store.add_run(kid, "task", sender=parent)
    store.finish_run(run.id, RunStatus.DONE, result="r")
    queued = notify.queue_if_due(store, parent)
    store.claim(parent, [run.id])  # e.g. by wait_for_agents

    assert notify.start(store, queued) is None
    assert store.runs_of(parent) == [] and store.meta_of(parent) == {}


# -- views and old databases ------------------------------------------------------


def test_views_show_running_descendants_and_the_queue(
    orch: Orchestrator, project: str, as_agent: Callable
) -> None:
    top = lead(orch, project, "top")
    middle = lead(orch, project, "middle", parent=top)
    orch.store.add_run(middle, "never launched")
    as_agent(top)
    mcp_server.send_message(middle, "y" * 300)
    as_agent(None)

    top_view = orch.view(orch.agent(top))
    detail = orch.view(orch.agent(middle), detail=True)

    assert top_view["children_running"] == 1 and "queue" not in top_view
    assert detail["children_running"] == 0
    first, second = detail["queue"]
    assert (first["kind"], first["sender"], first["prompt"]) == ("message", None, "never launched")
    assert (second["sender"], len(second["prompt"])) == (top, 120)
    assert set(first) == {"id", "kind", "sender", "created_at", "prompt"}
    orch.stop(middle)


def test_an_old_database_gets_run_metadata(tmp_path: Path) -> None:
    path = tmp_path / "old.db"
    with sqlite3.connect(path) as conn:
        conn.executescript(OLD_SCHEMA)
    conn.close()

    store = Store(path)
    new = store.add_run("a-1", "three", sender="a-1")

    assert store.meta(1).known is False and store.meta(1).kind == "message"
    assert store.meta(new.id).sender == "a-1"
    view = Orchestrator(store).view(store.get_agent("a-1"), detail=True)
    assert view["children_running"] == 0 and [q["id"] for q in view["queue"]] == [new.id]
    with sqlite3.connect(path) as conn:
        assert [row[1] for row in conn.execute("PRAGMA table_info(runs)")] == OLD_RUN_COLUMNS
    conn.close()
