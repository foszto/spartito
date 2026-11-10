"""While a turn executes the machine is kept from sleeping on idle (keep_awake.py); the
fake caffeinate / systemd-inhibit of conftest.py stand in for the real ones."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
import uuid
from pathlib import Path

import pytest
from test_runner_memory import infos, launches
from test_turn_end import alive, all_gone, sleeper
from typer.testing import CliRunner

from orchestrator import cli, keep_awake
from orchestrator.models import RunStatus
from orchestrator.runner import turn_processes
from orchestrator.service import Orchestrator, OrchestratorError
from orchestrator.settings import KEEP_AWAKE
from orchestrator.settings import keep_awake as keep_awake_on

NAME = "caffeinate" if sys.platform == "darwin" else "systemd-inhibit"


def entries(log: Path) -> list[list[str]]:
    return [line.split() for line in log.read_text().splitlines()] if log.exists() else []


def started(log: Path) -> list[list[str]]:
    """[pid, "start", name, mark, *args] of each inhibitor started."""
    return [entry for entry in entries(log) if entry[1] == "start"]


def ends(log: Path, pid: str) -> list[str]:
    return [
        " ".join(entry[1:]) for entry in entries(log) if entry[0] == pid and entry[1] != "start"
    ]


def wait_for(condition, timeout: float = 15) -> None:
    deadline = time.monotonic() + timeout
    while not condition():
        assert time.monotonic() < deadline, "timed out"
        time.sleep(0.05)


def turn(orch: Orchestrator, agent_id: str):
    done, _ = orch.wait([agent_id], timeout=30)
    assert done, "agent did not finish in time"
    return orch.store.runs_of(agent_id)[-1]


def test_the_command_per_platform() -> None:
    found = {"caffeinate": "/usr/bin/caffeinate", "systemd-inhibit": "/usr/bin/systemd-inhibit"}
    no_systemd = {"caffeinate": "/usr/bin/caffeinate"}

    assert keep_awake.command(42, "darwin", found.get) == ["/usr/bin/caffeinate", "-i", "-w", "42"]
    assert keep_awake.command(42, "linux", found.get) == [
        "/usr/bin/systemd-inhibit",
        "--what=idle",
        "--who=orchestrator",
        "--why=an agent is working",
        "--mode=block",
        "sh",
        "-c",
        keep_awake.WAIT_FOR_PID,
        "sh",
        "42",
    ]
    assert keep_awake.command(42, "linux", no_systemd.get) is None
    assert keep_awake.command(42, "darwin", {}.get) is None
    assert keep_awake.command(42, "win32", found.get) is None


def test_the_linux_lock_is_held_by_a_loop_that_ends_with_the_runner() -> None:
    runner = sleeper()
    loop = subprocess.Popen(["sh", "-c", keep_awake.WAIT_FOR_PID, "sh", str(runner.pid)])
    try:
        time.sleep(0.3)
        assert loop.poll() is None
        runner.kill()
        runner.wait()
        assert loop.wait(timeout=3) == 0
    finally:
        runner.kill()
        loop.kill()


def test_a_turn_holds_one_inhibitor_tied_to_its_runner_and_a_queued_turn_none(
    orch: Orchestrator, project: str, fake_inhibitors: Path
) -> None:
    agent, first = orch.spawn(project, "SLEEP 2", worktree=False)
    second = orch.send(agent.id, "SLEEP 0.5")
    wait_for(lambda: started(fake_inhibitors))

    # The first turn's: unmarked, waiting for its runner; the queued one holds nothing.
    ((pid, _, name, mark, *args),) = started(fake_inhibitors)
    runner_pid = orch.store.get_run(first.id).pid
    assert name == NAME and mark == "unmarked" and args[-1] == str(runner_pid)
    assert NAME != "caffeinate" or args == ["-i", "-w", str(runner_pid)]
    assert orch.store.get_run(second.id).status is RunStatus.WAITING
    time.sleep(1)
    assert len(started(fake_inhibitors)) == 1 and alive(int(pid))

    assert turn(orch, agent.id).status is RunStatus.DONE
    wait_for(lambda: len(started(fake_inhibitors)) == 2)
    pids = [int(entry[0]) for entry in started(fake_inhibitors)]
    assert all_gone(pids)
    assert ends(fake_inhibitors, str(pids[0])) == ["TERM stored"]  # released, not left


@pytest.mark.parametrize("prompt", ["SLEEP 0.5", "SLEEP 0.5 FAIL"])
def test_released_when_the_run_is_stored_done_or_failed(
    orch: Orchestrator, project: str, fake_inhibitors: Path, prompt: str
) -> None:
    agent, _ = orch.spawn(project, prompt, worktree=False)

    run = turn(orch, agent.id)

    assert run.status is (RunStatus.FAILED if "FAIL" in prompt else RunStatus.DONE)
    ((pid, *_),) = started(fake_inhibitors)
    assert all_gone([int(pid)]) and ends(fake_inhibitors, pid) == ["TERM stored"]


def test_a_lead_turn_keeps_it_until_the_run_is_stored(
    orch: Orchestrator, project: str, fake_inhibitors: Path
) -> None:
    """The end of a lead's turn clears the runner's session and marked processes: not this."""
    lead = orch.lead(project)
    orch.send(lead.id, "LINGER 30")

    assert turn(orch, lead.id).status is RunStatus.DONE
    ((pid, *_),) = started(fake_inhibitors)
    assert all_gone([int(pid)]) and ends(fake_inhibitors, pid) == ["TERM stored"]


def test_a_stopped_turn_releases_it(
    orch: Orchestrator, project: str, fake_inhibitors: Path
) -> None:
    agent, _ = orch.spawn(project, "SLEEP 30", worktree=False)
    wait_for(lambda: started(fake_inhibitors))
    ((pid, *_),) = started(fake_inhibitors)

    orch.stop(agent.id)

    assert orch.store.runs_of(agent.id)[-1].status is RunStatus.STOPPED
    assert all_gone([int(pid)])


def test_a_relaunch_without_memory_keeps_the_one_inhibitor(
    orch: Orchestrator, project: str, fake_inhibitors: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ORCH_CANARY_SECONDS", "1")
    monkeypatch.setenv("FAKE_HOOKS", "off")
    lead = orch.lead(project)
    orch.send(lead.id, "LOADOUT")

    assert turn(orch, lead.id).result == "plain"
    assert launches(lead.cwd) == ["memory", "plain"]
    ((pid, *_),) = started(fake_inhibitors)
    assert ends(fake_inhibitors, pid) == ["TERM stored"]


def test_the_setting_switches_it_off(
    orch: Orchestrator, project: str, fake_inhibitors: Path
) -> None:
    orch.update_settings({KEEP_AWAKE: "off"})
    agent, _ = orch.spawn(project, "SLEEP 0.2", worktree=False)

    assert turn(orch, agent.id).status is RunStatus.DONE
    assert started(fake_inhibitors) == []


def test_without_an_inhibitor_it_says_so_once(
    orch: Orchestrator, project: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    agent, _ = orch.spawn(project, "SLEEP 0", worktree=False)
    run = turn(orch, agent.id)
    monkeypatch.setattr(keep_awake, "command", lambda pid: None)

    for _ in range(2):
        assert keep_awake.start(orch.store, run.id, agent.id, unset="ORCH_TURN") is None

    assert infos(orch, agent.id).count(keep_awake.UNAVAILABLE) == 1


def test_turn_processes_leave_out_the_inhibitor() -> None:
    token = f"ORCH_TURN={uuid.uuid4().hex}"
    marked = sleeper(env={**os.environ, "ORCH_TURN": token.split("=")[1]}, start_new_session=True)
    try:
        wait_for(lambda: marked.pid in turn_processes(None, token), timeout=5)
        keep_awake.held.add(marked.pid)

        assert marked.pid not in turn_processes(None, token)
    finally:
        keep_awake.held.discard(marked.pid)
        os.kill(marked.pid, signal.SIGKILL)
        marked.wait()


def test_the_setting_is_global_kept_by_other_saves_and_read_tolerantly(
    orch: Orchestrator, project: str
) -> None:
    assert keep_awake_on(orch.store)  # default on
    result = CliRunner().invoke(cli.app, ["config", "keep-awake", "off"])
    assert result.exit_code == 0 and "keep awake while agents work: off" in result.output
    assert orch.settings() == {KEEP_AWAKE: False} and not keep_awake_on(orch.store)

    # The settings dialog sends only its own fields: the value stays.
    orch.update_settings({"worker": {"fake": {"model": "m"}}})
    assert orch.settings()[KEEP_AWAKE] is False

    with pytest.raises(OrchestratorError, match="globally"):
        orch.update_settings({KEEP_AWAKE: True}, project)
    with pytest.raises(OrchestratorError, match="on or off"):
        orch.update_settings({KEEP_AWAKE: "maybe"})
    assert CliRunner().invoke(cli.app, ["config", "keep-awake", "maybe"]).exit_code != 0

    CliRunner().invoke(cli.app, ["config", "keep-awake", ""])
    assert KEEP_AWAKE not in orch.settings() and keep_awake_on(orch.store)

    orch.store.set_settings("", {KEEP_AWAKE: "garbage"})  # written by something else
    assert keep_awake_on(orch.store)
