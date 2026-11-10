"""`memory` gives Write/Edit only to a session whose hooks are proven to run (runner.py)."""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

import pytest

from orchestrator.models import RunStatus
from orchestrator.runner import MEMORY_OFF, hooks_ran
from orchestrator.service import Orchestrator


def answer(orch: Orchestrator, agent_id: str) -> str:
    done, _ = orch.wait([agent_id], timeout=30)
    assert done, "agent did not finish in time"
    run = orch.store.runs_of(agent_id)[-1]
    assert run.status is RunStatus.DONE, run.error
    return run.result or ""


def infos(orch: Orchestrator, agent_id: str) -> list[str]:
    return [event.text for event in orch.store.events(agent_id) if event.kind == "info"]


def test_with_its_hooks_running_a_lead_keeps_memory(orch: Orchestrator, project: str) -> None:
    lead = orch.lead(project)
    orch.send(lead.id, "LOADOUT")

    assert answer(orch, lead.id) == "memory"
    assert MEMORY_OFF not in infos(orch, lead.id)


def test_without_hooks_the_turn_runs_again_without_write_and_edit(
    orch: Orchestrator, project: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_HOOKS", "off")
    lead = orch.lead(project)
    orch.send(lead.id, "LOADOUT")

    assert answer(orch, lead.id) == "plain"  # the CLI actually run had no memory
    assert MEMORY_OFF in infos(orch, lead.id)


def test_a_role_without_memory_waits_for_nothing(
    orch: Orchestrator, project: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_HOOKS", "off")
    worker, _ = orch.spawn(project, "LOADOUT")

    assert answer(orch, worker.id) == "plain"
    assert MEMORY_OFF not in infos(orch, worker.id)


def sleeper(code: str) -> subprocess.Popen[str]:
    return subprocess.Popen([sys.executable, "-c", code], text=True, stdin=subprocess.PIPE)


def test_the_canary_proves_hooks_only_when_it_appears(tmp_path: Path) -> None:
    canary = tmp_path / "canary"
    touching = sleeper(
        f"import pathlib, time; pathlib.Path({str(canary)!r}).touch(); time.sleep(5)"
    )
    silent = sleeper("import time; time.sleep(5)")
    gone = sleeper("pass")
    try:
        assert hooks_ran(touching, canary, timeout=5)
        canary.unlink()
        started = time.monotonic()
        assert not hooks_ran(silent, canary, timeout=0.3)  # a CLI that waits without hooks
        assert time.monotonic() - started < 2
        gone.wait()
        assert not hooks_ran(gone, canary, timeout=5)  # exited without hooks
    finally:
        for child in (touching, silent, gone):
            child.kill()
            child.wait()
