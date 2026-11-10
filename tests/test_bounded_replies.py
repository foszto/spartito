"""MCP replies stay bounded and wait_for_agents returns each result once per caller."""

from __future__ import annotations

import functools
import json
import sqlite3
import subprocess
from pathlib import Path
from typing import Any

import anyio
import pytest
from test_store import OLD_RUN_COLUMNS, OLD_SCHEMA
from typer.testing import CliRunner

from orchestrator import cli, config, git, mcp_server
from orchestrator.models import RunStatus
from orchestrator.service import Orchestrator, OrchestratorError
from orchestrator.store import Store

LONG = "".join(f"{n:05d}" for n in range(2000))  # 10000 chars


def wait(*agent_ids: str, **options: Any) -> dict[str, Any]:
    call = functools.partial(mcp_server.wait_for_agents, list(agent_ids), **options)
    return anyio.run(call)


def finish(orch: Orchestrator, agent_id: str, result: str | None, structured: Any = None) -> int:
    run = orch.store.add_run(agent_id, "task")
    orch.store.finish_run(run.id, RunStatus.DONE, result=result, structured=structured)
    return run.id


def worker(orch: Orchestrator, project: str, result: str | None = "the answer", **kw: Any) -> str:
    agent = orch.create_agent(project, name="w", worktree=False)
    finish(orch, agent.id, result, **kw)
    return agent.id


def lead(orch: Orchestrator, project: str) -> str:
    return orch.create_agent(project, name="lead", role="lead", worktree=False).id


# -- wait_for_agents --------------------------------------------------------


def test_an_unfinished_wait_returns_statuses_only(orch: Orchestrator, project: str) -> None:
    done = worker(orch, project, "SECRET-RESULT")
    busy = orch.create_agent(project, name="busy", worktree=False).id
    pending = orch.store.add_run(busy, "queued, never launched")

    reply = wait(done, busy, timeout_seconds=1)

    assert reply == {
        "finished": False,
        "agents": [
            {"id": done, "status": "done", "queued_runs": 0},
            {"id": busy, "status": "waiting", "queued_runs": 1},
        ],
    }
    orch.store.finish_run(pending.id, RunStatus.DONE, result="later")
    finished = wait(done, busy)
    assert [agent["last_run"]["result"] for agent in finished["agents"]] == [
        "SECRET-RESULT",
        "later",
    ]  # nothing counted as delivered while unfinished


def test_a_result_is_delivered_once_then_marked(orch: Orchestrator, project: str) -> None:
    agent_id = worker(orch, project, "the answer")
    run_id = orch.store.last_run(agent_id).id

    first = wait(agent_id)
    again = wait(agent_id)

    assert first["finished"] and again["finished"]
    (outcome,) = first["agents"]
    assert set(outcome) == {"id", "status", "branch", "cost_usd", "tokens", "last_run"}
    assert outcome["last_run"] == {"id": run_id, "status": "done", "result": "the answer"}
    (repeat,) = again["agents"]
    assert "result" not in repeat["last_run"] and "the answer" not in json.dumps(again)
    assert repeat["last_run"]["already_delivered"]
    assert (repeat["status"], repeat["last_run"]["id"]) == ("done", run_id)


def test_a_new_run_after_send_message_is_delivered_again(orch: Orchestrator, project: str) -> None:
    spawned = mcp_server.spawn_agent("first task", project=project, worktree=False)
    first = wait(spawned["id"], timeout_seconds=30)
    mcp_server.send_message(spawned["id"], "second task")
    second = wait(spawned["id"], timeout_seconds=30)
    third = wait(spawned["id"])

    assert first["agents"][0]["last_run"]["result"] == "echo[new]: first task"
    assert second["agents"][0]["last_run"]["result"] == "echo[resumed]: second task"
    assert "already_delivered" in third["agents"][0]["last_run"]


def test_each_caller_gets_a_result_once(
    orch: Orchestrator, project: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    agent_id = worker(orch, project, "the answer")
    lead_a, lead_b = lead(orch, project), lead(orch, project)

    replies = {}
    for caller in (lead_a, lead_b, None, lead_a, lead_b, None):
        if caller:
            monkeypatch.setenv("ORCH_AGENT_ID", caller)
        else:
            monkeypatch.delenv("ORCH_AGENT_ID", raising=False)
        last = wait(agent_id)["agents"][0]["last_run"]
        replies.setdefault(caller, []).append(last.get("result"))

    assert replies == {caller: ["the answer", None] for caller in (lead_a, lead_b, None)}


def test_a_long_result_is_cut_at_the_limit_unless_raised(orch: Orchestrator, project: str) -> None:
    cut_id, full_id = worker(orch, project, LONG), worker(orch, project, LONG)

    cut = wait(cut_id)["agents"][0]["last_run"]["result"]
    full = wait(full_id, result_chars=20000)["agents"][0]["last_run"]["result"]

    assert cut.split("…")[0] == LONG[:4000]
    assert "of 10000 chars" in cut and "result_offset=4000" in cut and len(cut) < 4200
    assert full == LONG


def test_structured_output_is_returned_instead_of_the_text(
    orch: Orchestrator, project: str
) -> None:
    data = {"verdict": "ok", "items": [1, 2, 3]}
    small = worker(orch, project, "TEXT-VERSION", structured=data)
    big = worker(orch, project, "x", structured={"items": [LONG]})

    small_run = wait(small)["agents"][0]["last_run"]
    big_run = wait(big)["agents"][0]["last_run"]

    assert small_run["structured"] == data and "result" not in small_run
    assert isinstance(big_run["structured"], str) and len(big_run["structured"]) < 4200
    assert big_run["structured"].startswith('{"items": ["00000')
    assert "of 10015 chars" in big_run["structured"]


def test_a_failed_run_reports_its_error(orch: Orchestrator, project: str) -> None:
    agent_id = orch.create_agent(project, name="w", worktree=False).id
    run = orch.store.add_run(agent_id, "task")
    orch.store.finish_run(run.id, RunStatus.FAILED, error="boom")

    (outcome,) = wait(agent_id)["agents"]

    assert outcome["status"] == "failed"
    assert outcome["last_run"] == {"id": run.id, "status": "failed", "error": "boom"}


# -- get_agent --------------------------------------------------------------


def test_get_agent_limits_the_result_and_returns_it_in_full_on_request(
    orch: Orchestrator, project: str
) -> None:
    agent_id = worker(orch, project, LONG)

    default = mcp_server.get_agent(agent_id)["last_run"]["result"]
    full = mcp_server.get_agent(agent_id, result_chars=0)["last_run"]["result"]
    page = mcp_server.get_agent(agent_id, result_offset=8000)["last_run"]["result"]

    assert default.split("…")[0] == LONG[:4000] and len(default) < 4200
    assert "result_offset=4000" in default
    assert full == LONG
    assert page == f"[chars 8000-10000 of 10000] {LONG[8000:]}"


def test_get_agent_caps_the_git_output(orch: Orchestrator, project: str) -> None:
    agent = orch.create_agent(project, name="busy")
    git.add_worktree(orch.project(project).path, agent.cwd, agent.branch, agent.base_ref)
    commit = ["-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-q"]
    for n in range(25):
        subprocess.run(
            ["git", "-C", agent.cwd, *commit, "--allow-empty", "-m", f"c{n}"], check=True
        )
    for n in range(35):
        (Path(agent.cwd) / f"file{n}.txt").write_text("x")

    capped = mcp_server.get_agent(agent.id)["changes"]
    raised = mcp_server.get_agent(agent.id, max_commits=0, max_status=0)["changes"]
    cli_status = CliRunner().invoke(cli.app, ["status", agent.id]).output

    assert capped.count(" c") == 20 and "(+5 more)" in capped
    assert capped.count("?? file") == 30 and "(+5 more)" in capped.split("uncommitted:")[1]
    assert raised.count(" c") == 25 and raised.count("?? file") == 35 and "more)" not in raised
    assert "c0\n" in cli_status and cli_status.count("?? file") == 35  # humans see it all


# -- agent_log --------------------------------------------------------------


def test_agent_log_stays_under_the_cap_and_returns_an_event_in_full(
    orch: Orchestrator, project: str
) -> None:
    agent_id = worker(orch, project)
    run_id = orch.store.last_run(agent_id).id
    for n in range(100):
        orch.store.add_event(run_id, agent_id, "text", f"event {n} " + "y" * 3000)
    newest = orch.store.events(agent_id)[-1]

    reply = mcp_server.agent_log(agent_id)
    (one,) = mcp_server.agent_log(agent_id, event_id=newest.id)
    everything = mcp_server.agent_log(agent_id, tail=100, event_chars=0, max_chars=0)

    assert len(json.dumps(reply)) <= 6000
    assert reply[0].startswith("[") and "older events omitted" in reply[0]
    assert reply[-1].startswith(f"#{newest.id} ") and "event 99 " in reply[-1]
    assert "… [+" in reply[-1] and len(reply[-1]) < 400
    assert one.endswith(newest.text) and len(newest.text) > 3000
    assert len(everything) == 100 and all(line.endswith("y" * 3000) for line in everything)


def test_agent_log_refuses_another_agents_event(orch: Orchestrator, project: str) -> None:
    mine, other = worker(orch, project), worker(orch, project)
    orch.store.add_event(orch.store.last_run(other).id, other, "text", "not yours")
    (event,) = orch.store.events(other)

    with pytest.raises(OrchestratorError, match="has no event"):
        mcp_server.agent_log(mine, event_id=event.id)


# -- humans and old databases -----------------------------------------------


def test_the_cli_still_prints_everything(orch: Orchestrator, project: str) -> None:
    agent_id = worker(orch, project, LONG)
    orch.store.add_event(orch.store.last_run(agent_id).id, agent_id, "text", "z" * 3000)
    runner = CliRunner()

    waits = [runner.invoke(cli.app, ["wait", agent_id]) for _ in range(2)]
    status = runner.invoke(cli.app, ["status", agent_id])
    logs = runner.invoke(cli.app, ["logs", agent_id])

    assert [result.exit_code for result in waits] == [0, 0]
    assert all(LONG in result.output for result in waits)
    assert status.exit_code == 0 and LONG in status.output
    assert logs.exit_code == 0 and "z" * 3000 in logs.output


def test_an_old_database_gets_deliveries_and_wait_works(project_free_home: Path) -> None:
    store = Store(config.db_path())

    reply = wait("a-1")
    again = wait("a-1")

    assert reply["agents"][0]["last_run"]["id"] == 2
    assert "already_delivered" in again["agents"][0]["last_run"]
    assert store.delivered("", [1, 2]) == {2}
    with sqlite3.connect(config.db_path()) as conn:
        assert [row[1] for row in conn.execute("PRAGMA table_info(runs)")] == OLD_RUN_COLUMNS
    conn.close()


def test_removing_an_agent_removes_its_deliveries(project_free_home: Path) -> None:
    store = Store(config.db_path())
    store.mark_delivered("", [1, 2])
    store.mark_delivered("a-1", [7])

    store.delete_agent("a-1")

    assert store.delivered("", [1, 2]) == set() and store.delivered("a-1", [7]) == set()


@pytest.fixture
def project_free_home() -> Path:
    """The live database file in the old schema, as left by code from before deliveries."""
    path = config.db_path()
    with sqlite3.connect(path) as conn:
        conn.executescript(OLD_SCHEMA)
    conn.close()
    return path
