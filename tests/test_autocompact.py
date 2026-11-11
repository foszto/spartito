"""Automatic compaction at a context threshold (settings.compact_at, Orchestrator.auto_compact)."""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

import pytest
from starlette.testclient import TestClient
from typer.testing import CliRunner

from orchestrator import cli, config, mcp_server, notify, runner, settings
from orchestrator.core.compaction import LEAD_KEEP
from orchestrator.models import Run, RunStatus, Usage
from orchestrator.providers.codex import COMPACT_PROMPT
from orchestrator.service import Orchestrator, compaction_left
from orchestrator.settings import COMPACT_AT, compact_tokens
from orchestrator.store import COMPACT, MESSAGE, Store
from orchestrator.web.app import app

ORIGIN = {"origin": "http://localhost"}


def set_threshold(orch: Orchestrator, value: object, project: str | None = None) -> None:
    data = orch.settings(project)
    data[COMPACT_AT] = value
    orch.update_settings(data, project)


def settle(orch: Orchestrator, agent_id: str) -> dict:
    """Wait until the agent is idle: its turn and an automatic compaction after it."""
    done, (view,) = orch.wait([agent_id], timeout=30)
    assert done, "agent did not finish in time"
    return view


def compaction_runs(orch: Orchestrator, agent_id: str) -> list[Run]:
    ids = orch.store.compactions(agent_id)
    return [run for run in orch.store.runs_of(agent_id) if run.id in ids]


def prompt_of(run_id: int) -> str:
    return json.loads((config.run_dir(run_id) / "ctx.json").read_text())["prompt"]


def infos(orch: Orchestrator, agent_id: str) -> list[str]:
    return [e.text for e in orch.store.events(agent_id) if e.kind == "info"]


def started(orch: Orchestrator, agent_id: str) -> list[int]:
    return [e.run_id for e in orch.store.events(agent_id) if e.kind == "prompt"]


# -- when it is queued ---------------------------------------------------------------


def test_a_turn_above_the_threshold_queues_a_compaction_before_the_next_turn(
    orch: Orchestrator, project: str
) -> None:
    set_threshold(orch, "2k")
    agent, turn = orch.spawn(project, "CONTEXT 5000", worktree=False)

    view = settle(orch, agent.id)

    (compaction,) = compaction_runs(orch, agent.id)
    assert compaction.id > turn.id and compaction.status is RunStatus.DONE
    assert orch.store.meta(compaction.id).auto == 1
    assert prompt_of(compaction.id) == "/compact"  # a worker: the CLI's default summary
    assert "automatic compaction: context 5k is above the threshold of 2k" in infos(orch, agent.id)
    assert started(orch, agent.id) == [turn.id, compaction.id]
    # Housekeeping: the agent's answer and status stay those of its turn.
    assert view["status"] == "done" and view["last_run"]["id"] == turn.id
    detail = orch.view(orch.agent(agent.id), detail=True)
    assert detail["last_compaction"]["automatic"] is True
    assert detail["context"]["compacted"]
    assert (detail["context"]["compact_at"], detail["context"]["compact_paused"]) == (2000, None)


@pytest.mark.parametrize(
    ("global_at", "project_at", "queued"),
    [
        ("6k", None, False),  # below the threshold
        ("off", None, False),
        (0, None, False),
        (None, None, False),  # the default, 256k
        ("2k", "10k", False),  # the project's value overrides the global one
        ("off", 2000, True),
    ],
)
def test_the_threshold_decides_and_a_project_overrides_it(
    orch: Orchestrator, project: str, global_at: object, project_at: object, queued: bool
) -> None:
    set_threshold(orch, global_at)
    if project_at is not None:
        set_threshold(orch, project_at, project)
    agent, _ = orch.spawn(project, "CONTEXT 5000", worktree=False)

    settle(orch, agent.id)

    assert len(compaction_runs(orch, agent.id)) == (1 if queued else 0)


def test_no_automatic_compaction_after_a_failed_turn(orch: Orchestrator, project: str) -> None:
    set_threshold(orch, "2k")
    agent, turn = orch.spawn(project, "FAIL CONTEXT 5000", worktree=False)

    settle(orch, agent.id)

    assert orch.store.get_run(turn.id).status is RunStatus.FAILED
    assert compaction_runs(orch, agent.id) == []


def done_turn(
    orch: Orchestrator, agent_id: str, usage: Usage, kind: str = MESSAGE, at: str = "00"
) -> Run:
    run = orch.store.add_run(agent_id, "x", kind=kind)
    orch.store.update_run(run.id, status="running", started_at=f"2026-01-01T00:00:{at}+00:00")
    orch.store.set_usage(run.id, agent_id, usage)
    orch.store.finish_run(run.id, RunStatus.DONE, result="ok")
    return orch.store.get_run(run.id)


def test_never_after_a_compaction_nor_while_one_is_queued(
    orch: Orchestrator, project: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    launched: list[int] = []
    monkeypatch.setattr(Orchestrator, "_launch", lambda self, run: launched.append(run.id))
    set_threshold(orch, "2k")
    agent = orch.create_agent(project, worktree=False)
    orch.store.set_session(agent.id, "s")
    turn = done_turn(orch, agent.id, Usage(context_tokens=5000, session_id="s"))

    first = orch.auto_compact(turn.id)

    assert first is not None and launched == [first.id]
    assert orch.auto_compact(turn.id) is None  # one is queued already
    manual = done_turn(orch, agent.id, Usage(context_tokens=5000, session_id="s"), COMPACT)
    assert orch.auto_compact(manual.id) is None  # never after a compaction run
    assert launched == [first.id]


def test_an_agent_that_cannot_be_compacted_is_skipped_quietly(
    orch: Orchestrator, project: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(Orchestrator, "_launch", lambda self, run: None)
    set_threshold(orch, "2k")
    agent = orch.create_agent(project, worktree=False)
    orch.store.set_session(agent.id, "s")
    turn = done_turn(orch, agent.id, Usage(context_tokens=5000, session_id="s"))
    monkeypatch.setattr(Orchestrator, "compact_refusal", lambda self, agent: "no way")

    assert orch.auto_compact(turn.id) is None
    assert compaction_runs(orch, agent.id) == [] and infos(orch, agent.id) == []


# -- not twice in a row ----------------------------------------------------------------


def paused_notes(orch: Orchestrator, agent_id: str) -> list[tuple[int, str]]:
    return [
        (e.run_id, e.text)
        for e in orch.store.events(agent_id)
        if e.kind == "info" and e.text.startswith("automatic compaction paused")
    ]


def paused_text(agent_id: str, left: str, threshold: str) -> str:
    return (
        f"automatic compaction paused: the last compaction left the context at {left}, above"
        f" the threshold of {threshold}; a compaction that gets it below (orch compact"
        f" {agent_id}) or a higher threshold resumes it"
    )


def test_a_threshold_below_the_fixed_overhead_compacts_once_then_pauses(
    orch: Orchestrator, project: str
) -> None:
    """The review's case: system prompt and tools alone are above the threshold, while the
    CLI reports a small size after compacting (the fake: 901). Judged by what the next
    turn's first request measured, not by that figure: one compaction, then paused."""
    set_threshold(orch, "20k")
    agent, _ = orch.spawn(project, "CONTEXT 25000", worktree=False)
    settle(orch, agent.id)
    (compaction,) = compaction_runs(orch, agent.id)
    assert orch.view(orch.agent(agent.id))["context"]["compact_paused"] is None  # not yet known

    second = orch.send(agent.id, "CONTEXT 25000 CONTEXT 26000")  # first request, end
    view = settle(orch, agent.id)
    third = orch.send(agent.id, "CONTEXT 26000 CONTEXT 27000")
    settle(orch, agent.id)

    assert compaction_runs(orch, agent.id) == [compaction]  # never again
    assert paused_notes(orch, agent.id) == [(second.id, paused_text(agent.id, "25k", "20k"))]
    assert view["context"]["compact_paused"] == 25000
    assert orch.store.get_usage(third.id).first_context_tokens == 26000
    shown = CliRunner().invoke(cli.app, ["status", agent.id]).output
    assert "automatic compaction paused: the last compaction left 25k" in shown

    set_threshold(orch, "30k")  # a higher threshold resumes it
    assert orch.view(orch.agent(agent.id))["context"]["compact_paused"] is None
    orch.send(agent.id, "CONTEXT 27000 CONTEXT 31000")
    settle(orch, agent.id)
    assert len(compaction_runs(orch, agent.id)) == 2


def test_a_conversation_that_grows_again_after_an_effective_compaction_is_compacted_again(
    orch: Orchestrator, project: str
) -> None:
    set_threshold(orch, "2k")
    agent, _ = orch.spawn(project, "CONTEXT 5000", worktree=False)
    settle(orch, agent.id)
    orch.send(agent.id, "CONTEXT 1000 CONTEXT 1500")  # what the compaction left: 1000
    settle(orch, agent.id)
    assert len(compaction_runs(orch, agent.id)) == 1

    orch.send(agent.id, "CONTEXT 1500 CONTEXT 3000")  # grown above it again
    settle(orch, agent.id)

    assert len(compaction_runs(orch, agent.id)) == 2
    assert paused_notes(orch, agent.id) == []


def test_codex_without_a_size_after_compacting_pauses_on_the_next_turn_above(
    orch: Orchestrator, project: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Codex reports neither a size after compacting nor a first request: the size at the
    end of the next turn decides (conservative)."""
    monkeypatch.setattr(Orchestrator, "_launch", lambda self, run: None)
    set_threshold(orch, "20k")
    agent = orch.create_agent(project, worktree=False, provider="codex")
    orch.store.set_session(agent.id, "s")
    turn = done_turn(orch, agent.id, Usage(context_tokens=30_000, session_id="s"))
    compaction = orch.auto_compact(turn.id)
    assert compaction is not None
    orch.store.update_run(compaction.id, started_at="2026-01-01T00:00:01+00:00")
    record = Usage(compact_trigger="auto", compact_pre_tokens=30_000, session_id="s")
    orch.store.set_usage(compaction.id, agent.id, record)
    orch.store.finish_run(compaction.id, RunStatus.DONE, result="compacted 30k -> ? tokens")
    after = done_turn(orch, agent.id, Usage(context_tokens=24_000, session_id="s"), at="02")

    assert orch.auto_compact(after.id) is None
    assert paused_notes(orch, agent.id) == [(after.id, paused_text(agent.id, "24k", "20k"))]
    later = done_turn(orch, agent.id, Usage(context_tokens=25_000, session_id="s"), at="03")
    assert orch.auto_compact(later.id) is None
    assert len(paused_notes(orch, agent.id)) == 1  # said once


def test_a_compaction_that_never_started_pauses_nothing(
    orch: Orchestrator, project: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(Orchestrator, "_launch", lambda self, run: None)
    set_threshold(orch, "20k")
    agent = orch.create_agent(project, worktree=False)
    orch.store.set_session(agent.id, "s")
    turn = done_turn(orch, agent.id, Usage(context_tokens=30_000, session_id="s"))
    stopped = orch.auto_compact(turn.id)
    orch.store.finish_run(stopped.id, RunStatus.STOPPED, error="stopped")  # while queued
    after = done_turn(orch, agent.id, Usage(context_tokens=31_000, session_id="s"), at="02")

    assert orch.auto_compact(after.id) is not None


def test_what_the_last_compaction_left() -> None:
    def run(n: int, status: RunStatus = RunStatus.DONE, started: bool = True) -> Run:
        when = f"t{n:02}" if started else None
        return Run(id=n, agent_id="a", prompt="x", status=status, started_at=when)

    def measure(end: int | None, first: int | None = None, **more: object) -> Usage:
        return Usage(context_tokens=end, first_context_tokens=first, session_id="s", **more)

    runs, usage = [run(1)], {1: measure(5000)}
    assert compaction_left(runs, usage, set(), "s") is None
    runs.append(run(2, RunStatus.FAILED))
    assert compaction_left(runs, usage, {2}, "s") == (None, 2)  # nothing measured it yet
    runs.append(run(3))  # the turn after it: its first request tells
    usage[3] = measure(7000, first=6000)
    assert compaction_left(runs, usage, {2}, "s") == (6000, 3)
    runs.append(run(4))  # a compaction that measured the size after compacting itself
    usage[4] = measure(800, compact_trigger="manual", compact_post_tokens=100)
    assert compaction_left(runs, usage, {2, 4}, "s") == (800, 4)
    runs.append(run(5))  # the CLI compacted on its own in a turn; a turn without a first
    usage[5] = measure(9000, first=9500, compact_trigger="auto", compact_post_tokens=300)
    runs.append(run(6))  # request (Codex) tells by its end
    usage[6] = measure(1200)
    assert compaction_left(runs, usage, {2, 4}, "s") == (1200, 6)
    runs.append(run(7, RunStatus.STOPPED, started=False))  # never started: compacted nothing
    runs.append(run(8, RunStatus.WAITING, started=False))  # queued: nothing yet
    assert compaction_left(runs, usage, {2, 4, 7, 8}, "s") == (1200, 6)
    assert compaction_left(runs, usage, {2, 4}, "other") is None  # another session
    runs.append(run(9))  # a new session: what came before it no longer counts
    usage[9] = Usage(context_tokens=50, session_id="new")
    assert compaction_left(runs, usage, {2, 4, 7, 8}, "new") is None


# -- order with queued messages --------------------------------------------------------


@pytest.mark.parametrize("fails", [False, True])
def test_it_runs_ahead_of_a_queued_message_and_the_message_still_runs(
    orch: Orchestrator, project: str, monkeypatch: pytest.MonkeyPatch, fails: bool
) -> None:
    if fails:
        monkeypatch.setenv("FAKE_COMPACT_NOOP", "1")
    set_threshold(orch, "2k")
    agent, turn = orch.spawn(project, "SLEEP 1.5 CONTEXT 5000", worktree=False)
    while orch.store.get_run(turn.id).status is not RunStatus.RUNNING:
        time.sleep(0.05)
    message = orch.send(agent.id, "after")

    settle(orch, agent.id)

    (compaction,) = compaction_runs(orch, agent.id)
    assert message.id < compaction.id  # queued after the message, it ran before it
    assert started(orch, agent.id) == [turn.id, compaction.id, message.id]
    expected = RunStatus.FAILED if fails else RunStatus.DONE
    assert orch.store.get_run(compaction.id).status is expected
    done = orch.store.get_run(message.id)
    assert (done.status, done.result) == (RunStatus.DONE, "echo[resumed]: after")
    if fails:  # it left the context as it was, above the threshold: paused, no loop
        orch.send(agent.id, "CONTEXT 6000")
        settle(orch, agent.id)
        assert compaction_runs(orch, agent.id) == [orch.store.get_run(compaction.id)]
        assert orch.view(orch.agent(agent.id))["context"]["compact_paused"] == 6000


def test_store_order_an_automatic_compaction_goes_first_and_never_holds_a_message(
    tmp_path: Path,
) -> None:
    store = Store(tmp_path / "db")
    store.add_project(_project())
    store.add_agent(_agent())
    message = store.add_run("a", "queued first")
    compaction = store.add_auto_compaction("a", "/compact")
    assert compaction is not None and store.add_auto_compaction("a", "/compact") is None

    with store.connection() as conn:
        assert store.may_start(conn, compaction.id, "a", COMPACT)
        assert not store.may_start(conn, message.id, "a", MESSAGE)
    store.withdraw_queued(compaction.id)  # withdrawn: the message goes on
    with store.connection() as conn:
        assert store.may_start(conn, message.id, "a", MESSAGE)

    again = store.add_auto_compaction("a", "/compact")
    store.finish_run(again.id, RunStatus.FAILED, error="boom")  # failed: likewise
    with store.connection() as conn:
        assert store.may_start(conn, message.id, "a", MESSAGE)
    # A manual compaction keeps its place behind the message.
    manual = store.add_run("a", "/compact", kind=COMPACT)
    with store.connection() as conn:
        assert not store.may_start(conn, manual.id, "a", COMPACT)


def _project():  # noqa: ANN202
    from orchestrator.models import Project

    return Project(name="p", path="/repo", base_branch="main")


def _agent():  # noqa: ANN202
    from orchestrator.models import Agent

    return Agent(id="a", project="p", name="a", role="worker", provider="fake", cwd="/w")


# -- keep-instructions -------------------------------------------------------------------


def test_a_lead_keeps_its_rules_and_open_work_a_worker_gets_the_default_summary(
    orch: Orchestrator, project: str
) -> None:
    set_threshold(orch, "2k")
    lead = orch.lead(project)
    orch.send(lead.id, "CONTEXT 5000")
    settle(orch, lead.id)
    worker, _ = orch.spawn(project, "CONTEXT 5000", worktree=False)
    settle(orch, worker.id)

    (lead_compaction,) = compaction_runs(orch, lead.id)
    (worker_compaction,) = compaction_runs(orch, worker.id)
    assert prompt_of(lead_compaction.id) == f"/compact {LEAD_KEEP}"
    assert prompt_of(worker_compaction.id) == "/compact"
    assert len(LEAD_KEEP) < 300


def test_a_codex_lead_compacts_without_instructions(orch: Orchestrator, project: str) -> None:
    lead = orch.create_agent(project, role="lead", provider="codex", worktree=False)
    assert orch.compact_prompt(lead, None) == COMPACT_PROMPT


# -- answers still reach the parent ------------------------------------------------------


def test_the_parent_gets_the_childs_answer_not_its_automatic_compaction(
    orch: Orchestrator, project: str
) -> None:
    set_threshold(orch, "2k")
    lead = orch.create_agent(project, name="boss", role="lead", worktree=False)
    child, work = orch.spawn(
        project, "CONTEXT 5000 work", worktree=False, parent_id=lead.id, sender=lead.id
    )
    settle(orch, child.id)
    settle(orch, lead.id)

    (compaction,) = compaction_runs(orch, child.id)
    assert compaction.status is RunStatus.DONE
    (notice,) = [r for r in orch.store.runs_of(lead.id) if r.prompt.startswith("[orch notif")]
    body = json.loads(notice.prompt[len(notify.HEADER) :])
    (entry,) = body["agents"]
    assert entry["last_run"]["id"] == work.id
    assert entry["last_run"]["result"] == "echo[new]: CONTEXT 5000 work"
    assert "compact" not in json.dumps(body)


def test_wait_for_agents_shows_an_answer_behind_a_queued_compaction(
    orch: Orchestrator, project: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A caller that never got a run of the child, while a compaction of it is queued:
    the child's answer is delivered with its text, not as already delivered."""
    monkeypatch.setattr(Orchestrator, "_launch", lambda self, run: None)
    child = orch.create_agent(project, worktree=False)
    answer = orch.store.add_run(child.id, "task")
    orch.store.finish_run(answer.id, RunStatus.DONE, result="the answer")
    orch.store.mark_checked(answer.id)
    compaction = orch.store.add_auto_compaction(child.id, "/compact")

    assert mcp_server._delivery_floor(orch, "someone", child.id) == answer.id
    orch.store.finish_run(compaction.id, RunStatus.DONE, result="compacted")
    orch.store.mark_checked(compaction.id)
    monkeypatch.setenv("ORCH_AGENT_ID", "someone")
    reply = mcp_server._wait(orch, [child.id], timeout=5, limit=4000)
    (outcome,) = reply["agents"]
    assert outcome["last_run"]["id"] == answer.id
    assert outcome["last_run"]["result"] == "the answer"


# -- settings -------------------------------------------------------------------------------


def test_threshold_values() -> None:
    assert [compact_tokens(v) for v in ("256000", "256k", "1.5M", "off", "0", 300, " 2K ")] == [
        256_000,
        256_000,
        1_500_000,
        0,
        0,
        300,
        2000,
    ]
    assert compact_tokens("") is None and compact_tokens(None) is None
    for bad in ("-1", "abc", True, "1.5", "inf", "nan", "k"):
        with pytest.raises(ValueError, match=COMPACT_AT):
            compact_tokens(bad)
    assert settings.clean({COMPACT_AT: "300k"}) == {COMPACT_AT: 300_000}
    assert settings.clean({COMPACT_AT: ""}) == {}


def test_an_unreadable_stored_value_switches_it_off(orch: Orchestrator) -> None:
    orch.store.set_settings(settings.GLOBAL, {COMPACT_AT: "lots"})
    assert settings.compact_at(orch.store, None) == 0


def test_a_settings_save_without_the_key_keeps_it(orch: Orchestrator, project: str) -> None:
    set_threshold(orch, "100k")
    set_threshold(orch, "50k", project)

    orch.update_settings({"worker": {"claude": {"model": "haiku"}}})  # an older page's save
    orch.update_settings({}, project)

    assert orch.settings()[COMPACT_AT] == 100_000
    assert orch.settings(project)[COMPACT_AT] == 50_000
    orch.update_settings({COMPACT_AT: ""}, project)  # named and empty: cleared
    assert COMPACT_AT not in orch.settings(project)
    assert settings.compact_at(orch.store, project) == 100_000


def test_the_cli_sets_and_shows_the_threshold(orch: Orchestrator, project: str) -> None:
    runner = CliRunner()
    shown = runner.invoke(cli.app, ["config", "show"]).output
    assert "compact automatically above 256,000 tokens of context (default)" in shown

    project_set = runner.invoke(cli.app, ["config", "compact-at", "300k", "-p", project])
    off = runner.invoke(cli.app, ["config", "compact-at", "off"])
    bad = runner.invoke(cli.app, ["config", "compact-at", "lots"])

    assert project_set.exit_code == 0
    assert "compact automatically above 300,000 tokens of context" in project_set.output
    assert off.exit_code == 0 and "compact automatically off (never)" in off.output
    assert bad.exit_code != 0 and COMPACT_AT in bad.output
    assert settings.compact_at(orch.store, project) == 300_000
    assert settings.compact_at(orch.store, None) == 0


def test_the_dashboard_gets_the_threshold_and_saves_it(orch: Orchestrator, project: str) -> None:
    client = TestClient(app, base_url="http://localhost")
    state = client.get("/api/state").json()
    assert (state["compact_at_key"], state["compact_at_default"]) == (COMPACT_AT, 256_000)

    saved = client.post(
        "/api/settings", json={"project": project, "data": {COMPACT_AT: "64k"}}, headers=ORIGIN
    )
    refused = client.post("/api/settings", json={"data": {COMPACT_AT: "-5"}}, headers=ORIGIN)

    assert saved.status_code == 200 and orch.settings(project)[COMPACT_AT] == 64_000
    assert refused.status_code == 400


# -- the database ----------------------------------------------------------------------------


def test_an_existing_run_meta_gets_the_auto_column_and_old_inserts_still_work(
    tmp_path: Path,
) -> None:
    path = tmp_path / "old.db"
    with sqlite3.connect(path) as conn:
        conn.executescript(
            "CREATE TABLE run_meta (run_id INTEGER PRIMARY KEY, agent_id TEXT NOT NULL,"
            " kind TEXT NOT NULL, sender TEXT, notify TEXT NOT NULL DEFAULT 'each', origin TEXT,"
            " cause_run INTEGER, claimed TEXT, checked INTEGER NOT NULL DEFAULT 0);"
            " INSERT INTO run_meta (run_id, agent_id, kind) VALUES (7, 'a', 'compact');"
        )
    conn.close()

    store = Store(path)

    assert store.meta(7).auto == 0  # an existing compaction was a manual one
    with store.connection() as conn:  # what older code inserts
        conn.execute(
            "INSERT INTO run_meta (run_id, agent_id, kind, sender, notify, origin, cause_run)"
            " VALUES (8, 'a', 'message', NULL, 'each', NULL, NULL)"
        )
    assert store.meta(8).auto == 0
    Store(path)  # opening it again changes nothing


# -- a stop at the end of the turn ----------------------------------------------------------


def test_a_stop_while_queueing_goes_through_and_leaves_no_compaction(
    orch: Orchestrator, project: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    set_threshold(orch, "20k")
    agent = orch.create_agent(project, worktree=False)
    orch.store.set_session(agent.id, "s")
    turn = done_turn(orch, agent.id, Usage(context_tokens=30_000, session_id="s"))

    def stopped(self: Orchestrator, run: Run) -> None:
        raise runner.Stopped

    monkeypatch.setattr(Orchestrator, "_launch", stopped)  # the stop arrives while launching
    with pytest.raises(runner.Stopped):
        runner.queue_auto_compaction(orch.store, turn.id)
    assert compaction_runs(orch, agent.id) == []

    monkeypatch.setattr(Orchestrator, "_launch", lambda self, run: None)
    runner.queue_auto_compaction(orch.store, turn.id)  # queued; then the stop arrives
    (queued,) = compaction_runs(orch, agent.id)
    assert runner.auto_queued == [queued.id]
    runner.withdraw_auto_queued(orch.store)  # what take_turn does on a stop
    assert compaction_runs(orch, agent.id) == [] and runner.auto_queued == []


def test_other_errors_while_queueing_never_fail_the_turn(
    orch: Orchestrator, project: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(self: Orchestrator, run_id: int) -> None:
        raise RuntimeError("database is locked")

    monkeypatch.setattr(Orchestrator, "auto_compact", broken)
    runner.queue_auto_compaction(orch.store, 1)  # no exception
    assert runner.auto_queued == []
