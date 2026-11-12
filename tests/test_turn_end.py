"""A run ends when the turn does: a lingering background command must not keep a lead's
run open, and a CLI that answers more than once is stored correctly."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
import uuid
from pathlib import Path

import pytest

from orchestrator.models import Agent, RunStatus
from orchestrator.providers import RunContext, get
from orchestrator.providers.base import NOTIFICATION_RESULT
from orchestrator.providers.claude import NO_BACKGROUND_ENV
from orchestrator.runner import (
    RUNNER_ARGV,
    _send,
    combine_results,
    end_turn,
    turn_processes,
)
from orchestrator.service import Orchestrator

FIXTURES = Path(__file__).parent / "fixtures"
# A mark no process carries. Never a fixed text: a shell whose command line contains the
# mark (one that runs these tests, say) would be part of the "turn" and be signalled.
NO_MARK = f"ORCH_TURN={uuid.uuid4().hex}"


def finished(orch: Orchestrator, agent_id: str, timeout: float = 30) -> dict:
    done, (view,) = orch.wait([agent_id], timeout=timeout)
    assert done, "agent did not finish in time"
    return view


def last_run(orch: Orchestrator, agent_id: str):
    return orch.store.runs_of(agent_id)[-1]


def fake_pids(cwd: str, count: int = 2, timeout: float = 15) -> list[int]:
    """Pids of the fake CLI and its children, once it has written `count` of them."""
    path = Path(cwd) / ".fake-pids"
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        pids = [int(pid) for pid in path.read_text().split()] if path.exists() else []
        if len(pids) >= count:
            return pids
        time.sleep(0.05)
    raise AssertionError(f"the fake CLI wrote {pids}, expected {count} pids")


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
    pids = fake_pids(lead.cwd)  # the child's comes after the answer: the grace period
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


def test_killing_the_runners_process_group_ends_the_cli(orch: Orchestrator, project: str) -> None:
    """stop() falls back to SIGKILL for the runner's group: the CLI must be in it."""
    agent, run = orch.spawn(project, "SLEEP 30", worktree=False)
    (cli,) = fake_pids(agent.cwd, count=1)

    os.killpg(orch.store.get_run(run.id).pid, signal.SIGKILL)

    assert all_gone([cli])


@pytest.mark.parametrize("apart", ["", "NEWSESSION "])
def test_a_lead_leaves_no_orphans_behind_a_cli_that_exited(
    orch: Orchestrator, project: str, apart: str
) -> None:
    """Like `nohup server &` in a tool shell: the shell is gone before the answer, the
    server re-parented, in the shell's process group (and session); the CLI exits."""
    lead = orch.lead(project)
    orch.send(lead.id, f"ORPHAN 30 {apart}TOKENS 1")

    finished(orch, lead.id)

    assert last_run(orch, lead.id).status is RunStatus.DONE
    assert all_gone(fake_pids(lead.cwd))


def test_a_stop_during_the_cleanup_keeps_the_answer(orch: Orchestrator, project: str) -> None:
    lead = orch.lead(project)
    orch.send(lead.id, "LINGER 30 STUBBORN TOKENS 10")
    pids = fake_pids(lead.cwd)
    time.sleep(6.5)  # grace period (5 s) over; the child ignores SIGTERM for 3 s

    assert orch.stop(lead.id) == 1

    run = last_run(orch, lead.id)
    assert run.status is RunStatus.DONE and run.result == "echo[new]: LINGER 30 STUBBORN TOKENS 10"
    assert orch.store.get_usage(run.id).input_tokens == 10
    assert all_gone(pids)


@pytest.mark.parametrize("role", ["lead", "worker"])
def test_a_cli_that_died_while_its_stdout_is_held_open_fails_soon(
    orch: Orchestrator, project: str, role: str
) -> None:
    if role == "lead":
        agent = orch.lead(project)
        orch.send(agent.id, "DIE")
    else:
        agent, _ = orch.spawn(project, "DIE", worktree=False)
    started = time.monotonic()

    finished(orch, agent.id)

    assert time.monotonic() - started < 15
    run = last_run(orch, agent.id)
    assert run.status is RunStatus.FAILED and run.result == "working"
    assert all_gone(fake_pids(agent.cwd))


def sleeper(*args: str, **kwargs) -> subprocess.Popen:
    return subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)", *args], **kwargs)


def test_turn_processes_follow_the_session_of_a_shell_that_has_exited() -> None:
    """The snapshot at the answer names the shell; its orphan is found by that id later."""
    shell = subprocess.Popen(
        ["/bin/sh", "-c", "/bin/sleep 30 >/dev/null 2>&1 & echo $!"],
        stdout=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    orphan = int(shell.stdout.readline())
    shell.wait()
    try:
        known = {shell.pid: (shell.pid, shell.pid)}

        assert orphan in turn_processes(shell.pid, NO_MARK, known)
        assert orphan not in turn_processes(shell.pid, NO_MARK, {})
    finally:
        os.kill(orphan, signal.SIGKILL)


def test_a_reused_pid_is_neither_selected_nor_signalled() -> None:
    other = sleeper()
    try:
        stale = {other.pid: (1, 1)}  # recorded for an earlier process with that pid

        assert other.pid not in turn_processes(-1, NO_MARK, stale)
        _send(stale, signal.SIGKILL)
        assert other.poll() is None
    finally:
        other.kill()
        other.wait()


def test_other_runners_and_their_sessions_are_not_part_of_a_turn() -> None:
    """A sub-agent's runner inherits the lead's environment through its MCP server."""
    token = uuid.uuid4().hex
    env = {**os.environ, "ORCH_TURN": token}
    ours = sleeper(env=env)
    runner = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import subprocess, sys, time; "
            "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)']); "
            "time.sleep(30)",
            *RUNNER_ARGV.split(),
            "7",
        ],
        env=env,
        start_new_session=True,
    )
    try:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            procs = turn_processes(-1, f"ORCH_TURN={token}")
            if ours.pid in procs:
                break
            time.sleep(0.1)

        assert ours.pid in procs
        assert not [pid for pid, (_, sid) in procs.items() if sid == runner.pid]
    finally:
        os.killpg(runner.pid, signal.SIGKILL)
        runner.wait()
        ours.kill()
        ours.wait()


class ExitedCli:
    """A CLI that has exited and been reaped; its pid now belongs to another process."""

    def __init__(self, pid: int) -> None:
        self.pid, self.returncode = pid, 0

    def poll(self) -> int:
        return 0

    def wait(self, timeout: float | None = None) -> int:
        return 0


def test_a_reused_pid_of_an_exited_cli_is_left_alone() -> None:
    """E.g. a terminal tab's shell, a new session leader, got the dead CLI's pid."""
    other = sleeper(start_new_session=True)
    try:
        assert other.pid not in turn_processes(None, NO_MARK)

        end_turn(ExitedCli(other.pid), NO_MARK)

        assert other.poll() is None
    finally:
        other.kill()
        other.wait()


def test_a_sub_agents_runner_does_not_pass_on_the_leads_mark(
    orch: Orchestrator,
    project: str,
    repo: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Its runner inherits the lead's environment; its git hooks must not carry the mark."""
    seen = tmp_path / "hook-env"
    hook = repo / ".git" / "hooks" / "post-checkout"
    hook.write_text(f'#!/bin/sh\necho "mark=$ORCH_TURN" > {seen}\n')
    hook.chmod(0o755)
    monkeypatch.setenv("ORCH_TURN", "lead-turn")  # as in the lead's MCP server

    agent, _ = orch.spawn(project, "task")
    finished(orch, agent.id)

    assert seen.read_text() == "mark=\n"


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


# A runner started as `python -m orchestrator.runner` by a process that ignores SIGTERM and
# SIGINT (a runner ending its turn starts a parent's notification run): what its __main__
# leaves in effect before main() takes over, seen from main's first step (Store(), here a
# probe that prints this process's ignored signals before and now, then sends itself SIGTERM).
IGNORING_PARENT = """
import os, runpy, signal, sys
signal.signal(signal.SIGTERM, signal.SIG_IGN)
signal.signal(signal.SIGINT, signal.SIG_IGN)
mask = lambda: open("/proc/self/status").read().split("SigIgn:")[1].split()[0]
before = mask()
import orchestrator.store

class Probe:
    def __init__(self):
        print(before, mask(), flush=True)
        os.kill(os.getpid(), signal.SIGTERM)
        sys.exit(0)

orchestrator.store.Store = Probe
sys.argv = ["runner", "1"]
runpy.run_module("orchestrator.runner", run_name="__main__", alter_sys=True)
"""


@pytest.mark.skipif(not Path("/proc/self/status").exists(), reason="reads /proc")
def test_a_runner_started_while_stop_signals_are_ignored_ends_on_sigterm():
    done = subprocess.run(
        [sys.executable, "-c", IGNORING_PARENT], capture_output=True, text=True, timeout=60
    )
    before, now = (int(mask, 16) for mask in done.stdout.split())
    term, interrupt = 1 << (signal.SIGTERM - 1), 1 << (signal.SIGINT - 1)
    assert before & term and before & interrupt  # inherited ignored, as from such a runner
    assert not now & term and not now & interrupt
    assert done.returncode == -signal.SIGTERM, done.stderr
