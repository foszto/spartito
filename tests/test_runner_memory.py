"""`memory` gives Write/Edit only to a session whose hooks are proven to run (runner.py)."""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

import pytest
from test_turn_end import all_gone

from orchestrator import config
from orchestrator.models import Agent, RunStatus
from orchestrator.runner import (
    EXITED,
    HOOKS_OFF,
    HOOKS_RAN,
    MEMORY_OFF,
    MEMORY_ON,
    MEMORY_SLOW,
    RECHECK_SECONDS,
    hooks_known_off,
    hooks_record,
    wait_for_canary,
)
from orchestrator.service import Orchestrator


@pytest.fixture(autouse=True)
def short_canary_wait(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ORCH_CANARY_SECONDS", "1")


def turn(orch: Orchestrator, agent_id: str, prompt: str | None = None):
    if prompt is not None:
        orch.send(agent_id, prompt)
    done, _ = orch.wait([agent_id], timeout=30)
    assert done, "agent did not finish in time"
    return orch.store.runs_of(agent_id)[-1]


def infos(orch: Orchestrator, agent_id: str) -> list[str]:
    return [event.text for event in orch.store.events(agent_id) if event.kind == "info"]


def launches(cwd: str) -> list[str]:
    path = Path(cwd) / ".fake-launches"
    return path.read_text().split() if path.exists() else []


def pids(cwd: str) -> list[int]:
    return [int(pid) for pid in (Path(cwd) / ".fake-pids").read_text().split()]


def one_prompt_and_one_usage(orch: Orchestrator, agent_id: str, run_id: int) -> bool:
    prompts = [e for e in orch.store.events(agent_id) if e.kind == "prompt" and e.run_id == run_id]
    return len(prompts) == 1 and list(orch.store.usage_of(agent_id)) == [run_id]


def age(agent_id: str) -> None:
    stale = time.time() - RECHECK_SECONDS - 60
    hooks_record(agent_id).write_text(json.dumps({"checked": stale}))


def test_with_its_hooks_running_a_lead_keeps_memory(orch: Orchestrator, project: str) -> None:
    lead = orch.lead(project)

    run = turn(orch, lead.id, "LOADOUT")

    assert run.status is RunStatus.DONE and run.result == "memory"
    assert launches(lead.cwd) == ["memory"] and infos(orch, lead.id) == []
    assert not hooks_record(lead.id).exists()


def test_without_hooks_the_turn_runs_once_more_without_memory_and_that_is_remembered(
    orch: Orchestrator, project: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_HOOKS", "off")
    lead = orch.lead(project)

    first = turn(orch, lead.id, "LOADOUT TOKENS 10")

    # The CLI that waited without a canary was ended with its child, before any input;
    # the turn ran once, without memory, with one prompt and one usage record.
    assert first.status is RunStatus.DONE and first.result == "plain"
    assert launches(lead.cwd) == ["memory", "plain"]
    assert len(pids(lead.cwd)) == 3 and all_gone(pids(lead.cwd))
    assert one_prompt_and_one_usage(orch, lead.id, first.id)
    assert infos(orch, lead.id) == [MEMORY_OFF] and hooks_record(lead.id).exists()

    # A fresh record: no canary, no wait, no new event.
    assert turn(orch, lead.id, "LOADOUT").result == "plain"
    assert launches(lead.cwd) == ["memory", "plain", "plain"]

    # After a day the canary is tried again; still off: no new event.
    age(lead.id)
    assert turn(orch, lead.id, "LOADOUT").result == "plain"
    assert launches(lead.cwd)[3:] == ["memory", "plain"]
    assert infos(orch, lead.id) == [MEMORY_OFF]

    # Hooks back on: memory again, said once, the record gone.
    monkeypatch.delenv("FAKE_HOOKS")
    age(lead.id)
    assert turn(orch, lead.id, "LOADOUT").result == "memory"
    assert infos(orch, lead.id) == [MEMORY_OFF, MEMORY_ON]
    assert not hooks_record(lead.id).exists()


def test_a_cli_that_exits_before_its_hooks_fails_once_with_its_own_error(
    orch: Orchestrator, project: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_HOOKS", "exit")
    lead = orch.lead(project)

    run = turn(orch, lead.id, "LOADOUT")

    assert run.status is RunStatus.FAILED and "Not logged in" in (run.error or "")
    assert launches(lead.cwd) == ["memory"]
    assert MEMORY_OFF not in infos(orch, lead.id) and not hooks_record(lead.id).exists()


def test_a_worker_with_memory_from_loadout_toml_gets_one_message_and_ends_at_eof(
    orch: Orchestrator, project: str
) -> None:
    (config.home() / "loadout.toml").write_text("[worker]\nmemory = true\n")
    worker, _ = orch.spawn(project, "LOADOUT TOKENS 10")

    run = turn(orch, worker.id)

    # The fake accepts nothing but one stream-json user message followed by EOF.
    assert run.status is RunStatus.DONE and run.result == "memory"
    assert launches(worker.cwd) == ["memory"]
    assert one_prompt_and_one_usage(orch, worker.id, run.id)
    assert all_gone(pids(worker.cwd))


def test_a_record_counts_only_for_a_resumed_session_and_for_a_day(tmp_path: Path) -> None:
    agent = Agent(id="lead-1", project="p", name="l", role="lead", provider="fake", cwd="/")
    resumed = Agent(**{**agent.__dict__, "session_id": "s"})

    assert not hooks_known_off(resumed)  # no record
    hooks_record(agent.id).parent.mkdir(parents=True, exist_ok=True)
    hooks_record(agent.id).write_text(json.dumps({"checked": time.time()}))
    assert hooks_known_off(resumed)
    assert not hooks_known_off(agent)  # a fresh session tries the canary again
    age(agent.id)
    assert not hooks_known_off(resumed)
    hooks_record(agent.id).write_text("garbage")
    assert not hooks_known_off(resumed)


def sleeper(code: str) -> subprocess.Popen[str]:
    return subprocess.Popen([sys.executable, "-c", code], text=True, stdin=subprocess.PIPE)


def test_the_canary_tells_ran_off_and_exited_apart(tmp_path: Path) -> None:
    canary = tmp_path / "canary"
    touching = sleeper(
        f"import pathlib, time; pathlib.Path({str(canary)!r}).touch(); time.sleep(5)"
    )
    silent = sleeper("import time; time.sleep(5)")
    gone = sleeper("pass")
    try:
        assert wait_for_canary(touching, canary, timeout=5) == HOOKS_RAN
        canary.unlink()
        started = time.monotonic()
        assert wait_for_canary(silent, canary, timeout=0.3) == HOOKS_OFF  # live, no canary
        assert time.monotonic() - started < 2
        gone.wait()
        assert wait_for_canary(gone, canary, timeout=5) == EXITED  # not a hooks verdict
    finally:
        for child in (touching, silent, gone):
            child.kill()
            child.wait()


def test_a_slow_start_turns_memory_off_for_one_turn_only(
    orch: Orchestrator, project: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_HOOKS", "slow")
    lead = orch.lead(project)

    assert turn(orch, lead.id, "LOADOUT").result == "plain"
    assert infos(orch, lead.id) == [MEMORY_SLOW] and not hooks_record(lead.id).exists()

    monkeypatch.delenv("FAKE_HOOKS")
    assert turn(orch, lead.id, "LOADOUT").result == "memory"  # tried again at once
    assert launches(lead.cwd) == ["memory", "plain", "memory"]


@pytest.mark.parametrize("hooks", ["off", "on"])
def test_a_record_that_cannot_be_kept_does_not_fail_the_turn(
    orch: Orchestrator, project: str, monkeypatch: pytest.MonkeyPatch, hooks: str
) -> None:
    lead = orch.lead(project)
    if hooks == "off":
        monkeypatch.setenv("FAKE_HOOKS", "off")
        (config.home() / "hooks-off").write_text("not a directory")  # cannot be written
    else:
        hooks_record(lead.id).mkdir(parents=True)  # cannot be removed

    run = turn(orch, lead.id, "LOADOUT")

    assert run.status is RunStatus.DONE
    assert run.result == ("plain" if hooks == "off" else "memory")
