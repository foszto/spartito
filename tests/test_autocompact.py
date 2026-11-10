"""Automatic compaction at a context threshold (settings.compact_at, Orchestrator.auto_compact)."""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

import pytest
from starlette.testclient import TestClient
from typer.testing import CliRunner

from orchestrator import cli, config, mcp_server, notify, settings
from orchestrator.models import Run, RunStatus, Usage
from orchestrator.providers.codex import COMPACT_PROMPT
from orchestrator.service import LEAD_KEEP, Orchestrator, compaction_left
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


def done_turn(orch: Orchestrator, agent_id: str, usage: Usage, kind: str = MESSAGE) -> Run:
    run = orch.store.add_run(agent_id, "x", kind=kind)
    orch.store.update_run(run.id, status="running", started_at="2026-01-01T00:00:00+00:00")
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


def test_a_compaction_that_leaves_the_context_above_the_threshold_pauses_it(
    orch: Orchestrator, project: str
) -> None:
    set_threshold(orch, 500)  # the fake compaction leaves 901 tokens
    agent, _ = orch.spawn(project, "CONTEXT 780", worktree=False)
    settle(orch, agent.id)
    (compaction,) = compaction_runs(orch, agent.id)
    paused = [text for text in infos(orch, agent.id) if text.startswith("automatic compaction p")]
    assert paused == [
        "automatic compaction paused: the context is 901 after this compaction, above the"
        f" threshold of 500; a compaction that gets it below (orch compact {agent.id}) or a"
        " higher threshold resumes it"
    ]

    orch.send(agent.id, "CONTEXT 800")
    view = settle(orch, agent.id)

    assert compaction_runs(orch, agent.id) == [compaction]  # not again
    assert view["context"]["compact_paused"] == 901
    shown = CliRunner().invoke(cli.app, ["status", agent.id]).output
    assert "automatic compaction paused: the last compaction left 901" in shown

    set_threshold(orch, 1000)  # a higher threshold resumes it
    assert orch.view(orch.agent(agent.id))["context"]["compact_paused"] is None
    orch.send(agent.id, "CONTEXT 1200")
    settle(orch, agent.id)
    assert len(compaction_runs(orch, agent.id)) == 2


def test_what_the_last_compaction_left() -> None:
    def run(n: int, status: RunStatus = RunStatus.DONE) -> Run:
        return Run(id=n, agent_id="a", prompt="x", status=status, started_at=f"t{n:02}")

    turn = Usage(context_tokens=5000, session_id="s")
    runs, usage = [run(1)], {1: turn}
    assert compaction_left(runs, usage, set(), "s") is None
    runs.append(run(2, RunStatus.FAILED))  # a failed compaction left the size before it
    assert compaction_left(runs, usage, {2}, "s") == 5000
    runs.append(run(3))  # a manual one that worked: what the CLI reported
    usage[3] = Usage(compact_trigger="manual", compact_post_tokens=100, session_id="s")
    assert compaction_left(runs, usage, {2, 3}, "s") == 100
    runs.append(run(4))
    usage[4] = Usage(context_tokens=9000, compact_trigger="auto", compact_post_tokens=300)
    usage[4].session_id = "s"  # the CLI compacted on its own in a later turn
    assert compaction_left(runs, usage, {2, 3}, "s") == 300
    runs.append(run(5, RunStatus.WAITING))  # a queued one has left nothing yet
    assert compaction_left(runs, usage, {2, 3, 5}, "s") == 300


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
        assert orch.view(orch.agent(agent.id))["context"]["compact_paused"] == 5000


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
