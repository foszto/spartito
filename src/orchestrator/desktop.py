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
decision), then shown by the first of these that can, each with a click that opens Spartito
at the agent or decision or, where that cannot be had, no misleading click target:

1. a dashboard page allowed to show notifications: it touches a marker file on each poll
   (`page_present`, through the facade); while the marker is fresh (PAGE_FRESH_SECONDS) the
   notice is left to the page, which claims it and shows it as a Web Notification;
2. the Spartito window process on macOS (gui/notifier.py), allowed to notify: the same with
   its own marker (`app_present`, APP_MARKER);
3. macOS, nothing of these running, but the launcher installed and allowed to notify (the
   grant file GRANT_FILE, written by the window process when it sees the grant): a detached
   helper starts the launcher's app hidden (`open -n -g -j -a <bundle> --args --notify`),
   which claims and posts the pending notices and exits; when `open` fails, or the notice is
   still unclaimed after CLAIM_WAIT_SECONDS, the helper shows it with osascript (4.);
4. Linux with `notify-send`: a detached helper shows it, with a default action that opens
   `orch app` at the target where this notify-send has actions (libnotify 0.7.10 or later);
   without them, the text names the command that opens it;
5. macOS otherwise: `osascript`, whose click opens Script Editor and cannot be changed, so
   its text ends with where to look instead (CLAUSE_DECISION, CLAUSE_FINISHED).

Whoever shows a notice claims it first, so it is shown once. A helper that cannot be
started falls through to the next way in the same call. Without an agent nothing is recorded:
4. or 5. shows it.

`osascript` gets the texts as arguments of its run handler, never inside the script source
(they come from agents). Children are started and left alone: a reaper thread kills
osascript after TIMEOUT_SECONDS, a helper ends itself (LINUX_WAIT_SECONDS at most); a missing
binary or any error is ignored. Off with the global setting `desktop_notifications`
(`orch config desktop-notifications off`): then nothing is recorded either.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path

from . import app_open, config, notify, settings
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
APP_MARKER = "app-notifier"  # in config.runtime_dir(): the Spartito window process (2.)
GRANT_FILE = "app-notify.json"  # in config.runtime_dir(): {"identifier", "bundle"} (3.)
INTERPRETER_RECORD = ("Contents", "Resources", "interpreter.json")  # in the bundle (launcher.py)
OPEN = "/usr/bin/open"  # tests set a fake
NOTIFY_SEND = "notify-send"  # looked up on PATH; tests set a fake
CLAIM_WAIT_SECONDS = 15.0  # for the launcher's app to claim the notice (3.)
OPEN_TIMEOUT = 10.0
LINUX_WAIT_SECONDS = 1800.0  # a Linux helper waiting for a click ends after this
CLAUSE_DECISION = " — open Spartito to answer"
CLAUSE_FINISHED = " — open Spartito to see it"
LINUX_ICON = Path(__file__).parent / "web" / "static" / "icon-192.png"
DESKTOP_ENTRY = "spartito"  # the launcher's desktop file name (gui/launcher.py)
HELPER = "import sys; from orchestrator import desktop; sys.exit(desktop.helper(sys.argv[1:]))"


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
    return _fresh(page_marker())


def app_marker() -> Path:
    return config.runtime_dir() / APP_MARKER


def app_present() -> None:
    """The Spartito window process, allowed to notify, is there: refresh its marker."""
    app_marker().touch()


def app_fresh() -> bool:
    return _fresh(app_marker())


def _fresh(marker: Path) -> bool:
    try:
        age = time.time() - marker.stat().st_mtime
    except OSError:
        return False
    return 0 <= age < PAGE_FRESH_SECONDS  # a marker from the future: not to be trusted


def target_of(agent_id: str | None, decision_id: int | None) -> str:
    """Where a click should open Spartito: "decision:<n>", the agent, or "" (anywhere)."""
    if decision_id is not None:
        return f"decision:{decision_id}"
    return agent_id or ""


def show(
    store: Store, body: str, agent_id: str | None = None, decision_id: int | None = None
) -> None:
    """Notify about `agent_id` (a decision notice with `decision_id`, else a finished one)
    under the tool's name, unless switched off: record the notice, then leave it to whoever
    can show it best (see the module doc). Never raises and never waits for a child."""
    try:
        if not settings.desktop_notifications(store):
            return
        notice = _record(store, body, agent_id, decision_id)
        if notice is not None and (page_fresh() or app_fresh()):
            return
        if PLATFORM == "darwin" and notice is not None and (bundle := bundle_ready()):
            argv = ["bundle", OPEN, str(bundle), str(notice), body, _number(decision_id)]
            if spawn(argv):
                return
        if PLATFORM.startswith("linux") and (send := shutil.which(NOTIFY_SEND)):
            if notice is not None and not _claim(store, notice):
                return  # a page showed it meanwhile
            spawn(["linux", send, body, target_of(agent_id, decision_id)])
            return  # no other way on Linux
        plain(store, notice, body, decision_id)
    except Exception:
        pass


def _number(value: int | None) -> str:
    return "" if value is None else str(value)


def plain(store: Store | None, notice: int | None, body: str, decision_id: int | None) -> None:
    """5. Claim the notice (when there is one and a store) and show it with osascript, its
    text ending with where to look. Never raises."""
    try:
        clause = CLAUSE_FINISHED if decision_id is None else CLAUSE_DECISION
        argv = command(config.PROJECT_NAME, one_line(body, TEXT_CHARS - len(clause)) + clause)
        if argv is None:
            return
        if store is not None and notice is not None and not _claim(store, notice):
            return  # someone showed it meanwhile
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


def spawn(args: list[str]) -> bool:
    """Start the detached helper (`helper(args)` in a process of its own); False when it
    could not be started."""
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
    """Claim the notice to show it; one that cannot be claimed (the store is busy) is shown
    anyway, as before notices existed."""
    try:
        return store.claim_notice(notice, STORE_WAIT_SECONDS)
    except Exception:
        return True


# -- 3. the launcher's app (macOS) -----------------------------------------------------------


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
    `orch app install`."""
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
        return f"Python changed since `orch app install` (it copied {data.get('version', '?')})"
    return None


def bundle_ready() -> Path | None:
    """The launcher's app that can post a notification now: allowed (grant file), there, and
    its interpreter copy current; None otherwise."""
    bundle = granted_bundle()
    if bundle is None or not bundle.is_dir() or interpreter_problem(bundle):
        return None
    return bundle


def unclaimed(store: Store, notice: int) -> bool:
    return any(row["id"] == notice for row in store.notices(notice - 1))


# -- the detached helper ---------------------------------------------------------------------


def helper(args: list[str]) -> int:
    """`spawn`'s process: ["bundle", open, bundle, notice, body, decision] or
    ["linux", notify-send, body, target]. Never raises."""
    try:
        if args and args[0] == "bundle" and len(args) == 6:
            bundle_helper(args[1], Path(args[2]), int(args[3]), args[4], args[5])
        elif args and args[0] == "linux" and len(args) == 4:
            linux_helper(args[1], args[2], args[3])
    except Exception:
        return 1
    return 0


def bundle_helper(opener: str, bundle: Path, notice: int, body: str, decision: str) -> None:
    """Start the launcher's app to post the pending notices; when that fails, or the notice is
    still unclaimed after CLAIM_WAIT_SECONDS, show it with osascript."""
    store = Store()
    argv = [opener, "-n", "-g", "-j", "-a", str(bundle), "--args", "--notify"]
    try:
        started = (
            subprocess.run(
                argv, stdin=subprocess.DEVNULL, capture_output=True, timeout=OPEN_TIMEOUT
            ).returncode
            == 0
        )
    except OSError, subprocess.TimeoutExpired:
        started = False
    if started:
        deadline = time.monotonic() + CLAIM_WAIT_SECONDS
        while time.monotonic() < deadline:
            if not unclaimed(store, notice):
                return
            time.sleep(0.25)
    plain(store, notice, body, int(decision) if decision else None)


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
    """`orch app` at `target` (a checked target; anything else: the plain window)."""
    try:
        target = app_open.check_target(target)
    except ValueError:
        target = ""
    if target.startswith("decision:"):
        return ["--decision", target.removeprefix("decision:")]
    return ["--agent", target] if target else []


def linux_env() -> dict[str, str]:
    """The environment for notify-send: the session bus at its usual place when the
    variable is missing (a service started without it)."""
    env = dict(os.environ)
    if not env.get("DBUS_SESSION_BUS_ADDRESS"):
        runtime = env.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}"
        env["DBUS_SESSION_BUS_ADDRESS"] = f"unix:path={runtime}/bus"
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
        command = " ".join(["orch", "app", *open_command(target)])
        clause = f" — open it: {command}"
        text = one_line(body, TEXT_CHARS - len(clause)) + clause
    return [*argv, "--", config.PROJECT_NAME, text]


def display() -> bool:
    return bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))


def linux_helper(send: str, body: str, target: str) -> None:
    """Show the notification; with actions (and a display to open a window on), wait up to
    LINUX_WAIT_SECONDS for its click and then start `orch app` at the target."""
    actions = display() and has_actions(send)
    argv = linux_argv(send, body, target, actions)
    try:
        done = subprocess.run(
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


def start_app(options: list[str]) -> None:
    subprocess.Popen(
        [sys.executable, "-m", "orchestrator", "app", *options],
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
