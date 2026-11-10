"""A run ends when the turn does: a lingering background command must not keep a lead's
run open, and a CLI that answers more than once is stored correctly."""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from orchestrator.models import Agent, RunStatus
from orchestrator.providers import RunContext, get
from orchestrator.providers.base import NOTIFICATION_RESULT
from orchestrator.providers.claude import NO_BACKGROUND_ENV
from orchestrator.runner import combine_results
from orchestrator.service import Orchestrator

FIXTURES = Path(__file__).parent / "fixtures"


def finished(orch: Orchestrator, agent_id: str, timeout: float = 30) -> dict:
    done, (view,) = orch.wait([agent_id], timeout=timeout)
    assert done, "agent did not finish in time"
    return view


def last_run(orch: Orchestrator, agent_id: str):
    return orch.store.runs_of(agent_id)[-1]


def fake_pids(cwd: str, timeout: float = 15) -> list[int]:
    """Pids of a lingering fake CLI and its child, once it has written them."""
    path = Path(cwd) / ".fake-pids"
    deadline = time.monotonic() + timeout
    while not path.exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    return [int(pid) for pid in path.read_text().split()]


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def all_gone(pids: list[int], timeout: float = 2) -> bool:
    """True once none of the processes exists (allowing a moment for zombies to be reaped)."""
    deadline = time.monotonic() + timeout
    while any(alive(pid) for pid in pids) and time.monotonic() < deadline:
        time.sleep(0.05)
    return not any(alive(pid) for pid in pids)


def test_a_lead_run_ends_with_its_answer_and_leaves_no_processes(
    orch: Orchestrator, project: str
) -> None:
    lead = orch.lead(project)
    started = time.monotonic()
    orch.send(lead.id, "LINGER 30")

    view = finished(orch, lead.id)

    assert time.monotonic() - started < 15
    run = last_run(orch, lead.id)
    assert run.status is RunStatus.DONE and run.result == "echo[new]: LINGER 30"
    assert view["status"] == "done"
    assert all_gone(fake_pids(lead.cwd))


def test_a_failed_lead_answer_ends_the_run_too(orch: Orchestrator, project: str) -> None:
    lead = orch.lead(project)
    started = time.monotonic()
    orch.send(lead.id, "FAIL TOKENS 10 LINGER 30")

    finished(orch, lead.id)

    assert time.monotonic() - started < 15
    run = last_run(orch, lead.id)
    assert run.status is RunStatus.FAILED and run.error == "boom"
    assert orch.store.get_usage(run.id).input_tokens == 10
    assert all_gone(fake_pids(lead.cwd))


def test_stop_during_the_grace_period_ends_everything_and_keeps_the_answer(
    orch: Orchestrator, project: str
) -> None:
    lead = orch.lead(project)
    orch.send(lead.id, "LINGER 30")
    pids = fake_pids(lead.cwd)  # written after the answer: the runner is in its grace period
    time.sleep(0.5)

    started = time.monotonic()
    assert orch.stop(lead.id) == 1

    assert time.monotonic() - started < 5
    run = last_run(orch, lead.id)
    assert run.status is RunStatus.DONE and run.result == "echo[new]: LINGER 30"
    assert all_gone(pids)


def test_a_lead_ignores_results_for_background_tasks_before_its_answer(
    orch: Orchestrator, project: str
) -> None:
    """On resume Claude first reports a background task its previous process left behind."""
    lead = orch.lead(project)
    orch.send(lead.id, "STALE TOKENS 10 LINGER 30")

    finished(orch, lead.id)

    run = last_run(orch, lead.id)
    assert run.status is RunStatus.DONE and run.result == "echo[new]: STALE TOKENS 10 LINGER 30"
    assert orch.store.get_usage(run.id).input_tokens == 20
    assert all_gone(fake_pids(lead.cwd))


def test_a_lead_stores_its_answer_not_a_later_one(orch: Orchestrator, project: str) -> None:
    lead = orch.lead(project)
    orch.send(lead.id, "TWICE TOKENS 100")

    finished(orch, lead.id)

    run = last_run(orch, lead.id)
    assert run.result == "echo[new]: TWICE TOKENS 100"
    assert orch.store.get_usage(run.id).input_tokens == 100


def test_other_roles_wait_for_the_process_and_stop_still_ends_it(
    orch: Orchestrator, project: str
) -> None:
    agent, run = orch.spawn(project, "LINGER 30", worktree=False)
    pids = fake_pids(agent.cwd)
    time.sleep(1.5)  # a lead would be in its grace period by now

    assert orch.store.get_run(run.id).status is RunStatus.RUNNING
    assert orch.stop(agent.id) == 1
    assert orch.store.get_run(run.id).status is RunStatus.STOPPED
    assert all_gone(pids)


@pytest.mark.parametrize(("mode", "tokens"), [("", 200), ("CUMULATIVE ", 100)])
def test_a_second_answer_is_stored_with_the_usage_of_both(
    orch: Orchestrator, project: str, mode: str, tokens: int
) -> None:
    """Per-result usage is summed; session totals are taken from the last result."""
    agent, run = orch.spawn(project, f"{mode}TWICE TOKENS 100", worktree=False)

    finished(orch, agent.id)

    stored = orch.store.get_run(run.id)
    assert stored.status is RunStatus.DONE and stored.result == "second answer"
    usage = orch.store.get_usage(run.id)
    assert usage.input_tokens == tokens and usage.cost_usd == pytest.approx(tokens / 100000)


def parse(fixture: str) -> list:
    provider = get("claude")
    lines = (FIXTURES / fixture).read_text().splitlines()
    return [event for line in lines for event in provider.parse(line)]


def test_claude_two_results_sum_to_the_session_total() -> None:
    """Captured with Claude Code 2.1.322: `sleep 20` in the background, answer, then the
    CLI's own turn when the command ended."""
    events = parse("claude_two_results.jsonl")
    results = [e for e in events if e.kind in ("result", NOTIFICATION_RESULT)]

    assert [e.kind for e in results] == ["result", NOTIFICATION_RESULT]
    combined = combine_results(results)
    assert combined.text == "The background `sleep 20` finished with exit code 0."
    usage = combined.usage
    assert (usage.input_tokens, usage.output_tokens) == (8, 129)
    assert (usage.cache_read_tokens, usage.cache_creation_tokens) == (23582, 12289)
    # The CLI's own session total after the second result.
    assert combined.cost_usd == pytest.approx(0.00275892)
    assert not usage.cumulative


def test_claude_reports_a_stopped_background_task_on_resume_before_the_answer() -> None:
    """Captured after the previous process was ended with `sleep 127` still running."""
    events = parse("claude_resumed_after_stop.jsonl")
    results = [e for e in events if e.kind in ("result", NOTIFICATION_RESULT)]

    assert [(e.kind, e.text[:3]) for e in results] == [
        (NOTIFICATION_RESULT, ""),
        ("result", "ok\n"),
    ]
    assert combine_results(results).usage.input_tokens == 2


@pytest.mark.parametrize("lead", [True, False])
def test_claude_leads_cannot_start_background_commands(tmp_path: Path, lead: bool) -> None:
    agent = Agent(id="a-1", project=None, name="a", role="r", provider="claude", cwd="/w")
    ctx = RunContext(agent=agent, prompt="hi", system_prompt="", scratch=tmp_path, lead=lead)

    env = get("claude").invocation(ctx).env

    assert env == (NO_BACKGROUND_ENV if lead else {})
    assert NO_BACKGROUND_ENV == {"CLAUDE_CODE_DISABLE_BACKGROUND_TASKS": "1"}
