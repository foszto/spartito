"""Starting and stopping runner processes: one detached runner per queued run."""

from __future__ import annotations

import contextlib
import os
import signal
import subprocess
import sys
import threading
import time
from typing import Protocol

from .. import config
from ..models import NOTIFICATION, Agent, Run, RunStatus
from ..store import Store


class Facade(Protocol):
    """What these functions use of the facade (they are its methods)."""

    store: Store

    def agent(self, agent_id: str) -> Agent: ...

    def run_ended(self, run_id: int, agent_id: str, recheck: bool = False) -> None: ...

    def stop_runs(self, agent_id: str) -> list[int]: ...

    @staticmethod
    def _kill(run: Run, sig: int) -> None: ...


STOP_GRACE_SECONDS = 8


def launch(store: Store, run: Run) -> None:
    """Start the run's runner process (Orchestrator._launch; core/lifecycle passes it on as
    the `launch` of processes without the facade)."""
    log = open(config.logs_dir() / f"run-{run.id}.runner.log", "w")  # noqa: SIM115
    env = {**os.environ, "ORCH_HOME": str(config.home())}
    proc = subprocess.Popen(
        [sys.executable, "-m", "orchestrator.runner", str(run.id)],
        stdin=subprocess.DEVNULL,
        stdout=log,
        stderr=log,
        env=env,
        start_new_session=True,  # survives the caller; also makes a killable group
    )
    log.close()
    store.update_run(run.id, pid=proc.pid)
    # Reap the runner if this process outlives it, so a dead runner is not a zombie
    # that still looks alive to Store.reconcile().
    threading.Thread(target=proc.wait, daemon=True).start()


def stop(orch: Facade, agent_id: str) -> int:
    """Stop every queued and running turn of an agent. Returns how many were stopped."""
    return len(orch.stop_runs(agent_id))


def stop_runs(orch: Facade, agent_id: str) -> list[int]:
    """Stop every queued and running turn of an agent; returns their ids."""
    agent = orch.agent(agent_id)
    # Newest first, so a queued turn cannot start while the running one is going down.
    active = sorted(orch.store.active_runs(agent.id), key=lambda run: -run.id)
    for run in active:
        orch._kill(run, signal.SIGTERM)
    deadline = time.monotonic() + STOP_GRACE_SECONDS
    while orch.store.active_runs(agent.id) and time.monotonic() < deadline:
        time.sleep(0.2)
    for run in orch.store.active_runs(agent.id):
        orch._kill(run, signal.SIGKILL)
        if run.started_at is None and orch.store.meta(run.id).kind == NOTIFICATION:
            orch.store.delete_run(run.id)  # never had a turn: leave no trace
        else:
            orch.store.finish_run(run.id, RunStatus.STOPPED, error="stopped")
        orch.run_ended(run.id, agent.id)  # a deliberate stop: no recheck of the agent
    return [run.id for run in active]


def _kill(run: Run, sig: int) -> None:
    if run.pid:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(run.pid, sig)
