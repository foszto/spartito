"""Withdrawing a queued message, or replacing its text, before it starts."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from typing import Any

import click
import pytest
from starlette.testclient import TestClient
from typer.testing import CliRunner

from orchestrator import cli, mcp_server, runner
from orchestrator.models import RunStatus
from orchestrator.service import Orchestrator, OrchestratorError
from orchestrator.store import MESSAGE, NOTIFICATION, Store, pid_alive
from orchestrator.web.app import app

ORIGIN = {"origin": "http://localhost"}


def until(predicate: Callable[[], Any], timeout: float = 30) -> Any:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if value := predicate():
            return value
        time.sleep(0.05)
    raise AssertionError("timed out")


def finished(orch: Orchestrator, *agent_ids: str) -> list[dict]:
    done, views = orch.wait(list(agent_ids), timeout=30)
    assert done, "agents did not finish in time"
    return views


def running(orch: Orchestrator, agent_id: str) -> None:
    until(lambda: orch.store.running_run(agent_id))


def test_a_withdrawn_message_never_runs_and_the_others_go_on(
    orch: Orchestrator, project: str
) -> None:
    lead = orch.create_agent(project, name="boss", role="lead", worktree=False)
    agent, first = orch.spawn(project, "SLEEP 1.5 first", worktree=False, parent_id=lead.id)
    second = orch.send(agent.id, "second", sender=lead.id)
    third = orch.send(agent.id, "third")
    other, dependent = orch.spawn(project, "dependent", worktree=False, after=[second.id])
    running(orch, agent.id)
    runner_pid = until(lambda: orch.store.get_run(second.id).pid)

    withdrawn = orch.withdraw(second.id)

    assert withdrawn.id == second.id and withdrawn.prompt == "second"
    assert orch.store.get_run(second.id) is None
    until(lambda: not pid_alive(runner_pid), timeout=5)  # it noticed at its next poll
    assert orch.store.get_run(first.id).status is RunStatus.RUNNING  # untouched
    view, other_view = finished(orch, agent.id, other.id)
    runs = orch.store.runs_of(agent.id)
    assert [(run.id, run.status, run.result) for run in runs] == [
        (first.id, RunStatus.DONE, "echo[new]: SLEEP 1.5 first"),
        (third.id, RunStatus.DONE, "echo[resumed]: third"),
    ]
    assert view["last_run"]["id"] == third.id
    assert other_view["status"] == "skipped"
    assert "no longer exists" in orch.store.get_run(dependent.id).error
    events = [e for e in orch.store.events(agent.id) if e.run_id == second.id]
    assert [(e.kind, e.text) for e in events] == [
        ("info", f"queued message {second.id} withdrawn: second")
    ]
    # Its sender (the parent) hears nothing: what it sent is gone, the rest was the user's.
    finished(orch, lead.id)
    assert orch.store.runs_of(lead.id) == []


def test_only_a_queued_message_can_be_withdrawn_or_edited(orch: Orchestrator, project: str) -> None:
    agent, first = orch.spawn(project, "SLEEP 30", worktree=False)
    try:
        running(orch, agent.id)
        notice = orch.store.add_run(agent.id, "[orch notification] pending", kind=NOTIFICATION)
        for act in (orch.withdraw, lambda run_id: orch.replace_message(run_id, "new")):
            with pytest.raises(OrchestratorError, match="has already started"):
                act(first.id)
            with pytest.raises(OrchestratorError, match="is a notification"):
                act(notice.id)
            with pytest.raises(OrchestratorError, match="no run 9999"):
                act(9999)
    finally:
        orch.stop(agent.id)
    assert orch.store.get_run(first.id).status is RunStatus.STOPPED
    with pytest.raises(OrchestratorError, match=r"has already ended \(stopped\)"):
        orch.withdraw(first.id)
    with pytest.raises(OrchestratorError, match="is empty"):
        orch.replace_message(first.id, "  ")


def test_the_agent_receives_the_replaced_text(orch: Orchestrator, project: str) -> None:
    agent, _ = orch.spawn(project, "SLEEP 1 first", worktree=False)
    queued = orch.send(agent.id, "old text")
    running(orch, agent.id)

    replaced = orch.replace_message(queued.id, "new text")

    assert replaced.prompt == "new text"
    finished(orch, agent.id)
    run = orch.store.get_run(queued.id)
    assert (run.status, run.result) == (RunStatus.DONE, "echo[resumed]: new text")
    prompts = [e.text for e in orch.store.events(agent.id) if e.run_id == queued.id]
    assert prompts[0] == "new text"


def test_a_runner_waiting_for_a_dependency_ends_when_its_run_is_withdrawn(
    orch: Orchestrator, project: str
) -> None:
    dep_agent, dep = orch.spawn(project, "SLEEP 4 dep", worktree=False)
    agent, waiting = orch.spawn(project, "never", worktree=False, after=[dep.id])
    try:
        running(orch, dep_agent.id)
        runner_pid = until(lambda: orch.store.get_run(waiting.id).pid)
        time.sleep(1)  # the runner is in its poll loop, waiting for the dependency

        orch.withdraw(waiting.id)

        until(lambda: not pid_alive(runner_pid), timeout=3)
        # It ended on its own, not because the dependency did.
        assert orch.store.get_run(dep.id).status is RunStatus.RUNNING
    finally:
        orch.stop(dep_agent.id)
    assert orch.store.runs_of(agent.id) == []
    events = orch.store.events(agent.id)
    assert [(e.run_id, e.kind) for e in events] == [(waiting.id, "info")]


def test_an_edit_committed_just_before_the_turn_is_taken_reaches_the_agent(
    orch: Orchestrator, project: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    agent = orch.create_agent(project, worktree=False)
    queued = orch.store.add_run(agent.id, "old text")  # no runner launched: the test is it
    stale = orch.store.get_run(queued.id)  # what a runner read when it started
    take = orch.store.take_turn

    def edit_then_take(run: Any, kind: str) -> bool | None:
        orch.replace_message(queued.id, "new text")
        monkeypatch.setattr(orch.store, "take_turn", take)
        return take(run, kind)

    monkeypatch.setattr(orch.store, "take_turn", edit_then_take)

    runner.take_turn(orch.store, stale, MESSAGE)

    done = orch.store.get_run(queued.id)
    assert (done.status, done.result) == (RunStatus.DONE, "echo[new]: new text")
    prompts = [e.text for e in orch.store.events(agent.id) if e.kind == "prompt"]
    assert prompts == ["new text"]


@pytest.mark.parametrize("first", ["turn", "change"])
@pytest.mark.parametrize("action", ["withdraw", "replace"])
def test_a_change_and_the_turn_starting_at_once_are_serialised(
    orch: Orchestrator, project: str, monkeypatch: pytest.MonkeyPatch, action: str, first: str
) -> None:
    """`first` is paused inside its write transaction; the other one, started meanwhile,
    waits for that lock and then sees the outcome."""
    agent = orch.create_agent(project, worktree=False)
    run = orch.store.add_run(agent.id, "queued")  # no runner: this test takes the turn
    inside, release = threading.Event(), threading.Event()

    def hold(original: Callable[..., Any]) -> Callable[..., Any]:
        def held(*args: Any, **kwargs: Any) -> Any:
            value = original(*args, **kwargs)
            inside.set()
            assert release.wait(10)
            return value

        return held

    if first == "turn":
        monkeypatch.setattr(Store, "start_run", staticmethod(hold(Store.start_run)))
    else:
        monkeypatch.setattr(Store, "_queued_message", hold(Store._queued_message))
    result: dict[str, Any] = {}

    def change() -> None:
        try:
            if action == "withdraw":
                orch.withdraw(run.id)
            else:
                orch.replace_message(run.id, "replaced")
            result["changed"] = True
        except OrchestratorError as exc:
            result["changed"] = str(exc)

    def take() -> None:
        result["started"] = orch.store.take_turn(run)

    leader = threading.Thread(target=take if first == "turn" else change)
    leader.start()
    assert inside.wait(10)
    follower = threading.Thread(target=change if first == "turn" else take)
    follower.start()
    time.sleep(0.3)
    assert follower.is_alive()  # waits for the leader's write lock
    release.set()
    leader.join(10)
    follower.join(10)

    stored = orch.store.get_run(run.id)
    if first == "turn":
        assert result["started"] is True and "has already started" in result["changed"]
        assert (stored.status, stored.prompt) == (RunStatus.RUNNING, "queued")
    elif action == "withdraw":
        assert result == {"changed": True, "started": None} and stored is None
    else:
        assert result == {"changed": True, "started": True}
        assert (stored.status, stored.prompt) == (RunStatus.RUNNING, "replaced")


@pytest.fixture
def as_agent(monkeypatch: pytest.MonkeyPatch) -> Callable[[str | None], None]:
    def act(agent_id: str | None) -> None:
        if agent_id:
            monkeypatch.setenv("ORCH_AGENT_ID", agent_id)
        else:
            monkeypatch.delenv("ORCH_AGENT_ID", raising=False)

    return act


def test_mcp_withdraw_message_acts_only_on_the_callers_own_messages(
    orch: Orchestrator, project: str, as_agent: Callable[[str | None], None]
) -> None:
    lead = orch.create_agent(project, name="boss", role="lead", worktree=False)
    peer = orch.create_agent(project, name="peer", role="lead", worktree=False)
    worker = orch.create_agent(project, worktree=False, parent_id=lead.id)
    mine = orch.store.add_run(worker.id, "mine", sender=lead.id)
    theirs = orch.store.add_run(worker.id, "theirs", sender=peer.id)
    users = orch.store.add_run(worker.id, "the user's")

    as_agent(lead.id)
    for run in (theirs, users):
        with pytest.raises(OrchestratorError, match=f"no run {run.id} sent by you"):
            mcp_server.withdraw_message(run.id)
        with pytest.raises(OrchestratorError, match="sent by you"):
            mcp_server.withdraw_message(run.id, replacement="hijack")
    assert mcp_server.withdraw_message(mine.id, replacement="mine, v2") == {
        "replaced": mine.id,
        "agent_id": worker.id,
    }
    assert orch.store.get_run(mine.id).prompt == "mine, v2"
    assert mcp_server.withdraw_message(mine.id) == {"withdrawn": mine.id, "agent_id": worker.id}
    assert orch.store.get_run(mine.id) is None

    as_agent(None)  # the interactive session: any run
    assert mcp_server.withdraw_message(theirs.id)["withdrawn"] == theirs.id
    assert [run.prompt for run in orch.store.runs_of(worker.id)] == ["the user's"]
    doc = mcp_server.withdraw_message.__doc__ or ""
    assert len(doc.strip().splitlines()) == 2


def test_cli_withdraws_and_edits_queued_runs(orch: Orchestrator, project: str) -> None:
    agent = orch.create_agent(project, worktree=False)
    first = orch.store.add_run(agent.id, "first")
    second = orch.store.add_run(agent.id, "second")
    notice = orch.store.add_run(agent.id, "[orch notification] pending", kind=NOTIFICATION)

    edited = CliRunner().invoke(cli.app, ["edit", str(first.id), "first, reworded"])
    withdrawn = CliRunner().invoke(cli.app, ["withdraw", str(second.id)])
    refused = CliRunner().invoke(cli.app, ["withdraw", str(notice.id)])
    gone = CliRunner().invoke(cli.app, ["edit", str(second.id), "too late"])

    assert edited.exit_code == 0, edited.output
    assert edited.output.strip() == f"replaced the text of run {first.id} of {agent.id}"
    assert withdrawn.exit_code == 0, withdrawn.output
    assert withdrawn.output.strip() == f"withdrew run {second.id} of {agent.id}"
    assert refused.exit_code == 1 and "is a notification" in click.unstyle(refused.output)
    assert gone.exit_code == 1 and f"no run {second.id}" in click.unstyle(gone.output)
    status = click.unstyle(CliRunner().invoke(cli.app, ["status", agent.id]).output)
    assert f"#{first.id} " in status and "first, reworded" in status
    assert f"#{second.id} " not in status


def test_api_withdraws_and_edits_queued_runs(orch: Orchestrator, project: str) -> None:
    client = TestClient(app, base_url="http://localhost")
    agent, first = orch.spawn(project, "SLEEP 30", worktree=False)
    try:
        running(orch, agent.id)
        queued = orch.send(agent.id, "a long brief " * 20)
        other = orch.send(agent.id, "to withdraw")

        assert client.post(f"/api/runs/{other.id}/withdraw", json={}).status_code == 403
        fetched = client.get(f"/api/runs/{queued.id}").json()
        edited = client.post(
            f"/api/runs/{queued.id}/edit", json={"message": "short now"}, headers=ORIGIN
        )
        withdrawn = client.post(f"/api/runs/{other.id}/withdraw", json={}, headers=ORIGIN)
        started = client.post(f"/api/runs/{first.id}/withdraw", json={}, headers=ORIGIN)
        empty = client.post(f"/api/runs/{queued.id}/edit", json={"message": ""}, headers=ORIGIN)
        missing = client.get("/api/runs/9999")
        detail = client.get(f"/api/agents/{agent.id}/events").json()["agent"]
    finally:
        orch.stop(agent.id)

    assert fetched["prompt"] == "a long brief " * 20 and fetched["kind"] == "message"
    assert edited.json() == {"agent_id": agent.id, "run_id": queued.id}
    assert withdrawn.json() == {"withdrawn": other.id, "agent_id": agent.id}
    assert started.status_code == 400 and "has already started" in started.json()["error"]
    assert empty.status_code == 400 and "'message' is required" in empty.json()["error"]
    assert missing.status_code == 400
    assert [(run["id"], run["prompt"]) for run in detail["queue"]] == [(queued.id, "short now")]
