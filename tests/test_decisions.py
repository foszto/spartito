"""Decisions the owner is asked for (decisions.py) and desktop notifications (desktop.py).

The fake osascript of conftest.py stands in for the real one; `desktop.PLATFORM` decides
whether this process counts as macOS. Runners are processes of their own: tests that need
their notifications run on macOS only.
"""

from __future__ import annotations

import sqlite3
import sys
import time
from collections.abc import Callable
from pathlib import Path

import pytest
from starlette.testclient import TestClient
from test_autocompact import compaction_runs, set_threshold
from test_notifications import lead, notifications, running, settle, until
from test_store import OLD_RUN_COLUMNS, OLD_SCHEMA, columns
from typer.testing import CliRunner

from orchestrator import cli, config, decisions, desktop, mcp_server, settings
from orchestrator.models import RunStatus
from orchestrator.service import ORCHESTRATOR_ID, Orchestrator, OrchestratorError
from orchestrator.store import MESSAGE, NOTIFICATION, Store
from orchestrator.web.app import app

ORIGIN = {"origin": "http://localhost"}
on_macos = pytest.mark.skipif(sys.platform != "darwin", reason="runners decide by the platform")


def calls(log: Path) -> list[list[str]]:
    """The argument lists the fake osascript was called with, in order."""
    if not log.exists():
        return []
    return [line.split("\x1f")[:-1] for line in log.read_text().splitlines()]


def bodies(log: Path) -> list[str]:
    """The texts shown, without the clause that says where to look (desktop.plain), which
    tests of their own check."""
    clauses = (desktop.CLAUSE_DECISION, desktop.CLAUSE_FINISHED)
    return [
        next((call[-1].removesuffix(c) for c in clauses if call[-1].endswith(c)), call[-1])
        for call in calls(log)
    ]


@pytest.fixture
def as_agent(monkeypatch: pytest.MonkeyPatch) -> Callable[[str | None], None]:
    """Make the MCP tools act for an agent (None: the interactive session)."""

    def act(agent_id: str | None) -> None:
        if agent_id:
            monkeypatch.setenv("ORCH_AGENT_ID", agent_id)
        else:
            monkeypatch.delenv("ORCH_AGENT_ID", raising=False)

    return act


@pytest.fixture
def macos(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(desktop, "PLATFORM", "darwin")


def ask(as_agent: Callable, agent_id: str | None, question: str = "Which database?", **kw):
    as_agent(agent_id)
    try:
        return mcp_server.request_decision(question, **kw)
    finally:
        as_agent(None)


def withdraw(as_agent: Callable, agent_id: str | None, decision_id: int):
    as_agent(agent_id)
    try:
        return mcp_server.withdraw_decision(decision_id)
    finally:
        as_agent(None)


# -- the MCP tools -----------------------------------------------------------


def test_request_decision_records_an_open_decision_and_notifies(
    orch: Orchestrator, project: str, as_agent: Callable, fake_osascript: Path, macos: None
) -> None:
    lead_id = lead(orch, project)
    reply = ask(
        as_agent, lead_id, options=["sqlite", "postgres"], recommendation="sqlite: no server"
    )

    assert reply == {"decision_id": reply["decision_id"], "next": mcp_server.DECISION_NEXT}
    assert "user message" in reply["next"] and "does not depend" in reply["next"]
    (item,) = orch.list_decisions()
    assert item["id"] == reply["decision_id"] and item["agent_id"] == lead_id
    assert item["project"] == project and item["state"] == decisions.OPEN
    assert item["question"] == "Which database?" and item["options"] == ["sqlite", "postgres"]
    assert item["recommendation"] == "sqlite: no server" and item["answer"] is None
    assert item["agent_name"] == lead_id and item["agent_state"] is None
    until(lambda: calls(fake_osascript), what="the notification")
    (args,) = calls(fake_osascript)
    assert args == [
        "-e",
        "on run argv",
        "-e",
        "display notification (item 2 of argv) with title (item 1 of argv)",
        "-e",
        "end run",
        config.PROJECT_NAME,
        f"Decision needed ({project}): Which database? — open Spartito to answer",
    ]


def test_the_orchestrators_decision_has_no_project(
    orch: Orchestrator, as_agent: Callable, fake_osascript: Path, macos: None
) -> None:
    orch.orchestrator()
    reply = ask(as_agent, ORCHESTRATOR_ID, "Go ahead?")

    (item,) = orch.list_decisions()
    assert item["project"] is None and item["options"] == [] and item["recommendation"] is None
    assert item["agent_name"] == config.ORCHESTRATOR_NAME and item["id"] == reply["decision_id"]
    until(lambda: calls(fake_osascript), what="the notification")
    assert bodies(fake_osascript) == [f"Decision needed ({config.ORCHESTRATOR_NAME}): Go ahead?"]


@pytest.mark.parametrize(
    ("question", "options", "error"),
    [
        ("  ", None, "question must not be empty"),
        ("x" * 501, None, "question is too long"),
        ("q", [f"o{n}" for n in range(7)], "at most 6 options"),
        ("q", ["a", "a"], "options must differ"),
        ("q", ["a", ""], "option 2 must not be empty"),
        ("q", ["y" * 101], "option 1 is too long"),
        ("q", "a, b", "options must be a list"),
    ],
)
def test_request_decision_validates(
    orch: Orchestrator, project: str, as_agent: Callable, question, options, error
) -> None:
    lead_id = lead(orch, project)
    with pytest.raises(OrchestratorError, match=error):
        ask(as_agent, lead_id, question, options=options)
    with pytest.raises(OrchestratorError, match="recommendation is too long"):
        ask(as_agent, lead_id, "q", recommendation="r" * 1001)
    assert orch.list_decisions(closed=True) == []


def test_the_interactive_session_asks_the_user_directly(
    orch: Orchestrator, as_agent: Callable
) -> None:
    with pytest.raises(OrchestratorError, match="ask them here"):
        ask(as_agent, None)


def test_an_agent_withdraws_only_its_own_open_decisions(
    orch: Orchestrator, project: str, as_agent: Callable
) -> None:
    mine, other = lead(orch, project, "mine"), lead(orch, project, "other")
    first = ask(as_agent, mine)["decision_id"]
    second = ask(as_agent, other)["decision_id"]

    with pytest.raises(OrchestratorError, match=f"you have no decision #{second}"):
        withdraw(as_agent, mine, second)
    with pytest.raises(OrchestratorError, match="you have no decision #99"):
        withdraw(as_agent, mine, 99)
    assert withdraw(as_agent, mine, first) == {"withdrawn": first}
    with pytest.raises(OrchestratorError, match=f"decision #{first} is already withdrawn"):
        withdraw(as_agent, mine, first)

    assert [item["id"] for item in orch.list_decisions()] == [second]
    closed = {item["id"]: item for item in orch.list_decisions(closed=True)}
    assert closed[first]["state"] == decisions.WITHDRAWN and closed[first]["closed_at"]


# -- answering ---------------------------------------------------------------


def test_an_answer_is_a_user_message_to_the_agent_and_closes_the_decision(
    orch: Orchestrator, project: str, as_agent: Callable
) -> None:
    lead_id, other = lead(orch, project), lead(orch, project, "other")
    decision_id = ask(as_agent, lead_id, options=["sqlite", "postgres"])["decision_id"]
    ask(as_agent, other, "Unrelated?")

    result = orch.answer_decision(decision_id, "2")

    run = orch.store.get_run(result["run_id"])
    assert result["agent_id"] == lead_id and run.agent_id == lead_id
    assert run.prompt == f"Decision #{decision_id} — Which database?\nAnswer: postgres"
    meta = orch.store.meta(run.id)
    assert meta.kind == MESSAGE and meta.sender is None
    answered = result["decision"]
    assert answered["state"] == decisions.ANSWERED and answered["answer"] == "postgres"
    assert answered["run_id"] == run.id and answered["closed_at"]
    assert [item["question"] for item in orch.list_decisions()] == ["Unrelated?"]
    with pytest.raises(OrchestratorError, match=f"decision #{decision_id} is already answered"):
        orch.answer_decision(decision_id, "sqlite")
    assert [r.id for r in orch.store.runs_of(lead_id)] == [run.id]
    settle(orch, lead_id)


def test_free_text_and_numbers_beyond_the_options_stay_as_written(
    orch: Orchestrator, project: str, as_agent: Callable
) -> None:
    lead_id = lead(orch, project)
    first = ask(as_agent, lead_id, options=["a", "b"])["decision_id"]
    second = ask(as_agent, lead_id, "How many?")["decision_id"]

    assert (
        orch.answer_decision(first, "  neither, use c ")["decision"]["answer"] == "neither, use c"
    )
    assert orch.answer_decision(second, "3")["decision"]["answer"] == "3"
    with pytest.raises(OrchestratorError, match="answer must not be empty"):
        orch.answer_decision(ask(as_agent, lead_id, "Third?")["decision_id"], " ")
    with pytest.raises(OrchestratorError, match="there is no decision #99"):
        orch.answer_decision(99, "x")
    settle(orch, lead_id)


def test_an_answer_to_an_archived_agent_restores_it(
    orch: Orchestrator, project: str, as_agent: Callable
) -> None:
    lead_id = lead(orch, project)
    decision_id = ask(as_agent, lead_id)["decision_id"]
    orch.store.archive(lead_id)
    assert orch.list_decisions()[0]["agent_state"] == "archived"

    orch.answer_decision(decision_id, "sqlite")

    assert lead_id not in orch.store.archived()
    settle(orch, lead_id)


def test_a_removed_agents_decisions_are_closed(
    orch: Orchestrator, project: str, as_agent: Callable
) -> None:
    removed, gone = lead(orch, project, "removed"), lead(orch, project, "gone")
    by_remove = ask(as_agent, removed)["decision_id"]
    by_old_code = ask(as_agent, gone)["decision_id"]

    orch.store.archive(removed)
    orch.remove(removed)
    orch.store.delete_agent(gone)  # as older code removes it: the decision stays open

    (left,) = orch.list_decisions()
    assert left["id"] == by_old_code and left["agent_state"] == "gone"
    with pytest.raises(OrchestratorError, match=f"agent '{gone}' no longer exists"):
        orch.answer_decision(by_old_code, "sqlite")
    assert orch.list_decisions() == []
    closed = {item["id"]: item for item in orch.list_decisions(closed=True)}
    for decision_id in (by_remove, by_old_code):
        assert closed[decision_id]["state"] == decisions.WITHDRAWN
        assert closed[decision_id]["note"] == decisions.REMOVED
        assert closed[decision_id]["answer"] is None


def test_a_failed_delivery_leaves_the_decision_open(
    orch: Orchestrator, project: str, as_agent: Callable
) -> None:
    lead_id = lead(orch, project)
    decision_id = ask(as_agent, lead_id)["decision_id"]

    def refuse(agent_id: str, text: str) -> int:
        raise RuntimeError("no runner")

    with pytest.raises(RuntimeError):
        decisions.answer(orch.store, decision_id, "x", refuse)
    (item,) = orch.list_decisions()
    assert item["state"] == decisions.OPEN and item["answer"] is None


# -- CLI and API -------------------------------------------------------------


def test_cli_lists_and_answers(orch: Orchestrator, project: str, as_agent: Callable) -> None:
    lead_id = lead(orch, project)
    decision_id = ask(as_agent, lead_id, options=["sqlite", "postgres"], recommendation="sqlite")[
        "decision_id"
    ]
    runner = CliRunner()

    listed = runner.invoke(cli.app, ["decisions"])
    assert listed.exit_code == 0
    assert f"#{decision_id}  {project} · {lead_id} · 0m ago" in listed.output
    assert "Which database?" in listed.output and "2) postgres" in listed.output
    assert "recommends: sqlite" in listed.output

    answered = runner.invoke(cli.app, ["decide", str(decision_id), "1"])
    assert answered.exit_code == 0, answered.output
    assert f"#{decision_id} answered: sqlite -> {lead_id}" in answered.output
    again = runner.invoke(cli.app, ["decide", str(decision_id), "2"])
    assert again.exit_code == 1 and "already answered" in again.output

    assert runner.invoke(cli.app, ["decisions"]).output.strip() == "no open decisions"
    everything = runner.invoke(cli.app, ["decisions", "--all"]).output
    assert "[answered]" in everything and "answer: sqlite" in everything
    settle(orch, lead_id)


def test_api_lists_and_answers(orch: Orchestrator, project: str, as_agent: Callable) -> None:
    client = TestClient(app, base_url="http://localhost")
    lead_id = lead(orch, project)
    decision_id = ask(as_agent, lead_id, options=["sqlite", "postgres"])["decision_id"]

    (item,) = client.get("/api/state").json()["decisions"]
    assert item["id"] == decision_id and item["options"] == ["sqlite", "postgres"]
    assert client.get("/api/decisions").json()["decisions"] == [item]

    path = f"/api/decisions/{decision_id}/answer"
    assert client.post(path, json={"answer": "postgres"}).status_code == 403  # no origin
    answered = client.post(path, json={"answer": "postgres"}, headers=ORIGIN)
    assert answered.status_code == 200
    assert answered.json()["agent_id"] == lead_id
    assert answered.json()["decision"]["answer"] == "postgres"
    again = client.post(path, json={"answer": "sqlite"}, headers=ORIGIN)
    assert again.status_code == 400 and "already answered" in again.json()["error"]

    assert client.get("/api/state").json()["decisions"] == []
    (closed,) = client.get("/api/decisions?all=1").json()["decisions"]
    assert closed["state"] == decisions.ANSWERED
    settle(orch, lead_id)


# -- the setting -------------------------------------------------------------


def test_the_setting_is_global_on_by_default_and_kept_by_the_dialog(orch: Orchestrator) -> None:
    key = settings.DESKTOP_NOTIFICATIONS
    assert settings.desktop_notifications(orch.store)

    result = CliRunner().invoke(cli.app, ["config", "desktop-notifications", "off"])
    assert result.exit_code == 0 and "desktop notifications: off" in result.output
    assert orch.settings()[key] is False and not settings.desktop_notifications(orch.store)
    orch.update_settings({"worker": {}})  # the settings dialog of a project-less save
    assert orch.settings()[key] is False
    with pytest.raises(OrchestratorError, match="must be on or off"):
        orch.update_settings({key: "maybe"})
    orch.update_settings({key: True})
    assert settings.desktop_notifications(orch.store)


def test_the_setting_cannot_be_set_per_project(orch: Orchestrator, project: str) -> None:
    with pytest.raises(OrchestratorError, match="can only be set globally"):
        orch.update_settings({settings.DESKTOP_NOTIFICATIONS: False}, project)


def test_switched_off_nothing_is_shown(
    orch: Orchestrator, project: str, as_agent: Callable, fake_osascript: Path, macos: None
) -> None:
    orch.update_settings({settings.DESKTOP_NOTIFICATIONS: "off"})
    ask(as_agent, lead(orch, project))
    desktop.show(orch.store, "hello")
    time.sleep(0.5)
    assert calls(fake_osascript) == []


def test_elsewhere_than_macos_nothing_is_shown(
    orch: Orchestrator,
    project: str,
    as_agent: Callable,
    fake_osascript: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(desktop, "PLATFORM", "linux")
    ask(as_agent, lead(orch, project))
    time.sleep(0.5)
    assert calls(fake_osascript) == []
    assert desktop.command("t", "b", "win32") is None
    assert desktop.command("t", "b", "darwin", {}.get) is None  # no osascript: nothing


def test_the_texts_are_arguments_on_one_line_never_script() -> None:
    tricky = '-x" & (do shell script "touch /tmp/x") & "\nsecond\tline' + "z" * 400
    argv = desktop.command("Spartito", tricky, "darwin", lambda name: f"/usr/bin/{name}")

    assert argv[:7] == [
        "/usr/bin/osascript",
        "-e",
        "on run argv",
        "-e",
        desktop.SCRIPT[1],
        "-e",
        "end run",
    ]
    title, body = argv[7:]
    assert title == "Spartito"
    assert body.startswith('-x" & (do shell script "touch /tmp/x") & " second linezz')
    assert len(body) == desktop.TEXT_CHARS and body.endswith("…")


def test_a_missing_or_broken_osascript_is_ignored(
    orch: Orchestrator, macos: None, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    broken = tmp_path / "osascript"
    broken.write_text("not a program")
    broken.chmod(0o644)  # found by command(), refused by the OS
    monkeypatch.setattr(desktop, "command", lambda title, body: [str(broken), body])
    desktop.show(orch.store, "hello")  # no exception
    monkeypatch.setattr(desktop, "command", lambda title, body: 1 / 0)
    desktop.show(orch.store, "hello")


# -- the "finished" notification: who counts --------------------------------


def turn(orch: Orchestrator, agent_id: str, sender: str | None = None, kind: str = MESSAGE,
         status: RunStatus = RunStatus.DONE) -> int:  # fmt: skip
    """A run that had its turn and ended, its parents checked, without a runner."""
    run = orch.store.add_run(agent_id, "task", sender=sender, kind=kind)
    with orch.store.transaction() as conn:
        orch.store.start_run(conn, run.id)
    orch.store.finish_run(run.id, status, result="ok")
    orch.store.mark_checked(run.id)
    return run.id


def test_an_agent_the_user_messaged_notifies_once_it_and_its_agents_are_done(
    orch: Orchestrator, project: str
) -> None:
    lead_id = lead(orch, project)
    first = turn(orch, lead_id)
    assert desktop.finished_for_owner(orch.store, first, lead_id) == f"{lead_id} finished"

    worker = orch.create_agent(project, name="w", worktree=False, parent_id=lead_id).id
    queued = orch.store.add_run(worker, "work", sender=lead_id)  # never launched: busy
    second = turn(orch, lead_id)
    assert desktop.finished_for_owner(orch.store, second, lead_id) is None
    assert desktop.finished_for_owner(orch.store, queued.id, worker) is None

    orch.store.finish_run(queued.id, RunStatus.DONE, result="done")
    orch.store.mark_checked(queued.id)
    assert desktop.finished_for_owner(orch.store, queued.id, worker) is None  # sent by the lead
    note = turn(orch, lead_id, kind=NOTIFICATION)
    assert desktop.finished_for_owner(orch.store, note, lead_id) == f"{lead_id} finished"
    failed = turn(orch, lead_id, status=RunStatus.FAILED)
    assert desktop.finished_for_owner(orch.store, failed, lead_id) == f"{lead_id} failed"
    stopped = turn(orch, lead_id, status=RunStatus.STOPPED)
    assert desktop.finished_for_owner(orch.store, stopped, lead_id) is None


def test_the_latest_message_decides_and_compactions_do_not_count(
    orch: Orchestrator, project: str
) -> None:
    top = orch.orchestrator().id
    lead_id = lead(orch, project, parent=top)
    turn(orch, lead_id)  # the user, earlier
    by_parent = turn(orch, lead_id, sender=top)
    assert desktop.finished_for_owner(orch.store, by_parent, lead_id) is None

    turn(orch, top)
    compaction = turn(orch, top, kind="compact")
    assert desktop.finished_for_owner(orch.store, compaction, top) == (
        f"{config.ORCHESTRATOR_NAME} finished"
    )
    unchecked = orch.store.add_run(top, "queued")  # its own turn still to come
    assert desktop.finished_for_owner(orch.store, compaction, top) is None
    orch.store.delete_run(unchecked.id)
    legacy = orch.create_agent(project, name="legacy", worktree=False).id
    with sqlite3.connect(orch.store.path) as conn:  # queued by older code: no metadata
        conn.execute(
            "INSERT INTO runs (agent_id, prompt, status, created_at, started_at)"
            " VALUES (?, 'old', 'done', 't', 't')",
            (legacy,),
        )
    conn.close()
    (old,) = orch.store.runs_of(legacy)
    assert desktop.finished_for_owner(orch.store, old.id, legacy) is None


def test_a_turn_that_queued_an_automatic_compaction_notifies_once_it_has_run(
    orch: Orchestrator, project: str
) -> None:
    agent_id = lead(orch, project)
    run = orch.store.add_run(agent_id, "task")
    with orch.store.transaction() as conn:
        orch.store.start_run(conn, run.id)
    compaction = orch.store.add_auto_compaction(agent_id, "/compact")  # as the runner does
    orch.store.finish_run(run.id, RunStatus.DONE, result="ok")
    orch.store.mark_checked(run.id)
    assert desktop.finished_for_owner(orch.store, run.id, agent_id) is None  # not idle yet

    for status in (RunStatus.DONE, RunStatus.FAILED):  # its own outcome is housekeeping
        with orch.store.transaction() as conn:
            orch.store.start_run(conn, compaction.id)
        orch.store.finish_run(compaction.id, status, result="compacted")
        orch.store.mark_checked(compaction.id)
        assert desktop.finished_for_owner(orch.store, compaction.id, agent_id) == (
            f"{agent_id} finished"
        )
        compaction = orch.store.add_auto_compaction(agent_id, "/compact")
    orch.store.delete_run(compaction.id)
    manual = turn(orch, agent_id, kind="compact", status=RunStatus.FAILED)  # the user's own
    assert desktop.finished_for_owner(orch.store, manual, agent_id) == f"{agent_id} failed"


@on_macos
def test_one_finished_notification_for_a_turn_above_the_compaction_threshold(
    orch: Orchestrator, project: str, fake_osascript: Path, macos: None
) -> None:
    set_threshold(orch, "2k")
    agent_id = lead(orch, project)
    brief = orch.send(agent_id, "CONTEXT 5000 from the user")

    settle(orch, agent_id)

    (compaction,) = compaction_runs(orch, agent_id)
    assert compaction.id > brief.id and orch.store.meta(compaction.id).auto == 1
    assert orch.store.get_run(compaction.id).status is RunStatus.DONE
    until(lambda: calls(fake_osascript), what="the notification")
    time.sleep(0.5)
    assert bodies(fake_osascript) == [f"{agent_id} finished"]


@on_macos
def test_a_lead_messaged_by_the_user_notifies_its_worker_never(
    orch: Orchestrator, project: str, as_agent: Callable, fake_osascript: Path
) -> None:
    lead_id = lead(orch, project)
    brief = orch.send(lead_id, "SLEEP 1 from the user")
    until(lambda: running(orch, brief.id), what="the brief")
    as_agent(lead_id)
    worker = mcp_server.spawn_agent("work", worktree=False)["id"]
    as_agent(None)

    settle(orch, lead_id)

    assert len(notifications(orch, lead_id)) == 1  # the worker's result woke the lead
    until(lambda: calls(fake_osascript), what="the notification")
    time.sleep(0.5)
    assert bodies(fake_osascript) == [f"{lead_id} finished"]
    assert all(worker not in body for body in bodies(fake_osascript))


@on_macos
def test_the_orchestrator_notifies_after_its_agents_finished(
    orch: Orchestrator, project: str, as_agent: Callable, fake_osascript: Path
) -> None:
    config.user_roles_dir().mkdir(parents=True)
    role = "---\nlead: true\nworktree: false\nprovider: fake\n---\nYou orchestrate."
    (config.user_roles_dir() / "orchestrator.md").write_text(role)
    top = orch.orchestrator().id
    brief = orch.send(top, "SLEEP 1 from the user")
    until(lambda: running(orch, brief.id), what="the brief")
    as_agent(top)
    lead_id = mcp_server.ask_project_lead(project, "SLEEP 1 work")["id"]
    as_agent(None)
    until(lambda: orch.store.get_run(brief.id).status is RunStatus.DONE, what="the brief")
    time.sleep(0.3)
    assert calls(fake_osascript) == []  # its lead still works

    settle(orch, top)

    assert len(notifications(orch, top)) == 1
    until(lambda: calls(fake_osascript), what="the notification")
    time.sleep(0.5)
    assert bodies(fake_osascript) == [f"{config.ORCHESTRATOR_NAME} finished"]
    assert all(lead_id not in body for body in bodies(fake_osascript))


# -- storage -----------------------------------------------------------------


def test_an_old_database_opens_with_the_decisions_table(tmp_path: Path) -> None:
    path = tmp_path / "old.db"
    with sqlite3.connect(path) as conn:
        conn.executescript(OLD_SCHEMA)
    conn.close()

    store = Store(path)
    agent = store.get_agent("a-1")
    decision = decisions.record(store, agent, "Keep it?", ["yes", "no"], None)

    assert decisions.listing(store) == [decision] and decision.project == "p"
    assert "decisions" in {
        row[0] for row in sqlite3.connect(path).execute("SELECT name FROM sqlite_master")
    }
    assert columns(path, "runs") == OLD_RUN_COLUMNS  # older code still reads its runs
    Store(path)  # opening it again changes nothing
    assert decisions.listing(store) == [decision]
