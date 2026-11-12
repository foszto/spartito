"""Keeps the machine from sleeping on idle while a turn executes.

The runner starts one inhibitor once its run has taken the agent's turn and releases it
when the run has been stored (runner.take_turn); queued runs and idle agents hold none.
The inhibitor itself waits for the runner's pid, so a runner that crashes or is killed
(SIGKILL included) never leaves it behind:

- macOS: `caffeinate -i -w <runner pid>`: no idle system sleep; the display may sleep.
- Linux with systemd-inhibit: an `idle` lock held by a shell loop that ends with the runner.
- otherwise nothing. An info event says so once per ORCH_HOME (a marker file), also when
  the inhibitor cannot be started or exits at once (no logind, a refused lock).

It runs in a session of its own and without the turn's mark (runner.TURN_ENV), outside the
CLI's process tree; runner.turn_processes also leaves out every pid in `held` explicitly.
Off with the global setting `keep_awake` (`spartito config keep-awake off`).
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

from . import config, settings
from .store import Store

UNAVAILABLE = (
    "keeping the machine awake during turns is not possible here ({why}); "
    "it may sleep on idle while agents work"
)
NONE_FOUND = "neither caffeinate nor systemd-inhibit is available"
RELEASE_SECONDS = 2.0
WAIT_FOR_PID = 'while kill -0 "$1" 2>/dev/null; do sleep 1; done'

held: set[int] = set()  # pids (and session ids) of running inhibitors of this process


def command(
    pid: int, platform: str = sys.platform, which: Callable[[str], str | None] = shutil.which
) -> list[str] | None:
    """The command that inhibits idle sleep until process `pid` has ended; None: none here."""
    if platform == "darwin" and (caffeinate := which("caffeinate")):
        return [caffeinate, "-i", "-w", str(pid)]
    if platform.startswith("linux") and (inhibit := which("systemd-inhibit")):
        return [
            inhibit,
            "--what=idle",
            "--who=orchestrator",
            "--why=an agent is working",
            "--mode=block",
            "sh",
            "-c",
            WAIT_FOR_PID,
            "sh",
            str(pid),
        ]
    return None


def start(store: Store, run_id: int, agent_id: str, unset: str) -> subprocess.Popen[bytes] | None:
    """Start the inhibitor for this process's turn, unless switched off or unavailable.
    `unset`: the turn's mark, which it must not carry. A failed start is noted, not
    raised; other errors (the database) are the caller's to ignore (runner.hold_awake)."""
    if not settings.keep_awake(store):
        return None
    argv = command(os.getpid())
    if argv is None:
        _note_unavailable(store, run_id, agent_id, NONE_FOUND)
        return None
    try:
        proc = subprocess.Popen(
            argv,
            env={k: v for k, v in os.environ.items() if k != unset},
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,  # not in the runner's session, which a turn's end clears
        )
    except OSError as exc:
        _note_unavailable(store, run_id, agent_id, f"{Path(argv[0]).name} did not start: {exc}")
        return None
    held.add(proc.pid)
    return proc


def release(proc: subprocess.Popen[bytes] | None, store: Store, run_id: int, agent_id: str) -> None:
    """End the inhibitor now (it would end with the runner anyway); note once when it had
    already failed. Never raises: the run is stored by now, so even a stop arriving
    meanwhile is ignored."""
    if proc is None:
        return
    try:
        code = proc.poll()
        if code is None:
            proc.terminate()
            try:
                proc.wait(timeout=RELEASE_SECONDS)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=RELEASE_SECONDS)
        elif code != 0:  # e.g. systemd-inhibit without logind, or the lock refused
            why = f"{Path(str(proc.args[0])).name} exited at once with code {code}"
            _note_unavailable(store, run_id, agent_id, why)
    except Exception:
        pass
    finally:
        held.discard(proc.pid)


def _note_unavailable(store: Store, run_id: int, agent_id: str, why: str) -> None:
    try:
        (config.home() / "keep-awake-unavailable").open("x").close()
    except OSError:
        return  # said before (or the home is not writable: stay quiet)
    store.add_event(run_id, agent_id, "info", UNAVAILABLE.format(why=why))
