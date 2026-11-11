"""Desktop notifications on this machine (macOS only; elsewhere nothing, silently).

Two occasions, each from the process where it happens:

- a decision was recorded (decisions.record, in the requesting agent's MCP server);
- an agent the owner messaged directly has finished (`turn_ended`, from
  core/lifecycle.run_ended: the runner whose turn ended, or a process whose reconcile ended
  a run). Finished: a turn of the agent ended (it ran; a stopped turn does not count) and
  the agent and all its descendants are now settled (notify.settled), and its latest
  message run was queued by the user (run_meta.sender NULL; notification and compaction
  runs do not count). So the orchestrator answering after its agents finished notifies; a
  worker spawned by a lead never does. A turn that queued an automatic compaction leaves
  the agent busy: the one notification comes once that compaction has ended, and tells
  how the turn ended (the compaction is housekeeping; its own outcome is in the log).

Each one is recorded as a *notice* (store table `notices`: its kind, the agent, the
decision) so that an open dashboard page can show it as a Web Notification whose click opens
that agent or decision. Such a page touches a marker file (`page_present`, through the
facade) on each poll; while the marker is fresh (PAGE_FRESH_SECONDS) the notice is left to the
page, which claims it. Otherwise `osascript` shows it, as before any page could (macOS only;
elsewhere nothing is shown), and claims it first, so a page polling later does not show it
again and one that claimed it already is not doubled.

`osascript` gets the texts as arguments of its run handler, never inside the script source
(they come from agents). It is started and left alone: a reaper thread kills it after
TIMEOUT_SECONDS; a missing binary or any error is ignored. Off with the global setting
`desktop_notifications` (`orch config desktop-notifications off`): then nothing is recorded
either.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path

from . import config, notify, settings
from .models import RunStatus
from .store import COMPACT, MESSAGE, Store

SCRIPT = (
    "on run argv",
    "display notification (item 2 of argv) with title (item 1 of argv)",
    "end run",
)
TEXT_CHARS = 240  # of the body; Notification Center shows about two lines anyway
TIMEOUT_SECONDS = 5.0
PLATFORM = sys.platform  # tests set another
DECISION, FINISHED = "decision", "finished"  # the kinds of notice
PAGE_MARKER = "page-notifier"  # in config.runtime_dir()
PAGE_FRESH_SECONDS = 15.0  # a page polls far more often while it is visible
STORE_WAIT_SECONDS = 2.0  # for the write lock of a notice; then osascript shows it unrecorded


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


def page_marker() -> Path:
    return config.runtime_dir() / PAGE_MARKER


def page_present() -> None:
    """A dashboard page that can show notifications is polling: refresh the marker."""
    page_marker().touch()


def page_fresh() -> bool:
    """Whether such a page polled within the last PAGE_FRESH_SECONDS."""
    try:
        age = time.time() - page_marker().stat().st_mtime
    except OSError:
        return False
    return 0 <= age < PAGE_FRESH_SECONDS  # a marker from the future: not to be trusted


def show(
    store: Store, body: str, agent_id: str | None = None, decision_id: int | None = None
) -> None:
    """Notify about `agent_id` (a decision notice with `decision_id`, else a finished one)
    under the tool's name, unless switched off: record the notice, then leave it to a fresh
    page or show it with osascript (see the module doc). Without an agent nothing is
    recorded and osascript shows it. Never raises and never waits for osascript."""
    try:
        if not settings.desktop_notifications(store):
            return
        notice = _record(store, body, agent_id, decision_id)
        if notice is not None and page_fresh():
            return
        argv = command(config.PROJECT_NAME, body)
        if argv is None:
            return
        if notice is not None and not _claim(store, notice):
            return  # a page showed it meanwhile
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


def _record(store: Store, body: str, agent_id: str | None, decision_id: int | None) -> int | None:
    """The new notice's id; None without an agent or if it could not be stored."""
    if agent_id is None:
        return None
    try:
        kind = FINISHED if decision_id is None else DECISION
        return store.add_notice(kind, agent_id, body, decision_id, STORE_WAIT_SECONDS)
    except Exception:
        return None


def _claim(store: Store, notice: int) -> bool:
    """Claim the notice for osascript; one that cannot be claimed (the store is busy) is
    shown anyway, as before notices existed."""
    try:
        return store.claim_notice(notice, STORE_WAIT_SECONDS)
    except Exception:
        return True


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
    meta = store.meta(run_id)
    housekeeping = meta.kind == COMPACT and meta.auto  # queued after a turn that was done
    name = display_name(store, agent_id)
    return f"{name} finished" if run.status is RunStatus.DONE or housekeeping else f"{name} failed"


def turn_ended(store: Store, run_id: int, agent_id: str) -> None:
    """Called once a run has ended and its parents were checked. Never raises."""
    try:
        text = finished_for_owner(store, run_id, agent_id)
    except Exception:
        return
    if text:
        show(store, text, agent_id)
