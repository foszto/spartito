"""Desktop notifications on this machine (macOS and Linux; elsewhere nothing, silently).

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

Each one is recorded as a *notice* (store table `notices`), and `route` alone decides how it
is shown, from what is present now (`presence`: the Spartito window process, a dashboard page
allowed to notify, the launcher's app, notify-send). Every caller asks it: `show` (run end,
decision), the detached helper (`deliver`: escalation, a busy store), the window process when
it closes (core/notices.hand_over_notice), and the takers' claims (`claim`, `may_take`: the
window process, the launcher's posting process, the page). On macOS, in this order:

1. the Spartito window process (gui/notifier.py) while it runs and may post, or has not been
   answered yet (`route` "window", path window-system): it claims the notice on its next poll
   (POLL_SECONDS of the notifier, 2 s) and posts it, or shows it inside the window when the
   post fails. It counts as present while its process runs (pid, guarded against a reused
   one by the process's start time), even when its marker is late;
2. the launcher's app, allowed to post (grant file GRANT_FILE, written by the window process
   when it sees the grant, and a bundle whose interpreter copy is current): the detached
   helper starts it hidden (`open -n -g -j -a <bundle> --args --notify`), which claims and
   posts the pending notices and exits ("helper", launcher-helper);
3. a dashboard page allowed to show notifications, polling (its marker is fresh,
   PAGE_FRESH_SECONDS): it claims the notice and shows a Web Notification ("page");
4. the window process that may not post: a banner inside the window and the Dock's badge
   ("banner", window-banner);
5. none of these now, but a launcher that may post exists (2. failed) or a window runs: the
   notice waits unclaimed ("waiting"); the next posting process or page within its age
   limit, or the window when it next opens (its backlog), takes it, each as
   core/notices.worth_showing keeps it (decision still open; the newest few);
6. no launcher that may post, no window, no page: `osascript`, whose click opens Script
   Editor, so its text ends with where to look (CLAUSE_DECISION, CLAUSE_FINISHED).

Before all of these, on every platform: nothing is shown ("quiet", path not-shown; claimed
at once, so no taker ever gets it) when the environment says so (`quiet_reason`):
config.NOTIFICATIONS_ENV is off, or this process descends from an agent's turn of another
home (config.TURN_HOME_ENV, set by the runner; e.g. a test run in a temporary ORCH_HOME from
an agent's shell, whose notices would otherwise reach the owner's desktop through osascript).

On Linux: 3., then a detached helper with `notify-send` ("linux": a click opens `spartito
app` at the target where notify-send has actions), else the notice waits for a page. The
helpers are bounded: one waits for a click at most LINUX_WAIT_SECONDS (then notify-send is
killed), and at most LINUX_SLOTS of them wait at once (a lock per slot under
config.runtime_dir(); without a free slot the notice is shown without the wait, its text
naming the command). A burst is coalesced: at most BURST_LIMIT popups per BURST_SECONDS
for the home; the notices beyond are held back, each traced as `counted` on the path
LINUX_SUMMARY (the helper gets the notice's id), and the first of them waits until the
window frees up and shows "<n> more notices" in one popup (or itself, when it was the only
one), traced as `shown`. The state (run/linux-burst.json) heals itself: a count whose
summing helper ended or is overdue is discarded (logged), and a file that is not a burst
state starts afresh.

Claims keep a notice to one showing: a taker claims only a
notice left to it or to a later way of the list (`may_take`; the window that may post, any),
so a better way may still take it, and a worse one only once it was left to it. Who was
chosen and does not take the notice within ESCALATE_SECONDS (CLAIM_WAIT_SECONDS for the
launcher's app) is skipped: the helper process `deliver` routes it again without it.

Never blocking: the caller waits at most STORE_WAIT_SECONDS (0.25 s) for the store; a busy
store gets the notice as a spool file (run/notices/<uuid>.json), which the detached helper
keeps trying to record (opening the store is part of each try) for SPOOL_GIVE_UP_SECONDS, as
does every poll of the window, a page or the posting process; the trace says how long it
waited. Nothing goes out unrecorded except a notice without an agent.

The trace: every step (record, route, escalate, claim, shown, release, fallback, drop) of every
notice is a row of `notice_steps` (written by the process that took it, `actor`: role and pid)
and a line in logs/notices.log (NOTICE_LOG, size-capped; it also has the steps the store did
not take). `spartito notices` lists them.

`osascript` gets the texts as arguments of its run handler, never inside the script source
(they come from agents). Children are started and left alone: a reaper thread kills
osascript after TIMEOUT_SECONDS, a helper ends itself; a missing binary or any error is
ignored. Every child starts with SIGTERM, SIGINT, SIGHUP and SIGPIPE at their default
(DEFAULT_SIGNALS): a runner ignores SIGTERM and SIGINT while it ends its turn, which is when
it shows the notice, and an ignored signal is inherited across fork and exec. The helper sets
them first thing (`helper`); osascript and the helper's own children start through
EXEC_DEFAULT while this process ignores one (`with_default_signals`), or as they are when
that interpreter cannot be started (`start_defaulted`), so the notice is still shown. Off with the global setting `desktop_notifications` (`spartito config
desktop-notifications off`): then nothing is recorded either.
"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
import uuid
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from . import app_open, config, notify, settings
from .models import RunStatus
from .store import COMPACT, MESSAGE, NoticeStep, Store

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
STORE_WAIT_SECONDS = 0.25  # the caller waits this long for the write lock; then: spooled
SPOOL_TRY_SECONDS = 5.0  # each try of the spool helper waits this long for the write lock
APP_MARKER = "app-notifier"  # in config.runtime_dir(): the window process's pid (older readers)
APP_STATE = "app-notifier.json"  # beside it: {"pid", "start", "state"}, rewritten each poll
GRANT_FILE = "app-notify.json"  # in config.runtime_dir(): {"identifier", "bundle"} (2.)
SPOOL_DIR = "notices"  # in config.runtime_dir(): notices the busy store did not take yet
SPOOL_GIVE_UP_SECONDS = 600.0  # the helper keeps trying to record a spooled notice this long
NOTICE_LOG = "notices.log"  # in config.logs_dir(): one line per step
NOTICE_LOG_LIMIT = 1024 * 1024  # bytes; then it moves to notices.log.1
INTERPRETER_RECORD = ("Contents", "Resources", "interpreter.json")  # in the bundle (launcher.py)
OPEN = "/usr/bin/open"  # tests set a fake
NOTIFY_SEND = "notify-send"  # looked up on PATH; tests set a fake
CLAIM_WAIT_SECONDS = 15.0  # for the launcher's app to claim the notice (2.)
ESCALATE_SECONDS = 20.0  # for the window process or a page to claim a notice left to it
WAIT_STEP = 0.25  # between two looks at a notice the helper waits on
OPEN_TIMEOUT = 10.0
# A Linux helper waiting for a click ends after this (notify-send killed with it): five
# minutes covers a glance back at the desk, and keeps a burst from leaving helpers for long.
LINUX_WAIT_SECONDS = 300.0
LINUX_SLOTS = 3  # Linux helpers waiting for a click at once; beyond: shown without the wait
LINUX_SLOT_DIR = "linux-waiting"  # in config.runtime_dir(): slot-<n>, locked while waiting
BURST_LIMIT = 4  # Linux popups per BURST_SECONDS; the notices beyond are summed up in one
BURST_SECONDS = 60.0
BURST_STATE = "linux-burst.json"  # in config.runtime_dir(), changed under BURST_LOCK
BURST_LOCK = "linux-burst.lock"
CLAUSE_DECISION = " — open Spartito to answer"
CLAUSE_FINISHED = " — open Spartito to see it"
LINUX_ICON = Path(__file__).parent / "web" / "static" / "icon-192.png"
DESKTOP_ENTRY = "spartito"  # the launcher's desktop file name (gui/launcher.py)
HELPER = "import sys; from orchestrator import desktop; sys.exit(desktop.helper(sys.argv[1:]))"
# Signals a child of this module starts with at their default, whatever this process does
# with them: a runner ignores SIGTERM and SIGINT while it ends its turn (runner.main), when
# the notices are shown, and an ignored signal stays ignored across fork and exec.
DEFAULT_SIGNALS = ("SIGTERM", "SIGINT", "SIGHUP", "SIGPIPE")
# Runs a command with those at their default (argv: the program's path, then its arguments).
EXEC_DEFAULT = (
    "import os, signal, sys\n"
    f"for name in {DEFAULT_SIGNALS!r}:\n"
    "    signal.signal(getattr(signal, name), signal.SIG_DFL)\n"
    "os.execv(sys.argv[1], sys.argv[1:])"
)

# Whom a notice is left to (the `route` column), in the order of the module doc; a taker may
# claim a notice left to it or to a later one (RANK), or recorded by older code (no route).
WINDOW, HELPER_APP, PAGE, BANNER, WAITING = "window", "helper", "page", "banner", "waiting"
OSASCRIPT, LINUX = "osascript", "linux"  # shown at once by the deciding process
RANK = {WINDOW: 0, HELPER_APP: 1, PAGE: 2, BANNER: 3, WAITING: 4}
QUIET = "quiet"  # shown by nobody, by what the environment says (`quiet_reason`)
# The paths, as the trace names them.
PATHS = {
    WINDOW: "window-system",
    HELPER_APP: "launcher-helper",
    PAGE: "page",
    BANNER: "window-banner",
    WAITING: "waiting",
    OSASCRIPT: "osascript",
    LINUX: "linux",
    QUIET: "not-shown",
}
DROPPED = "dropped"  # a path: nobody shows it (a decision answered before it was shown)
COUNTED = "window-count"  # a path: counted in the window's "more" banner, not shown in one
LINUX_SUMMARY = "linux-summary"  # a path: held back by the Linux burst limit, shown in a summary
# The window process's notification states (gui/notifier.py) in which it takes notices to post.
POSTING_STATES = ("allowed", "waiting", "asking", "")  # "": a window of older code
ROLE: str | None = None  # this process's part, set by entry points; else guessed (`role`)


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


# -- who this process is ---------------------------------------------------------------------


def role() -> str:
    """This process's part: ROLE when an entry point set it, else from its command line."""
    if ROLE:
        return ROLE
    argv = sys.argv or [""]
    script = Path(argv[0]).name
    if script == "runner.py":
        return "runner"
    if script == "mcp_server.py":
        return "mcp"
    words = [arg for arg in argv[1:] if not arg.startswith("-")]
    if words and words[0] == "app":
        return "notify" if "--notify" in argv else "window"
    if words and words[0] in ("web", "mcp"):
        return words[0]
    return "cli"


TAKER_ROLE = {WINDOW: "window", BANNER: "window", HELPER_APP: "notify", PAGE: "page"}


def actor(part: str | None = None) -> str:
    """This process in the trace: '<role>:<pid>'; `part`: a taker (its role) or a role."""
    return f"{TAKER_ROLE.get(part or '', part) or role()}:{os.getpid()}"


# -- markers ---------------------------------------------------------------------------------


def page_marker() -> Path:
    return config.runtime_dir() / PAGE_MARKER


def page_present() -> None:
    """A dashboard page that can show notifications is polling: refresh the marker."""
    page_marker().touch()


def page_age() -> float | None:
    """Seconds since such a page last polled; None: never, or a marker from the future."""
    return _age(page_marker())


def page_fresh() -> bool:
    """Whether such a page polled within the last PAGE_FRESH_SECONDS."""
    age = page_age()
    return age is not None and age < PAGE_FRESH_SECONDS


def app_marker() -> Path:
    return config.runtime_dir() / APP_MARKER


def app_state_path() -> Path:
    return config.runtime_dir() / APP_STATE


_own_start: str | None = None


def app_present(state: str = "allowed") -> None:
    """The Spartito window process polled: refresh its markers, the pid (for older readers)
    and its notification state with the pid's start time (against a reused pid)."""
    global _own_start
    if _own_start is None:
        _own_start = process_start(os.getpid()) or ""
    pid = os.getpid()
    app_marker().write_text(f"{pid}\n")
    path = app_state_path()
    temp = path.with_name(f"{path.name}.{pid}.tmp")
    temp.write_text(json.dumps({"pid": pid, "start": _own_start, "state": state}))
    temp.replace(path)


def app_gone() -> None:
    """The window process is going: no markers."""
    for path in (app_marker(), app_state_path()):
        path.unlink(missing_ok=True)


def app_fresh() -> bool:
    """Whether a window process is there to take notices (see `window_presence`)."""
    window = window_presence()
    return window is not None and window.alive


def _age(marker: Path) -> float | None:
    try:
        age = time.time() - marker.stat().st_mtime
    except OSError:
        return None
    return age if age >= 0 else None  # a marker from the future: not to be trusted


def process_start(pid: int) -> str | None:
    """When process `pid` started, as `ps` says it (None: no such process, or no answer)."""
    try:
        done = subprocess.run(
            ["ps", "-o", "lstart=", "-p", str(pid)],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except OSError, subprocess.TimeoutExpired:
        return None
    return done.stdout.strip() or None if done.returncode == 0 else None


def pid_running(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except PermissionError:
        return True  # there, but another user's
    except OSError:
        return False
    return True


@dataclass(frozen=True)
class Window:
    """The window process as its markers and the process table show it."""

    pid: int
    alive: bool  # its process runs (and is the one that wrote the marker)
    age: float | None  # seconds since its last poll
    state: str  # its notification state ("": a window of older code)
    why: str = ""  # why it does not count, when it does not

    @property
    def taker(self) -> str:
        return WINDOW if self.state in POSTING_STATES else BANNER

    def describe(self) -> str:
        age = "?" if self.age is None else f"{self.age:.1f} s"
        state = self.state or "older code"
        alive = "running" if self.alive else f"not counted: {self.why}"
        return f"window pid {self.pid} ({state}, polled {age} ago, {alive})"


def window_presence() -> Window | None:
    """The window process, None without a marker. It counts (`alive`) while its process runs:
    the pid, guarded by the start time it recorded; a window of older code (no state file)
    only while its marker is fresh, as older code had it."""
    age = _age(app_marker())
    try:
        data = json.loads(app_state_path().read_text())
        pid, start, state = int(data["pid"]), str(data.get("start") or ""), str(data["state"])
    except OSError, ValueError, TypeError, KeyError:
        try:
            pid = int(app_marker().read_text().strip())
        except OSError, ValueError:
            return None
        fresh = age is not None and age < PAGE_FRESH_SECONDS
        alive = fresh and pid_running(pid)
        why = "" if alive else ("its process is gone" if fresh else "its marker is stale")
        return Window(pid, alive, age, "", why)
    if not pid_running(pid):
        return Window(pid, False, age, state, "its process is gone")
    now_start = process_start(pid)
    if start and now_start and now_start != start:
        return Window(pid, False, age, state, "its pid belongs to another process now")
    if not (start and now_start) and not (age is not None and age < PAGE_FRESH_SECONDS):
        return Window(pid, False, age, state, "its start time is unknown and its marker stale")
    return Window(pid, True, age, state)


# -- the one decision ------------------------------------------------------------------------


@dataclass(frozen=True)
class Presence:
    """What `route` decides from: who is there to show a notice now."""

    platform: str
    window: Window | None = None
    page: float | None = None  # seconds since a permitted page polled
    bundle: Path | None = None  # the launcher's app that may post
    no_bundle: str = ""  # why there is none
    notify_send: str | None = None
    quiet: str = ""  # why nothing may be shown here (`quiet_reason`)

    @property
    def page_fresh(self) -> bool:
        return self.page is not None and self.page < PAGE_FRESH_SECONDS

    @property
    def live_window(self) -> Window | None:
        return self.window if self.window is not None and self.window.alive else None

    def describe(self) -> str:
        if self.quiet:
            return self.quiet
        parts = [self.window.describe() if self.window else "no window"]
        parts.append("no page" if self.page is None else f"page polled {self.page:.1f} s ago")
        if self.platform == "darwin":
            parts.append("launcher ready" if self.bundle else f"launcher: {self.no_bundle}")
        elif self.platform.startswith("linux"):
            parts.append("notify-send" if self.notify_send else "no notify-send")
        return "; ".join(parts)


def presence() -> Presence:
    platform = PLATFORM
    if quiet := quiet_reason():
        return Presence(platform, quiet=quiet)
    window = window_presence() if platform == "darwin" else None
    bundle, why = bundle_state() if platform == "darwin" else (None, "")
    send = shutil.which(NOTIFY_SEND) if platform.startswith("linux") else None
    return Presence(platform, window, page_age(), bundle, why, send)


OFF_WORDS = ("off", "0", "false", "no")


def quiet_reason(environ: dict[str, str] | None = None) -> str:
    """Why this process may show no desktop notification at all ("" when it may): the
    switch config.NOTIFICATIONS_ENV is off, or config.TURN_HOME_ENV names another home than
    this one (started, maybe through other Spartito processes, from an agent's turn of
    another instance)."""
    env = os.environ if environ is None else environ
    switch = env.get(config.NOTIFICATIONS_ENV, "").strip()
    if switch.lower() in OFF_WORDS:
        return f"{config.NOTIFICATIONS_ENV}={switch}: not shown"
    outer = env.get(config.TURN_HOME_ENV)
    if outer and not same_home(outer, config.home()):
        return f"started by an agent of another home ({outer}): not shown"
    return ""


def same_home(one: str, other: Path) -> bool:
    try:
        return Path(one).expanduser().resolve() == other.resolve()
    except OSError, RuntimeError:
        return False


@dataclass(frozen=True)
class Choice:
    taker: str  # the route: WINDOW, HELPER_APP, PAGE, BANNER, WAITING, OSASCRIPT, LINUX
    reason: str

    @property
    def path(self) -> str:
        return PATHS[self.taker]

    @property
    def at_once(self) -> bool:
        """Shown (or, quiet, not shown) by the deciding process itself: claimed with the
        decision."""
        return self.taker in (OSASCRIPT, LINUX, QUIET)


def route(p: Presence, skip: Iterable[str] = ()) -> Choice:
    """How a notice is shown now (the module doc's order), `skip`: takers that did not take
    it. The one place that decides; everything else asks it or carries out its answer."""
    if p.quiet:
        return Choice(QUIET, p.quiet)
    skip = set(skip)
    window = p.live_window
    if window is not None and WINDOW in skip:
        window = None
    if p.platform == "darwin":
        if window is not None and window.taker == WINDOW:
            return Choice(WINDOW, f"the window process takes it ({window.state or 'older code'})")
        if p.bundle is not None and HELPER_APP not in skip:
            return Choice(HELPER_APP, "the launcher's app may post")
        if p.page_fresh and PAGE not in skip:
            return Choice(PAGE, "a permitted page polls")
        if window is not None and BANNER not in skip:
            return Choice(BANNER, f"the window may not post ({window.state})")
        if p.bundle is not None or p.live_window is not None:
            return Choice(WAITING, "nothing can show it now; the next taker or window does")
        return Choice(OSASCRIPT, f"no window, no permitted page, no launcher ({p.no_bundle})")
    if p.page_fresh and PAGE not in skip:
        return Choice(PAGE, "a permitted page polls")
    if p.platform.startswith("linux"):
        if p.notify_send:
            return Choice(LINUX, "notify-send")
        return Choice(WAITING, "no notify-send on PATH; a page may take it")
    return Choice(WAITING, f"no desktop notifications on {p.platform}; a page may take it")


def may_take(taker: str, route_now: str | None) -> bool:
    """Whether `taker` may claim a notice left to `route_now` (None: recorded by older code)."""
    if route_now is None:
        return True
    return route_now in RANK and taker in RANK and RANK[route_now] >= RANK[taker]


def takeable(taker: str) -> tuple[str, ...]:
    return tuple(name for name in RANK if may_take(taker, name))


# -- the trace -------------------------------------------------------------------------------


def notice_log() -> Path:
    return config.logs_dir() / NOTICE_LOG


def log_step(notice: int | str | None, step: str, path: str | None, who: str, detail: str):
    """One line in logs/notices.log; moved to notices.log.1 above NOTICE_LOG_LIMIT."""
    with contextlib.suppress(OSError):
        log = notice_log()
        with contextlib.suppress(FileNotFoundError):
            if log.stat().st_size > NOTICE_LOG_LIMIT:
                log.replace(log.with_name(NOTICE_LOG + ".1"))
        stamp = datetime.now().astimezone().isoformat(timespec="milliseconds")
        name = "-" if notice is None else f"#{notice}" if isinstance(notice, int) else notice
        line = f"{stamp} {name} {who} {step} {path or '-'}: {one_line(detail, 400)}\n"
        with log.open("a", encoding="utf-8") as out:
            out.write(line)


def step(kind: str, path: str | None, detail: str = "", who: str | None = None) -> NoticeStep:
    return NoticeStep(kind, path, who or actor(), detail, now_iso())


def now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


def note(store: Store | None, notice: int, item: NoticeStep, wait: float = 5.0) -> None:
    """Trace a step: in the log, and in the store (the log says when the store refused)."""
    log_step(notice, item.step, item.path, item.actor, item.detail)
    if store is None:
        return
    try:
        store.add_notice_step(notice, item, wait)
    except Exception as exc:
        log_step(notice, "lost", item.path, item.actor, f"the store did not take this step: {exc}")


def logged(notice: int, items: Iterable[NoticeStep]) -> None:
    for item in items:
        log_step(notice, item.step, item.path, item.actor, item.detail)


# -- showing a notice ------------------------------------------------------------------------


def target_of(agent_id: str | None, decision_id: int | None) -> str:
    """Where a click should open Spartito: "decision:<n>", the agent, or "" (anywhere)."""
    if decision_id is not None:
        return f"decision:{decision_id}"
    return agent_id or ""


def show(
    store: Store, body: str, agent_id: str | None = None, decision_id: int | None = None
) -> None:
    """Notify about `agent_id` (a decision notice with `decision_id`, else a finished one)
    under the tool's name, unless switched off: record the notice as `route` decides, then
    carry that out (see the module doc). Never raises and never waits for a child."""
    try:
        if not settings.desktop_notifications(store):
            return
        found = presence()
        choice = route(found)
        if agent_id is None:  # nothing to record, so only a way that shows it at once
            unrecorded(choice, found, body, decision_id)
            return
        kind = FINISHED if decision_id is None else DECISION
        decided = step("route", choice.path, f"{choice.reason}; {found.describe()}")
        recorded = step("record", None, "in the table at once")
        began = time.monotonic()
        try:
            notice = store.add_notice(
                kind,
                agent_id,
                body,
                decision_id,
                STORE_WAIT_SECONDS,
                route=choice.taker,
                claimed_by=actor() if choice.at_once else None,
                steps=[recorded, decided],
            )
        except Exception as exc:
            waited = time.monotonic() - began
            why = f"the store was busy for {waited:.1f} s ({exc})"
            spool(kind, agent_id, body, decision_id, why, decided)
            return
        logged(notice, [recorded, decided])
        row = {"id": notice, "text": body, "agent_id": agent_id, "decision_id": decision_id}
        carry_out(store, row, choice)
    except Exception:
        pass


def unrecorded(choice: Choice, found: Presence, body: str, decision_id: int | None) -> None:
    """A notice without an agent (never one of the two occasions): osascript or notify-send
    show it when this platform has them, unrecorded; logged."""
    if found.quiet:
        log_step(None, "drop", PATHS[QUIET], actor(), f"no agent, not recorded; {found.quiet}")
        return
    if found.platform == "darwin":
        taker, how = OSASCRIPT, "osascript"
    elif found.platform.startswith("linux") and found.notify_send:
        taker, how = LINUX, "notify-send"
    else:
        log_step(None, "drop", DROPPED, actor(), "no agent, nothing records it; not shown")
        return
    log_step(None, "shown", PATHS[taker], actor(), f"no agent, not recorded: {how}")
    if taker == OSASCRIPT:
        plain(body, decision_id)
    else:
        spawn(["linux", found.notify_send or NOTIFY_SEND, body, target_of(None, decision_id)])


def carry_out(
    store: Store,
    notice: dict[str, Any],
    choice: Choice,
    detached: bool = False,
    opener: str | None = None,
) -> None:
    """Do what `route` chose for a recorded notice (a row: id, text, agent_id, decision_id):
    show it at once (osascript, notify-send; claimed with the choice), or have the detached
    helper see it taken (`deliver`, run here when this is that helper). The caller's steps
    wait at most STORE_WAIT_SECONDS for the store (else only the log has them)."""
    nid, body, decision_id = notice["id"], str(notice["text"]), notice.get("decision_id")
    wait = 5.0 if detached else STORE_WAIT_SECONDS
    if choice.taker == QUIET:
        note(store, nid, step("drop", PATHS[QUIET], choice.reason), wait)
        return
    if choice.taker == OSASCRIPT:
        shown = plain(body, decision_id)
        if shown is not None:
            note(store, nid, step("shown", PATHS[OSASCRIPT], "osascript started"), wait)
        else:
            gone = "osascript could not be started"
            note(store, nid, step("drop", PATHS[OSASCRIPT], gone), wait)
        if detached and shown is not None:
            shown.join(TIMEOUT_SECONDS + 1)  # this process ends next
        return
    if choice.taker == LINUX:
        send = shutil.which(NOTIFY_SEND) or NOTIFY_SEND
        target = target_of(notice.get("agent_id"), decision_id)
        if detached:
            note(store, nid, step("shown", PATHS[LINUX], "notify-send"))
            linux_helper(send, body, target, nid, store)
        elif spawn(["linux", send, body, target, str(nid)]):
            note(store, nid, step("shown", PATHS[LINUX], "notify-send helper started"), wait)
        else:
            gone = "the notify-send helper did not start"
            store.release_notice(
                nid, STORE_WAIT_SECONDS, route=WAITING, step=step("release", PATHS[WAITING], gone)
            )
            logged(nid, [step("release", PATHS[WAITING], gone)])
        return
    if choice.taker == WAITING:
        return
    if detached:
        deliver(store, nid, opener)
    elif not spawn(["deliver", str(nid), opener or OPEN]):
        note(
            store,
            nid,
            step("fallback", choice.path, "the helper did not start; nobody escalates it"),
            wait,
        )


# -- a busy store: the spool -----------------------------------------------------------------


def spool_dir() -> Path:
    path = config.runtime_dir() / SPOOL_DIR
    path.mkdir(exist_ok=True)
    return path


def spool(
    kind: str, agent_id: str, body: str, decision_id: int | None, why: str, decided: NoticeStep
) -> Path | None:
    """Keep a notice the busy store did not take as a file (written whole, then renamed into
    place) and start the helper that records it; None when even that failed (logged)."""
    name = f"{uuid.uuid4().hex}.json"
    spooled = step("record", None, f"spooled: {why}")
    items = [spooled, decided]
    data = {
        "kind": kind,
        "agent_id": agent_id,
        "text": body,
        "decision_id": decision_id,
        "created_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "steps": [vars(item) for item in items],
    }
    try:
        path = spool_dir() / name
        temp = path.with_name(f".{name}.tmp")
        temp.write_text(json.dumps(data))
        temp.replace(path)
    except OSError as exc:
        log_step(name, "drop", DROPPED, actor(), f"{why}; not spooled either ({exc}): lost")
        return None
    logged_name = f"spool:{name}"
    for item in items:
        log_step(logged_name, item.step, item.path, item.actor, item.detail)
    if not spawn(["spool", str(path), OPEN]):
        log_step(
            logged_name, "fallback", None, actor(), "the helper did not start; a poll records it"
        )
    return path


def ingest(store: Store, path: Path, wait: float = STORE_WAIT_SECONDS) -> dict[str, Any] | None:
    """Record a spooled notice (taken by renaming its file, so only one process does), routed
    as now; the notice row, or None (taken by another, or unreadable: dropped, logged). The
    store's error when it did not take it (the file is back in the spool)."""
    taking = path.with_name(f"{path.name}.{os.getpid()}.taking")
    try:
        path.rename(taking)
        data = json.loads(taking.read_text())
        items = [NoticeStep(**item) for item in data.get("steps", [])]
    except OSError:
        return None
    except (ValueError, TypeError) as exc:
        taking.unlink(missing_ok=True)
        log_step(f"spool:{path.name}", "drop", DROPPED, actor(), f"unreadable spool file: {exc}")
        return None
    found = presence()
    choice = route(found)
    late = spooled_for(items)
    items.append(step("record", None, f"recorded from the spool{late}"))
    items.append(step("route", choice.path, f"{choice.reason}; {found.describe()}"))
    try:
        nid = store.add_notice(
            data["kind"],
            data["agent_id"],
            data["text"],
            data.get("decision_id"),
            wait,
            route=choice.taker,
            claimed_by=actor() if choice.at_once else None,
            created_at=data.get("created_at"),
            steps=items,
        )
    except BaseException:
        with contextlib.suppress(OSError):
            taking.rename(path)
        raise
    taking.unlink(missing_ok=True)
    logged(nid, items[-2:])
    return {
        "id": nid,
        "text": data["text"],
        "agent_id": data["agent_id"],
        "decision_id": data.get("decision_id"),
        "choice": choice,
    }


def spooled_for(items: list[NoticeStep]) -> str:
    """ ", N s after it was spooled" from the first step's time ("" without one)."""
    try:
        since = datetime.fromisoformat(items[0].at)
    except IndexError, ValueError, TypeError:
        return ""
    return f", {(datetime.now(UTC) - since).total_seconds():.1f} s after it was spooled"


def ingest_spool(store: Store, detached: bool = False) -> list[int]:
    """Record every spooled notice the store takes now and carry out its route (a poll of the
    window, a page or the posting process does this). The ids recorded."""
    try:
        paths = sorted(spool_dir().glob("*.json"))
    except OSError:
        return []
    done = []
    for path in paths:
        try:
            row = ingest(store, path, wait=0.5)
        except Exception:
            continue  # still busy: the helper or the next poll
        if row is not None:
            done.append(row["id"])
            carry_out(store, row, row["choice"], detached)
    return done


def spool_helper(path: Path, opener: str | None = None, store: Store | None = None) -> None:
    """The helper for a spooled notice: record it, however long the store is busy (opening
    the store is part of each try; up to SPOOL_GIVE_UP_SECONDS), then carry out its route.
    How it ends is logged: recorded (a step, with how late), by a poll, or given up."""
    name = f"spool:{path.name}"
    began = time.monotonic()
    tries, error = 0, "the store stayed busy"
    while True:
        if not path.exists():
            log_step(name, "record", None, actor(), "taken from the spool by a poll; helper ends")
            return
        tries += 1
        try:
            store = store or Store()
            row = ingest(store, path, SPOOL_TRY_SECONDS)
        except Exception as exc:  # busy (the store did not open, or did not take it)
            row, error = None, f"{type(exc).__name__}: {exc}"
        if row is not None:
            carry_out(store, row, row["choice"], detached=True, opener=opener)
            return
        if time.monotonic() - began >= SPOOL_GIVE_UP_SECONDS:
            break
        time.sleep(0.5)
    waited = time.monotonic() - began
    log_step(
        name,
        "fallback",
        None,
        actor(),
        f"gave up after {waited:.1f} s and {tries} tries ({error}); left in the spool for"
        " the next poll of the window, a page or the posting process",
    )


# -- the detached helper: seeing a notice taken ----------------------------------------------


def wait_taken(store: Store, notice: int, taker: str, seconds: float) -> str:
    """Wait until the notice is claimed ("claimed"), left to another ("moved"), gone, or
    `seconds` passed ("late")."""
    deadline = time.monotonic() + seconds
    while True:
        try:
            row = store.notice(notice)
        except Exception:
            row = {"claimed_at": None, "route": taker}  # busy: look again
        if row is None:
            return "gone"
        if row["claimed_at"] is not None:
            return "claimed"
        if row["route"] != taker:
            return "moved"
        if time.monotonic() >= deadline:
            return "late"
        time.sleep(WAIT_STEP)


def deliver(store: Store, notice: int, opener: str | None = None) -> None:
    """See the notice taken by whom `route` left it to; whoever does not take it in time is
    skipped and the notice routed again (escalated), until it is taken, waits, or is shown
    at once. The launcher's app is started here."""
    skip: set[str] = set()
    for _ in range(len(RANK) + 1):
        row = store.notice(notice)
        if row is None or row["claimed_at"] is not None:
            return
        taker = row["route"]
        if taker in (WINDOW, PAGE, BANNER):
            outcome = wait_taken(store, notice, taker, ESCALATE_SECONDS)
            if outcome != "late":
                return
            who = (
                presence().describe()
                if taker != PAGE
                else f"page polled {page_age() or 0:.1f} s ago"
            )
            why = f"{PATHS[taker]} did not take it within {ESCALATE_SECONDS:.0f} s ({who})"
            skip |= {WINDOW, BANNER} if taker in (WINDOW, BANNER) else {PAGE}
        elif taker == HELPER_APP:
            bundle, no = bundle_state()
            problem = no if bundle is None else start_launcher(opener or OPEN, bundle)
            if problem is None:
                outcome = wait_taken(store, notice, taker, CLAIM_WAIT_SECONDS)
                if outcome != "late":
                    return  # claimed, or the posting process left it to wait (and said why)
                problem = f"its app did not take it within {CLAIM_WAIT_SECONDS:.0f} s"
            why = f"launcher-helper: {problem}"
            skip.add(HELPER_APP)
        else:
            return  # waiting, or shown at once
        found = presence()
        choice = route(found, skip)
        moved = step("escalate", choice.path, f"{why}; now {choice.reason}; {found.describe()}")
        claimer = actor() if choice.at_once else None
        if not store.reroute_notice(notice, choice.taker, moved, was=(taker,), claimed_by=claimer):
            return  # taken meanwhile
        logged(notice, [moved])
        if choice.at_once or choice.taker == WAITING:
            carry_out(store, row, choice, detached=True, opener=opener)
            return


def start_launcher(opener: str, bundle: Path) -> str | None:
    """Start the launcher's app hidden to post the pending notices; None, or why it failed."""
    argv = [opener, "-n", "-g", "-j", "-a", str(bundle), "--args", "--notify"]
    try:
        done = subprocess.run(
            argv, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=OPEN_TIMEOUT
        )
    except subprocess.TimeoutExpired:
        return f"`open` took over {OPEN_TIMEOUT:.0f} s"
    except OSError as exc:
        return f"`open` did not run ({exc})"
    if done.returncode != 0:
        said = one_line(done.stderr or done.stdout, 160)
        return f"`open` failed (exit {done.returncode}{': ' + said if said else ''})"
    return None


# -- the takers' side ------------------------------------------------------------------------


def claim(
    store: Store,
    notice: int,
    taker: str,
    path: str | None = None,
    detail: str = "",
    who: str | None = None,
) -> bool:
    """`taker` claims the notice to show it, when it may (`may_take`); traced. True only for
    the one that did."""
    claimed = step("claim", path or PATHS.get(taker, taker), detail, who)
    if store.claim_notice(notice, routes=takeable(taker), by=claimed.actor, step=claimed):
        logged(notice, [claimed])
        return True
    return False


def release_to_wait(store: Store, notice: int, why: str, who: str | None = None) -> bool:
    """A taker could not show the notice it claimed: it waits again (traced)."""
    item = step("release", PATHS[WAITING], why, who)
    if store.release_notice(notice, route=WAITING, by=item.actor, step=item):
        logged(notice, [item])
        return True
    return False


def redirect(
    store: Store, notice: dict[str, Any], why: str, skip: Iterable[str], who: str | None = None
) -> Choice | None:
    """A notice left to (or claimed by) a taker that is going (`who`, this process by
    default): released when it was this one's claim, then routed again without the takers
    in `skip` and carried out. None when another took it meanwhile."""
    nid = notice["id"]
    skip = tuple(skip)
    mine = step("release", None, why, who)
    released = store.release_notice(nid, by=mine.actor, step=mine)
    if released:
        logged(nid, [mine])
    found = presence()
    choice = route(found, skip)
    moved = step("escalate", choice.path, f"{why}; now {choice.reason}; {found.describe()}")
    claimer = actor() if choice.at_once else None
    was = None if released else skip
    if not store.reroute_notice(nid, choice.taker, moved, was=was, claimed_by=claimer):
        return None
    logged(nid, [moved])
    carry_out(store, notice, choice)
    return choice


REASONS = ("fallback", "escalate", "release", "drop", "counted", "error")
OUTCOMES = ("shown", "counted", "drop", "claim")  # a step that says how a notice went


def summary(notice: dict[str, Any]) -> dict[str, Any]:
    """How a traced notice (store.notice_trace) went, as one line's fields: path, actor, at
    (of the step that showed, claimed or left it), reason (of the final outcome: the last
    fallback, escalation, release, drop or error since the last release that was followed by
    a claim, else the claim's or the route's reason; earlier ones stay in the steps). A claim
    without a step (a process of older code) says only who and when."""
    steps = notice.get("steps") or []
    claimed = notice.get("claimed_at")
    if not steps:
        return {
            "path": "shown (older code)" if claimed else "unclaimed (older code)",
            "actor": notice.get("claimed_by") or "-",
            "at": claimed or notice.get("created_at"),
            "reason": "recorded by older code: no trace",
        }
    last = steps[-1]
    route_now = notice.get("route") or ""
    outcome = next((s for s in reversed(steps) if s["step"] in OUTCOMES), None)
    if claimed and outcome is None and route_now not in (OSASCRIPT, LINUX, QUIET):
        return {
            "path": "not recorded",
            "actor": notice.get("claimed_by") or "unknown (older code)",
            "at": claimed,
            "reason": "claimed by a process that records no steps (older code)",
        }
    if claimed:
        last = outcome or last
        path = last["path"] or "-"
    elif route_now == WAITING:
        path = PATHS[WAITING]
    else:
        path = f"{PATHS.get(route_now, 'unclaimed')} (not taken yet)"
    current = steps
    if claimed:  # a release before the final claim belongs to an earlier attempt
        cut = max((i for i, s in enumerate(steps) if s["step"] == "release"), default=-1)
        if cut >= 0 and any(s["step"] == "claim" for s in steps[cut + 1 :]):
            current = steps[cut + 1 :]
    why = next((s for s in reversed(current) if s["step"] in REASONS), None)
    if why is None and current is not steps:
        why = next((s for s in reversed(current) if s["step"] == "claim" and s["detail"]), None)
    why = why or next((s for s in reversed(steps) if s["step"] == "route"), last)
    return {"path": path, "actor": last["actor"], "at": last["at"], "reason": why["detail"]}


def plain(body: str, decision_id: int | None) -> threading.Thread | None:
    """Show the notice with osascript, its text ending with where to look (the caller claimed
    it). Returns the thread that waits for osascript; None when there is none. Never raises."""
    try:
        clause = CLAUSE_FINISHED if decision_id is None else CLAUSE_DECISION
        argv = command(config.PROJECT_NAME, one_line(body, TEXT_CHARS - len(clause)) + clause)
        if argv is None:
            return None
        proc = start_defaulted(
            subprocess.Popen,
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        thread = threading.Thread(target=_reap, args=(proc,), daemon=True)
        thread.start()
        return thread
    except Exception:
        return None


def signals_ignored() -> list[str]:
    """Which of DEFAULT_SIGNALS but SIGPIPE (Python's own, and Popen's restore_signals sets
    it back in a child) this process ignores, so that its children would too."""
    names = [name for name in DEFAULT_SIGNALS if name != "SIGPIPE" and hasattr(signal, name)]
    return [name for name in names if signal.getsignal(getattr(signal, name)) == signal.SIG_IGN]


def with_default_signals(argv: list[str]) -> list[str]:
    """`argv`, started through EXEC_DEFAULT when this process ignores a signal it would pass
    on: the program then starts with every one of DEFAULT_SIGNALS at its default, so a
    SIGTERM ends it (no preexec_fn, which is unsafe with threads)."""
    if not signals_ignored():
        return argv
    program = shutil.which(argv[0]) or argv[0]
    return [sys.executable, "-c", EXEC_DEFAULT, program, *argv[1:]]


def start_defaulted(start: Callable[..., Any], argv: list[str], **options: Any) -> Any:
    """`start(argv, **options)` (Popen or run) through `with_default_signals`; when the shim's
    interpreter cannot be started (OSError: say the tool environment was replaced during a
    long turn), `argv` as it is, as before the shim, so the notice is still shown."""
    wrapped = with_default_signals(argv)
    if wrapped != argv:
        with contextlib.suppress(OSError):
            return start(wrapped, **options)
    return start(argv, **options)


def default_signals() -> None:
    """Set DEFAULT_SIGNALS to their default in this process (the helper, before anything
    else), so it and what it starts end on SIGTERM or SIGINT."""
    if threading.current_thread() is not threading.main_thread():
        return
    for name in DEFAULT_SIGNALS:
        if hasattr(signal, name):
            with contextlib.suppress(OSError, ValueError):
                signal.signal(getattr(signal, name), signal.SIG_DFL)


def spawn(args: list[str]) -> bool:
    """Start the detached helper (`helper(args)` in a process of its own, which sets its
    signals to their default first: `helper`); False when it could not be started."""
    try:
        subprocess.Popen(
            [sys.executable, "-c", HELPER, *args],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except Exception:
        return False
    return True


# -- the launcher's app (macOS) --------------------------------------------------------------


def grant_path() -> Path:
    return config.runtime_dir() / GRANT_FILE


def record_grant(identifier: str, bundle: Path) -> None:
    """The launcher's app at `bundle` may post notifications (seen by its window process)."""
    path = grant_path()
    temp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    temp.write_text(json.dumps({"identifier": identifier, "bundle": str(bundle)}))
    temp.replace(path)


def forget_grant(bundle: Path | None = None) -> None:
    """It may not (any more); with `bundle`, only when the grant is that bundle's."""
    if bundle is None or (granted_bundle() or Path()) == bundle:
        grant_path().unlink(missing_ok=True)


def granted_bundle() -> Path | None:
    """The bundle the grant file names, None without a valid one."""
    try:
        data = json.loads(grant_path().read_text())
        return Path(data["bundle"]) if isinstance(data.get("bundle"), str) else None
    except OSError, ValueError, TypeError, KeyError, AttributeError:
        return None


def interpreter_record(bundle: Path) -> dict | None:
    try:
        data = json.loads(bundle.joinpath(*INTERPRETER_RECORD).read_text())
    except OSError, ValueError:
        return None
    return data if isinstance(data, dict) else None


def interpreter_problem(bundle: Path) -> str | None:
    """Why the launcher's own copy of the Python interpreter cannot run (None: it can): none
    was made, or the interpreter it was copied from changed (a Python upgrade) since
    `spartito app install`."""
    data = interpreter_record(bundle)
    if data is None or not (bundle / "Contents" / "MacOS" / str(data.get("copy", ""))).is_file():
        return "the launcher has no copy of the Python interpreter"
    source, environment = Path(str(data.get("source"))), str(data.get("environment") or "")
    try:
        stat = source.stat()
        same = [stat.st_size, stat.st_mtime_ns] == [data.get("size"), data.get("mtime_ns")]
        resolved = os.path.realpath(environment) == data.get("resolved")
    except OSError:
        same = resolved = False
    if not (same and resolved):
        return f"Python changed since `{config.CLI_NAME} app install` (it copied {data.get('version', '?')})"
    return None


def bundle_state() -> tuple[Path | None, str]:
    """The launcher's app that can post a notification now (allowed: grant file; there; its
    interpreter copy current), or None and why not."""
    bundle = granted_bundle()
    if bundle is None:
        return None, "no grant file (not installed, or not allowed)"
    if not bundle.is_dir():
        return None, f"the granted bundle is gone ({bundle.name})"
    if problem := interpreter_problem(bundle):
        return None, f"unusable: {problem}"
    return bundle, ""


def bundle_ready() -> Path | None:
    return bundle_state()[0]


# -- the detached helper ---------------------------------------------------------------------


def helper(args: list[str]) -> int:
    """`spawn`'s process: ["deliver", notice, open], ["spool", file, open] or ["linux",
    notify-send, body, target, notice] (the notice, for its trace, may be left out, as older
    code did, and is for a notice without an agent). Its signals are set to their default first: it may have been
    started by a runner that ignores SIGTERM and SIGINT (`default_signals`). Never raises."""
    default_signals()
    global ROLE
    was, ROLE = ROLE, "helper"
    try:
        if args and args[0] == "deliver" and len(args) == 3:
            deliver(Store(), int(args[1]), args[2])
        elif args and args[0] == "spool" and len(args) == 3:
            spool_helper(Path(args[1]), args[2])
        elif args and args[0] == "linux" and len(args) in (4, 5):
            notice = int(args[4]) if len(args) == 5 and args[4].isdigit() else None
            linux_helper(args[1], args[2], args[3], notice)
    except Exception as exc:
        helper_failed(args, exc)
        return 1
    finally:
        ROLE = was
    return 0


def helper_failed(args: list[str], exc: Exception) -> None:
    """The helper ends with an error: a line in the log, and a step when the store takes it."""
    item = step("error", None, f"the helper ended: {type(exc).__name__}: {exc}")
    if len(args) > 1 and args[0] == "deliver" and args[1].isdigit():
        try:
            store: Store | None = Store()
        except Exception:
            store = None
        note(store, int(args[1]), item)
        return
    name = f"spool:{Path(args[1]).name}" if len(args) > 1 and args[0] == "spool" else None
    with contextlib.suppress(Exception):
        log_step(name, item.step, item.path, item.actor, item.detail)


# -- 4. Linux --------------------------------------------------------------------------------


def linux_icon() -> str:
    """The icon the launcher installed (gui/launcher.py), else the package's."""
    data = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")
    installed = data / "icons" / "hicolor" / "192x192" / "apps" / f"{DESKTOP_ENTRY}.png"
    return str(installed if installed.is_file() else LINUX_ICON)


def has_actions(send: str) -> bool:
    """Whether this notify-send can wait for a click (`--action` and `--wait`, libnotify
    0.7.10 or later), by its help text."""
    try:
        done = subprocess.run(
            [send, "--help"], stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=5
        )
    except OSError, subprocess.TimeoutExpired:
        return False
    text = done.stdout + done.stderr
    return "--action" in text and "--wait" in text


def open_command(target: str) -> list[str]:
    """`spartito app` at `target` (a checked target; anything else: the plain window)."""
    try:
        target = app_open.check_target(target)
    except ValueError:
        target = ""
    if target.startswith("decision:"):
        return ["--decision", target.removeprefix("decision:")]
    return ["--agent", target] if target else []


def linux_env() -> dict[str, str]:
    """The environment for notify-send: the session bus at its usual place when the
    variable is missing (a service started without it) and a bus is there."""
    env = dict(os.environ)
    if not env.get("DBUS_SESSION_BUS_ADDRESS"):
        bus = Path(env.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}") / "bus"
        if bus.exists():
            env["DBUS_SESSION_BUS_ADDRESS"] = f"unix:path={bus}"
    return env


def linux_argv(send: str, body: str, target: str, actions: bool) -> list[str]:
    argv = [
        send,
        f"--app-name={config.PROJECT_NAME}",
        f"--icon={linux_icon()}",
        f"--hint=string:desktop-entry:{DESKTOP_ENTRY}",
    ]
    if actions:
        argv += ["--wait", "--action=default=Open"]
        text = one_line(body, TEXT_CHARS)
    else:
        command = " ".join([config.CLI_NAME, "app", *open_command(target)])
        clause = f" — open it: {command}"
        text = one_line(body, TEXT_CHARS - len(clause)) + clause
    return [*argv, "--", config.PROJECT_NAME, text]


def display() -> bool:
    return bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))


def linux_helper(
    send: str, body: str, target: str, notice: int | None = None, store: Store | None = None
) -> None:
    """Show the notification unless a burst holds it back (`burst_turn`): a held one is
    counted, and the first held one shows the count once the burst window has passed (or
    itself, when it was the only one). Each held notice is traced (`counted`, path
    LINUX_SUMMARY), and so is how the summing one was shown."""
    began = time.monotonic()
    turn, wake, token = burst_turn()
    if turn == SHOW:
        linux_show(send, body, target)
        return
    trace = Tracer(notice, store)
    if turn == HELD:
        trace("counted", LINUX_SUMMARY, f"held back, {burst_why()}: in the summary popup")
        return
    due = max(0.0, min(wake - time.time(), BURST_SECONDS))
    trace("counted", LINUX_SUMMARY, f"held back, {burst_why()}: shows the summary in {due:.0f} s")
    time.sleep(due)
    held = burst_summarised(token)
    waited = time.monotonic() - began
    if held <= 1:  # only this one was held back, or the count is gone: shown as itself
        alone = "the only one held back, or the others' count was discarded"
        trace("shown", PATHS[LINUX], f"after {waited:.1f} s, as itself: {alone}")
        linux_show(send, body, target)
        return
    text = f"{held} more notices"
    trace("shown", LINUX_SUMMARY, f'after {waited:.1f} s: {held} held back, shown as "{text}"')
    linux_show(send, text, "")


class Tracer:
    """Traces steps of the helper's notice: in the store and the log, or the log alone for a
    notice without an id (no agent, or started by older code)."""

    def __init__(self, notice: int | None, store: Store | None) -> None:
        self.notice, self.store = notice, store

    def __call__(self, kind: str, path: str, detail: str) -> None:
        if self.notice is None:
            log_step(None, kind, path, actor(), detail)
            return
        if self.store is None:
            with contextlib.suppress(Exception):
                self.store = Store()
        note(self.store, self.notice, step(kind, path, detail))


def linux_show(send: str, body: str, target: str) -> None:
    """Show the notification; with actions (a display to open a window on, and a free slot
    of LINUX_SLOTS), wait up to LINUX_WAIT_SECONDS for its click and then start
    `spartito app` at the target. notify-send is killed when the time is up."""
    with waiting_slot(display() and has_actions(send)) as actions:
        argv = linux_argv(send, body, target, actions)
        try:
            done = start_defaulted(
                subprocess.run,
                argv,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                env=linux_env(),
                timeout=LINUX_WAIT_SECONDS if actions else 30,
            )
        except OSError, subprocess.TimeoutExpired:
            return
    if actions and "default" in done.stdout.split():
        start_app(open_command(target))


@contextlib.contextmanager
def waiting_slot(wanted: bool):
    """One of LINUX_SLOTS slots for a helper waiting for a click, held until the block ends
    (a lock, so a helper that died frees its slot); yields whether it got one."""
    if not wanted:
        yield False
        return
    import fcntl

    handles = []
    try:
        folder = config.runtime_dir() / LINUX_SLOT_DIR
        folder.mkdir(exist_ok=True)
        for n in range(LINUX_SLOTS):
            handle = (folder / f"slot-{n}").open("a")
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                handle.close()
                continue
            handles.append(handle)
            break
    except OSError:
        pass
    try:
        yield bool(handles)
    finally:
        for handle in handles:
            handle.close()


SHOW, HELD, SUMMARY = "show", "held", "summary"  # a helper's turn in a burst


def _number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def burst_checked(data: Any) -> dict[str, Any] | None:
    """The burst state when `data` has its shape, else None: {"shown": [times], "held": n,
    "summary": {"pid", "deadline", "token"} or None}."""
    if not isinstance(data, dict):
        return None
    shown, held, owner = data.get("shown"), data.get("held"), data.get("summary")
    if not (isinstance(shown, list) and all(_number(t) for t in shown)):
        return None
    if not (isinstance(held, int) and not isinstance(held, bool) and held >= 0):
        return None
    if owner is not None and not (
        isinstance(owner, dict)
        and isinstance(owner.get("pid"), int)
        and _number(owner.get("deadline"))
        and isinstance(owner.get("token"), str)
    ):
        return None
    return {"shown": [float(t) for t in shown], "held": held, "summary": owner}


@contextlib.contextmanager
def burst_state():
    """The burst state (`burst_checked`), read and written under BURST_LOCK; a file that is
    not one (unreadable, or of another shape) starts afresh, logged, and is rewritten."""
    import fcntl

    folder = config.runtime_dir()
    with (folder / BURST_LOCK).open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        path = folder / BURST_STATE
        try:
            text = path.read_text()
        except OSError:
            text = None
        state = None
        if text is not None:
            with contextlib.suppress(ValueError):
                state = burst_checked(json.loads(text))
            if state is None:
                log_step(
                    None, "fallback", LINUX_SUMMARY, actor(), f"{BURST_STATE}: unusable, reset"
                )
        state = state or {"shown": [], "held": 0, "summary": None}
        yield state
        temp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
        temp.write_text(json.dumps(state))
        temp.replace(path)


def burst_heal(state: dict[str, Any], now: float) -> None:
    """A summing helper that ended (killed while it waited) or is past its deadline: its
    count of held notices is discarded (logged), not added to a later burst."""
    owner = state["summary"]
    if owner is not None and pid_running(owner["pid"]) and now <= owner["deadline"]:
        return
    if state["held"]:
        gone = "ended" if owner is None or not pid_running(owner["pid"]) else "is overdue"
        who = f" (pid {owner['pid']})" if owner else ""
        why = f"{state['held']} held back; their summing helper{who} {gone}: discarded"
        log_step(None, "drop", LINUX_SUMMARY, actor(), why)
    state["held"], state["summary"] = 0, None


def burst_turn() -> tuple[str, float, str]:
    """This helper's turn: SHOW (fewer than BURST_LIMIT popups within BURST_SECONDS), HELD
    (counted; another helper sums it up), or SUMMARY (counted, and this helper shows the sum
    at the time returned, with the token that makes it the owner). Any error: SHOW."""
    now = time.time()
    try:
        with burst_state() as state:
            burst_heal(state, now)
            shown = [t for t in state["shown"] if 0 <= now - t < BURST_SECONDS]
            state["shown"] = shown
            if len(shown) < BURST_LIMIT:
                shown.append(now)
                return SHOW, now, ""
            state["held"] += 1
            if state["summary"] is not None:
                return HELD, now, ""
            wake = min(shown) + BURST_SECONDS
            token = uuid.uuid4().hex
            state["summary"] = {
                "pid": os.getpid(),
                "deadline": wake + BURST_SECONDS,
                "token": token,
            }
            return SUMMARY, wake, token
    except Exception:
        return SHOW, now, ""


def burst_summarised(token: str) -> int:
    """The summing helper's time has come: the count of the held notices (this one's
    included), now taken; the summary counts as a popup. 1 when it is no longer the owner
    (its count was discarded) or on an error: it shows itself."""
    try:
        with burst_state() as state:
            owner = state["summary"]
            if owner is None or owner["token"] != token:
                return 1
            held = state["held"]
            state["held"], state["summary"] = 0, None
            state["shown"] = [*state["shown"], time.time()]
            return max(held, 1)
    except Exception:
        return 1


def burst_why() -> str:
    return f"over {BURST_LIMIT} popups in {BURST_SECONDS:g} s"


def start_app(options: list[str]) -> None:
    subprocess.Popen(
        with_default_signals([sys.executable, "-m", "orchestrator", "app", *options]),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


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
