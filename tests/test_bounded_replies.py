"""MCP replies stay bounded and wait_for_agents returns each result once per caller."""

from __future__ import annotations

import functools
import json
import sqlite3
import subprocess
import threading
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


def test_concurrent_waits_of_one_caller_deliver_a_result_once(
    orch: Orchestrator, project: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    agent_id = worker(orch, project, "RESULT")
    barrier = threading.Barrier(2, timeout=10)
    claim = Store.claim

    def claim_together(self: Store, caller: str, run_ids: list[int]) -> set[int]:
        barrier.wait()  # both calls have read everything and decided what is new
        return claim(self, caller, run_ids)

    monkeypatch.setattr(Store, "claim", claim_together)
    replies: list[dict] = []
    threads = [threading.Thread(target=lambda: replies.append(wait(agent_id))) for _ in "ab"]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    runs = [reply["agents"][0]["last_run"] for reply in replies]
    assert sorted(run.get("result", "") for run in runs) == ["", "RESULT"]
    assert sum("already_delivered" in run for run in runs) == 1


@pytest.mark.parametrize("status", [RunStatus.FAILED, RunStatus.SKIPPED])
def test_an_error_is_shown_again_on_a_repeat_call(
    orch: Orchestrator, project: str, status: RunStatus
) -> None:
    agent_id = orch.create_agent(project, name="w", worktree=False).id
    run = orch.store.add_run(agent_id, "task")
    orch.store.finish_run(run.id, status, error="boom")

    first, again = wait(agent_id), wait(agent_id)

    assert first["agents"][0]["last_run"]["error"] == "boom"
    assert again["agents"][0]["last_run"]["error"] == "boom"
    assert "already_delivered" in again["agents"][0]["last_run"]


def test_every_new_run_is_delivered_in_order(orch: Orchestrator, project: str) -> None:
    agent_id = worker(orch, project, "one")
    wait(agent_id)
    second = finish(orch, agent_id, "two")
    third = finish(orch, agent_id, "three")

    (outcome,) = wait(agent_id)["agents"]

    assert outcome["earlier_runs"] == [{"id": second, "status": "done", "result": "two"}]
    assert outcome["last_run"] == {"id": third, "status": "done", "result": "three"}


def test_a_run_finishing_after_the_idle_check_is_delivered_too(
    orch: Orchestrator, project: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    agent_id = orch.create_agent(project, name="w", worktree=False).id
    waited_for = orch.store.add_run(agent_id, "running when the wait starts")
    idle = Orchestrator.wait_idle

    def idle_then_another_run(self: Orchestrator, *args: Any) -> bool:
        self.store.finish_run(waited_for.id, RunStatus.DONE, result="first")
        result = idle(self, *args)
        finish(self, agent_id, "second")  # sent and finished before the reply is built
        return result

    monkeypatch.setattr(Orchestrator, "wait_idle", idle_then_another_run)
    (outcome,) = wait(agent_id)["agents"]

    assert [run["result"] for run in outcome["earlier_runs"]] == ["first"]
    assert outcome["last_run"]["result"] == "second"
    detail = mcp_server.get_agent(agent_id, run_id=waited_for.id)
    assert detail["run"]["result"] == "first" and "last_run" not in detail


def test_a_run_queued_after_the_idle_check_means_not_finished(
    orch: Orchestrator, project: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    agent_id = worker(orch, project, "first")
    idle = Orchestrator.wait_idle

    def idle_then_queued(self: Orchestrator, *args: Any) -> bool:
        result = idle(self, *args)
        self.store.add_run(agent_id, "queued meanwhile")
        return result

    monkeypatch.setattr(Orchestrator, "wait_idle", idle_then_queued)
    reply = wait(agent_id)

    assert reply == {
        "finished": False,
        "agents": [{"id": agent_id, "status": "waiting", "queued_runs": 1}],
    }


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


def test_get_agent_pages_structured_output(orch: Orchestrator, project: str) -> None:
    data = {"items": [LONG[:2985]]}  # 3000 chars of JSON
    agent_id = worker(orch, project, "x", structured=data)
    text = json.dumps(data)

    cut = wait(agent_id, result_chars=2000)["agents"][0]["last_run"]["structured"]
    rest = mcp_server.get_agent(agent_id, result_offset=2000)["last_run"]["structured"]
    whole = mcp_server.get_agent(agent_id)["last_run"]["structured"]
    past = mcp_server.get_agent(agent_id, result_offset=5000)["last_run"]["structured"]

    assert len(text) == 3000 and cut.split("…")[0] == text[:2000]
    assert "result_offset=2000" in cut
    assert rest == f"[chars 2000-3000 of 3000] {text[2000:]}"
    assert whole == data
    assert past == "[result_offset 5000 is past the end: 3000 chars]"


def test_get_agent_limits_the_error_and_gives_it_in_full(orch: Orchestrator, project: str) -> None:
    agent_id = orch.create_agent(project, name="w", worktree=False).id
    run = orch.store.add_run(agent_id, "task")
    orch.store.finish_run(run.id, RunStatus.FAILED, error=LONG)

    cut = mcp_server.get_agent(agent_id)["last_run"]["error"]
    full = mcp_server.get_agent(agent_id, result_chars=0)["last_run"]["error"]
    waited = wait(agent_id)["agents"][0]["last_run"]["error"]

    assert cut.split("…")[0] == LONG[:4000] and len(cut) < 4100
    assert "result_chars=0 for all" in cut and "result_offset" not in cut
    assert waited == cut
    assert full == LONG


def test_get_agent_refuses_a_run_of_another_agent(orch: Orchestrator, project: str) -> None:
    mine, other = worker(orch, project), worker(orch, project)

    with pytest.raises(OrchestratorError, match="has no run"):
        mcp_server.get_agent(mine, run_id=orch.store.last_run(other).id)


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

    assert sum(map(len, reply)) <= 6000
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


@pytest.mark.parametrize("event_chars", [0, 10, 300])
@pytest.mark.parametrize("max_chars", [1, 30, 100, 359, 500, 4100])
def test_agent_log_respects_any_cap(
    orch: Orchestrator, project: str, event_chars: int, max_chars: int
) -> None:
    agent_id = worker(orch, project)
    run_id = orch.store.last_run(agent_id).id
    for n in range(5):
        orch.store.add_event(run_id, agent_id, "text", f"event {n} " + "y" * 4000)

    reply = mcp_server.agent_log(agent_id, event_chars=event_chars, max_chars=max_chars)

    assert reply and sum(map(len, reply)) <= max_chars
    if max_chars >= 200:
        assert "event 4" in reply[-1]


def test_agent_log_refuses_an_event_of_another_project(
    orch: Orchestrator, project: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    other_repo = tmp_path / "other"
    subprocess.run(["git", "init", "-q", "-b", "main", str(other_repo)], check=True)
    other_project = orch.add_project(str(other_repo), "other", "fake").name
    foreign = worker(orch, other_project)
    orch.store.add_event(orch.store.last_run(foreign).id, foreign, "text", "secret")
    (event,) = orch.store.events(foreign)
    mine = worker(orch, project)
    monkeypatch.setenv("ORCH_AGENT_ID", lead(orch, project))

    with pytest.raises(OrchestratorError, match="has no event"):
        mcp_server.agent_log(mine, event_id=event.id)
    with pytest.raises(OrchestratorError, match="outside project"):
        mcp_server.agent_log(foreign, event_id=event.id)


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
    assert store.last_delivered("", "a-1") == 2
    with sqlite3.connect(config.db_path()) as conn:
        assert [row[1] for row in conn.execute("PRAGMA table_info(runs)")] == OLD_RUN_COLUMNS
    conn.close()


def test_removing_an_agent_removes_its_deliveries(project_free_home: Path) -> None:
    store = Store(config.db_path())
    store.claim("", [1, 2])
    store.claim("a-1", [7])

    store.delete_agent("a-1")

    assert store.claim("", [1, 2]) == {1, 2} and store.claim("a-1", [7]) == {7}


@pytest.fixture
def project_free_home() -> Path:
    """The live database file in the old schema, as left by code from before deliveries."""
    path = config.db_path()
    with sqlite3.connect(path) as conn:
        conn.executescript(OLD_SCHEMA)
    conn.close()
    return path
