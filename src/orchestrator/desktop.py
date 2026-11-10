"""Desktop notifications on this machine (macOS only; elsewhere nothing, silently).

Two occasions, each from the process where it happens:

- a decision was recorded (decisions.record, in the requesting agent's MCP server);
- an agent the owner messaged directly has finished (`turn_ended`, from
  Orchestrator.run_ended: the runner whose turn ended, or a process whose reconcile ended
  a run). Finished: a turn of the agent ended (it ran; a stopped turn does not count) and
  the agent and all its descendants are now settled (notify.settled), and its latest
  message run was queued by the user (run_meta.sender NULL; notification and compaction
  runs do not count). So the orchestrator answering after its agents finished notifies; a
  worker spawned by a lead never does.

`osascript` gets the texts as arguments of its run handler, never inside the script source
(they come from agents). It is started and left alone: a reaper thread kills it after
TIMEOUT_SECONDS; a missing binary or any error is ignored. Off with the global setting
`desktop_notifications` (`orch config desktop-notifications off`).
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import threading
from collections.abc import Callable

from . import config, notify, settings
from .models import RunStatus
from .store import MESSAGE, Store

SCRIPT = (
    "on run argv",
    "display notification (item 2 of argv) with title (item 1 of argv)",
    "end run",
)
TEXT_CHARS = 240  # of the body; Notification Center shows about two lines anyway
TIMEOUT_SECONDS = 5.0
PLATFORM = sys.platform  # tests set another


def command(
    title: str,
    body: str,
    platform: str | None = None,
    which: Callable[[str], str | None] = shutil.which,
) -> list[str] | None:
    """The osascript call that shows the notification; None: not possible here. The title
    comes first, so a body starting with '-' is never read as an option."""
    if (platform or PLATFORM) != "darwin" or not (osascript := which("osascript")):
        return None
    lines = [arg for line in SCRIPT for arg in ("-e", line)]
    return [osascript, *lines, one_line(title, 80), one_line(body, TEXT_CHARS)]


def one_line(text: str, limit: int) -> str:
    flat = " ".join("".join(c if c.isprintable() else " " for c in text).split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


def show(store: Store, body: str) -> None:
    """Show `body` under the tool's name, unless switched off or not possible. Never raises
    and never waits for osascript."""
    try:
        if not settings.desktop_notifications(store):
            return
        argv = command(config.PROJECT_NAME, body)
        if argv is None:
            return
        proc = subprocess.Popen(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        threading.Thread(target=_reap, args=(proc,), daemon=True).start()
    except Exception:
        pass


def _reap(proc: subprocess.Popen[bytes]) -> None:
    try:
        proc.wait(timeout=TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
    except Exception:
        pass


def display_name(store: Store, agent_id: str) -> str:
    agent = store.get_agent(agent_id)
    return config.ORCHESTRATOR_NAME if agent and agent.project is None else agent_id


def finished_for_owner(store: Store, run_id: int, agent_id: str) -> str | None:
    """The text of the "finished" notification due now that run `run_id` of the agent has
    ended (see the module doc), or None."""
    run = store.get_run(run_id)
    if run is None or run.started_at is None or run.status.active:
        return None
    if run.status not in (RunStatus.DONE, RunStatus.FAILED):
        return None
    with store.connection() as conn:
        latest = conn.execute(
            "SELECT sender FROM run_meta WHERE agent_id = ? AND kind = ?"
            " ORDER BY run_id DESC LIMIT 1",
            (agent_id, MESSAGE),
        ).fetchone()
    if latest is None or latest["sender"] is not None:
        return None
    if not notify.settled(store, agent_id):
        return None
    name = display_name(store, agent_id)
    return f"{name} finished" if run.status is RunStatus.DONE else f"{name} failed"


def turn_ended(store: Store, run_id: int, agent_id: str) -> None:
    """Called once a run has ended and its parents were checked. Never raises."""
    try:
        text = finished_for_owner(store, run_id, agent_id)
    except Exception:
        return
    if text:
        show(store, text)
