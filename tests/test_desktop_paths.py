"""Who shows a desktop notification (desktop.show): a page, the Spartito window process, the
launcher's app started to post, the Linux helper, or osascript, and the fallbacks between them.

Nothing real is shown or started: osascript and notify-send are the fakes of conftest.py,
`open` is a fake written here, and the helper runs in this process unless a test runs
it as the detached process on purpose (with fakes only).
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest
from conftest import FakeNotifySend
from test_decisions import calls
from test_notifications import until
from typer.testing import CliRunner

from orchestrator import app_open, cli, desktop
from orchestrator.service import Orchestrator

BODY = "a-1 finished"


@pytest.fixture
def macos(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(desktop, "PLATFORM", "darwin")


REAL_SPAWN = desktop.spawn  # before conftest.py's no_detached_helpers replaces it


@pytest.fixture
def spawned(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    """desktop.spawn, noted instead of started (it succeeds unless `spawned.fail`)."""
    noted: list[list[str]] = []

    def spawn(args: list[str]) -> bool:
        noted.append(args)
        return not getattr(spawn, "fail", False)

    monkeypatch.setattr(desktop, "spawn", spawn)
    return noted


def texts(log: Path) -> list[str]:
    return [call[-1] for call in calls(log)]


def fake_bundle(root: Path, environment: Path | None = None) -> Path:
    """A launcher bundle with a current interpreter copy and its record (launcher.py)."""
    bundle = root / "Spartito Test.app"
    (bundle / "Contents" / "MacOS").mkdir(parents=True)
    (bundle / "Contents" / "Resources").mkdir(parents=True)
    (bundle / "Contents" / "MacOS" / "spartito-python").write_bytes(b"copy")
    source = root / "python3.14"
    source.write_bytes(b"the interpreter")
    environment = environment or source
    stat = source.stat()
    record = {
        "copy": "spartito-python",
        "source": str(source),
        "size": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
        "environment": str(environment),
        "resolved": os.path.realpath(environment),
        "version": "3.14.6",
    }
    bundle.joinpath(*desktop.INTERPRETER_RECORD).write_text(json.dumps(record))
    return bundle


# -- the order (desktop.route) ----------------------------------------------------------------

LIVE = desktop.Window(1, True, 0.5, "allowed")
DENIED = desktop.Window(1, True, 0.5, "denied")
LATE = desktop.Window(1, True, 120.0, "allowed")  # its marker is late, its process runs
GONE = desktop.Window(1, False, 0.5, "allowed", "its process is gone")
BUNDLE = Path("/Applications/X.app")


def mac(window=None, page=None, bundle=None) -> desktop.Presence:
    return desktop.Presence("darwin", window, page, bundle, "" if bundle else "no grant file")


@pytest.mark.parametrize(
    ("found", "skip", "taker"),
    [
        (mac(LIVE, 1.0, BUNDLE), (), "window"),  # first: a window that may post
        (mac(LATE), (), "window"),  # its process runs: present though its marker is late
        (mac(DENIED, 1.0, BUNDLE), (), "helper"),  # then the launcher's app
        (mac(GONE, 1.0, BUNDLE), (), "helper"),
        (mac(DENIED, 1.0), (), "page"),  # then a page
        (mac(LIVE, 1.0, BUNDLE), ("window",), "helper"),  # escalated: the window did not take it
        (mac(LIVE, 1.0, BUNDLE), ("window", "helper"), "page"),
        (mac(DENIED), (), "banner"),  # then the window that shows it inside
        (mac(None, 1.0, BUNDLE), ("helper", "page"), "waiting"),  # a launcher: never osascript
        (mac(LIVE), ("window",), "waiting"),  # a window runs: it waits for it
        (mac(None, 30.0), (), "osascript"),  # nothing at all (the page is stale)
        (mac(GONE), (), "osascript"),
        (desktop.Presence("linux", None, 1.0, None, "", "/bin/notify-send"), (), "page"),
        (desktop.Presence("linux", None, None, None, "", "/bin/notify-send"), (), "linux"),
        (desktop.Presence("linux"), (), "waiting"),
        (desktop.Presence("win32"), (), "waiting"),
    ],
)
def test_the_route(found: desktop.Presence, skip: tuple[str, ...], taker: str):
    choice = desktop.route(found, skip)
    assert choice.taker == taker and choice.path == desktop.PATHS[taker] and choice.reason


@pytest.mark.parametrize(
    ("taker", "takes"),
    [
        ("window", {None, "window", "helper", "page", "banner", "waiting"}),
        ("helper", {None, "helper", "page", "banner", "waiting"}),
        ("page", {None, "page", "banner", "waiting"}),
        ("banner", {None, "banner", "waiting"}),
    ],
)
def test_a_taker_claims_what_is_left_to_it_or_to_a_later_way(taker: str, takes: set):
    routes = {None, "window", "helper", "page", "banner", "waiting", "osascript", "linux"}
    assert {route for route in routes if desktop.may_take(taker, route)} == takes


def steps_of(orch: Orchestrator, notice: int) -> list[tuple[str, str | None]]:
    (traced,) = [n for n in orch.notice_trace(50) if n["id"] == notice]
    return [(item["step"], item["path"]) for item in traced["steps"]]


def test_show_leaves_it_to_the_window_and_a_helper_watches(
    orch: Orchestrator, fake_osascript, macos, spawned, tmp_path
):
    desktop.record_grant("local.x.app", fake_bundle(tmp_path))
    desktop.page_present()
    desktop.app_present("allowed")
    desktop.show(orch.store, BODY, "a-1")
    (notice,) = orch.notices()
    assert notice["route"] == "window"
    assert spawned == [["deliver", str(notice["id"]), desktop.OPEN]]
    assert steps_of(orch, notice["id"]) == [("record", None), ("route", "window-system")]
    assert calls(fake_osascript) == []


def test_a_late_window_marker_with_a_live_process_still_counts(
    orch: Orchestrator, fake_osascript, macos, spawned
):
    desktop.app_present("allowed")
    then = time.time() - desktop.PAGE_FRESH_SECONDS - 60
    os.utime(desktop.app_marker(), (then, then))
    window = desktop.window_presence()
    assert window is not None and window.alive and window.age > desktop.PAGE_FRESH_SECONDS
    desktop.show(orch.store, BODY, "a-1")
    (notice,) = orch.notices()
    assert notice["route"] == "window" and calls(fake_osascript) == []


def test_a_reused_pid_is_not_the_window(orch: Orchestrator, macos, monkeypatch):
    desktop.app_present("allowed")
    data = json.loads(desktop.app_state_path().read_text())
    data["start"] = "Mon Jan  1 00:00:00 2001"  # this pid's process started later
    desktop.app_state_path().write_text(json.dumps(data))
    window = desktop.window_presence()
    assert window is not None and not window.alive
    assert window.why == "its pid belongs to another process now"


def test_then_the_allowed_launcher_is_started_to_post(
    orch: Orchestrator, fake_osascript, macos, spawned, tmp_path
):
    bundle = fake_bundle(tmp_path)
    desktop.record_grant("local.x.app", bundle)
    desktop.show(orch.store, "Decision needed: which?", "a-1", 7)
    (notice,) = orch.notices()  # unclaimed: the launcher's app claims it
    assert notice["route"] == "helper"
    assert spawned == [["deliver", str(notice["id"]), desktop.OPEN]]
    assert calls(fake_osascript) == []


@pytest.mark.parametrize("why", ["no grant", "no bundle", "stale copy", "other platform"])
def test_without_a_launcher_that_may_post_osascript_shows_it(
    orch: Orchestrator, fake_osascript, macos, spawned, tmp_path, monkeypatch, why
):
    bundle = fake_bundle(tmp_path)
    if why != "no grant":
        desktop.record_grant("local.x.app", bundle)
    if why == "no bundle":
        desktop.record_grant("local.x.app", tmp_path / "Gone.app")
    if why == "stale copy":
        (tmp_path / "python3.14").write_bytes(b"an upgraded interpreter")
    if why == "other platform":
        monkeypatch.setattr(desktop, "PLATFORM", "win32")
    desktop.show(orch.store, BODY, "a-1")
    if why == "other platform":
        time.sleep(0.3)
        assert calls(fake_osascript) == [] and spawned == []
        assert [n["route"] for n in orch.notices()] == ["waiting"]
        return
    until(lambda: calls(fake_osascript), what="the notification")
    assert spawned == [] and texts(fake_osascript) == [f"{BODY} — open Spartito to see it"]
    assert orch.notices() == []
    (traced,) = orch.notice_trace(1)
    reason = {"no grant": "no grant file", "no bundle": "is gone", "stale copy": "Python changed"}
    assert traced["summary"]["path"] == "osascript" and reason[why] in traced["summary"]["reason"]


def test_a_helper_that_cannot_be_started_is_traced_never_osascript(
    orch: Orchestrator, fake_osascript, macos, spawned, tmp_path
):
    desktop.record_grant("local.x.app", fake_bundle(tmp_path))
    desktop.spawn.fail = True  # type: ignore[attr-defined]
    desktop.show(orch.store, BODY, "a-1")
    assert len(spawned) == 1
    time.sleep(0.3)
    assert calls(fake_osascript) == []
    (notice,) = orch.notices()
    assert steps_of(orch, notice["id"])[-1] == ("fallback", "launcher-helper")


def test_without_an_agent_nothing_is_recorded_and_osascript_shows_it(
    orch: Orchestrator, fake_osascript, macos, spawned, tmp_path
):
    desktop.show(orch.store, "hello")
    until(lambda: calls(fake_osascript), what="the notification")
    assert spawned == [] and texts(fake_osascript) == ["hello — open Spartito to see it"]
    assert "shown osascript: no agent, not recorded" in desktop.notice_log().read_text()


def test_show_does_not_wait_for_the_helper(orch: Orchestrator, macos, tmp_path, monkeypatch):
    """The real spawn: a detached process, started and left alone."""
    started: list[float] = []
    real_popen = subprocess.Popen

    def slow(argv, **options):
        started.append(time.monotonic())
        assert options["start_new_session"] and options["stdin"] == subprocess.DEVNULL
        return real_popen([sys.executable, "-c", "import time; time.sleep(3)"], **options)

    monkeypatch.setattr(desktop, "spawn", REAL_SPAWN)
    monkeypatch.setattr(desktop.subprocess, "Popen", slow)
    desktop.record_grant("local.x.app", fake_bundle(tmp_path))
    begin = time.monotonic()
    desktop.show(orch.store, BODY, "a-1")
    assert started and time.monotonic() - begin < 1.5


# -- the osascript text ----------------------------------------------------------------------


def test_the_osascript_text_says_where_to_look(orch: Orchestrator, fake_osascript, macos):
    desktop.show(orch.store, "Decision needed: which?", "a-1", 3)
    until(lambda: len(calls(fake_osascript)) == 1, what="the first")  # one writer at a time
    desktop.show(orch.store, "x" * 400, "a-2")
    until(lambda: len(calls(fake_osascript)) == 2, what="both notifications")
    decision, long = sorted(texts(fake_osascript), key=len)
    assert decision == "Decision needed: which? — open Spartito to answer"
    assert long.endswith("…" + desktop.CLAUSE_FINISHED) and len(long) == desktop.TEXT_CHARS


# -- the grant file --------------------------------------------------------------------------


def test_the_grant_file(tmp_path: Path):
    bundle, other = tmp_path / "A.app", tmp_path / "B.app"
    assert desktop.granted_bundle() is None
    desktop.record_grant("local.a.app", bundle)
    assert json.loads(desktop.grant_path().read_text()) == {
        "identifier": "local.a.app",
        "bundle": str(bundle),
    }
    assert desktop.granted_bundle() == bundle
    desktop.forget_grant(other)  # another bundle's: kept
    assert desktop.granted_bundle() == bundle
    desktop.forget_grant(bundle)
    assert desktop.granted_bundle() is None
    for broken in ("not json", "[]", '{"bundle": 3}'):
        desktop.grant_path().write_text(broken)
        assert desktop.granted_bundle() is None and desktop.bundle_ready() is None
    desktop.forget_grant()
    assert not desktop.grant_path().exists()


def test_the_interpreter_record(tmp_path: Path):
    environment = tmp_path / "env-python"  # the environment's interpreter, a symlink
    environment.symlink_to(tmp_path / "python3.14")
    bundle = fake_bundle(tmp_path, environment)
    assert desktop.interpreter_problem(bundle) is None
    stale = "Python changed since `spartito app install` (it copied 3.14.6)"
    environment.unlink()
    environment.symlink_to(tmp_path / "python3.15")  # the environment moved to another one
    (tmp_path / "python3.15").write_bytes(b"newer")
    assert desktop.interpreter_problem(bundle) == stale
    environment.unlink()
    environment.symlink_to(tmp_path / "python3.14")
    assert desktop.interpreter_problem(bundle) is None
    (tmp_path / "python3.14").unlink()  # the copied one is gone (a Homebrew upgrade)
    assert desktop.interpreter_problem(bundle) == stale
    (bundle / "Contents" / "MacOS" / "spartito-python").unlink()
    assert (
        desktop.interpreter_problem(bundle) == "the launcher has no copy of the Python interpreter"
    )
    bundle.joinpath(*desktop.INTERPRETER_RECORD).write_text("[]")
    assert (
        desktop.interpreter_problem(bundle) == "the launcher has no copy of the Python interpreter"
    )


# -- the detached helper: the launcher's app, escalation -----------------------------------------

FAKE_OPEN = """#!/bin/sh
printf '%s\\n' "$@" > "$(dirname "$0")/open.args"
exit "${FAKE_OPEN_EXIT:-0}"
"""


@pytest.fixture
def fake_open(tmp_path: Path) -> Path:
    path = tmp_path / "bin" / "open"
    path.parent.mkdir()
    path.write_text(FAKE_OPEN)
    path.chmod(0o755)
    return path


def left_to(orch: Orchestrator, route: str, body: str = BODY, decision: int | None = None) -> int:
    return orch.store.add_notice(
        "finished" if decision is None else "decision", "a-1", body, decision, route=route
    )


def test_the_helper_starts_the_bundle_hidden_and_leaves_the_notice_to_it(
    orch: Orchestrator, fake_osascript, macos, fake_open, tmp_path
):
    desktop.record_grant("local.x.app", fake_bundle(tmp_path))
    notice = left_to(orch, "helper")
    threading.Timer(0.3, orch.claim_notice, (notice, "helper")).start()  # its posting process
    desktop.deliver(orch.store, notice, str(fake_open))
    args = (fake_open.parent / "open.args").read_text().splitlines()
    assert args == [
        "-n",
        "-g",
        "-j",
        "-a",
        str(tmp_path / "Spartito Test.app"),
        "--args",
        "--notify",
    ]
    time.sleep(0.3)
    assert calls(fake_osascript) == []


@pytest.mark.parametrize("why", ["open fails", "nobody claims"])
def test_when_the_launchers_app_does_not_take_it_it_waits_never_osascript(
    orch: Orchestrator, fake_osascript, macos, fake_open, tmp_path, monkeypatch, why
):
    monkeypatch.setattr(desktop, "CLAIM_WAIT_SECONDS", 0.5)
    if why == "open fails":
        monkeypatch.setenv("FAKE_OPEN_EXIT", "1")
    desktop.record_grant("local.x.app", fake_bundle(tmp_path))
    notice = left_to(orch, "helper", "Decision needed", 4)
    desktop.deliver(orch.store, notice, str(fake_open))
    time.sleep(0.3)
    assert calls(fake_osascript) == []
    assert [n["route"] for n in orch.notices()] == ["waiting"]
    (traced,) = orch.notice_trace(1)
    said = {"open fails": "`open` failed (exit 1)", "nobody claims": "did not take it within"}
    assert said[why] in traced["summary"]["reason"] and traced["summary"]["path"] == "waiting"


def test_a_window_that_does_not_take_its_notice_is_escalated_to_the_launcher(
    orch: Orchestrator, fake_osascript, macos, fake_open, tmp_path, monkeypatch
):
    """Present (its process runs) but not polling: after ESCALATE_SECONDS the launcher's app
    is started instead."""
    monkeypatch.setattr(desktop, "ESCALATE_SECONDS", 0.3)
    monkeypatch.setattr(desktop, "CLAIM_WAIT_SECONDS", 3)
    desktop.record_grant("local.x.app", fake_bundle(tmp_path))
    desktop.app_present("allowed")
    notice = left_to(orch, "window")
    claimed = threading.Event()

    def posting_process() -> None:
        until(lambda: (fake_open.parent / "open.args").exists(), what="the start")
        claimed.set() if orch.claim_notice(notice, "helper") else None

    threading.Thread(target=posting_process, daemon=True).start()
    desktop.deliver(orch.store, notice, str(fake_open))
    assert claimed.wait(5) and calls(fake_osascript) == []  # set just after the claim
    assert steps_of(orch, notice)[0][0] == "escalate"
    assert "window-system did not take it within" in orch.notice_trace(1)[0]["steps"][0]["detail"]


@pytest.mark.skipif(sys.platform != "darwin", reason="the helper process routes as this platform")
def test_the_detached_helper_runs_as_its_own_process(
    orch: Orchestrator, fake_osascript, fake_open, tmp_path, monkeypatch
):
    """The command line `spawn` starts, run for real (with the fake open, which fails, and
    the fake osascript of the PATH): the notice waits, nothing is shown."""
    monkeypatch.setenv("FAKE_OPEN_EXIT", "1")
    desktop.record_grant("local.x.app", fake_bundle(tmp_path))
    notice = left_to(orch, "helper")
    args = ["deliver", str(notice), str(fake_open)]
    done = subprocess.run([sys.executable, "-c", desktop.HELPER, *args], timeout=60)
    assert done.returncode == 0
    assert [n["route"] for n in orch.notices()] == ["waiting"] and calls(fake_osascript) == []
    assert orch.notice_trace(1)[0]["steps"][-1]["actor"].startswith("helper:")
    junk = subprocess.run([sys.executable, "-c", desktop.HELPER, "nonsense"], timeout=60)
    assert junk.returncode == 0


# -- 4. Linux --------------------------------------------------------------------------------


@pytest.fixture
def notify_send(
    fake_notify_send: FakeNotifySend, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> FakeNotifySend:
    """The fake notify-send of conftest.py, as this process's notify-send, and a display."""
    monkeypatch.setattr(desktop, "NOTIFY_SEND", str(fake_notify_send.path))
    monkeypatch.setenv("DISPLAY", ":0")
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    return fake_notify_send


@pytest.fixture
def started(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    noted: list[list[str]] = []
    monkeypatch.setattr(desktop, "start_app", noted.append)
    return noted


def sent(fake: FakeNotifySend) -> list[str]:
    """The arguments of the last call of the fake notify-send."""
    return fake.calls()[-1]


def test_linux_with_notify_send_hands_the_claimed_notice_to_the_helper(
    orch: Orchestrator, fake_osascript, spawned, notify_send, monkeypatch
):
    monkeypatch.setattr(desktop, "PLATFORM", "linux")
    desktop.show(orch.store, "Decision needed", "a-1", 9)
    assert spawned == [["linux", str(notify_send.path), "Decision needed", "decision:9", "1"]]
    assert orch.notices() == [] and calls(fake_osascript) == []  # claimed: shown once


def test_linux_without_notify_send_shows_nothing(
    orch: Orchestrator, fake_osascript, spawned, monkeypatch
):
    monkeypatch.setattr(desktop, "PLATFORM", "linux")
    desktop.show(orch.store, BODY, "a-1")
    time.sleep(0.3)
    assert spawned == [] and calls(fake_osascript) == [] and len(orch.notices()) == 1


def test_linux_with_actions_a_click_opens_the_window_at_the_target(
    notify_send, started, monkeypatch, tmp_path
):
    monkeypatch.setenv("FAKE_ACTIONS", "1")
    monkeypatch.setenv("FAKE_INVOKED", "default")
    desktop.linux_helper(str(notify_send.path), "Decision needed", "decision:5")
    assert sent(notify_send) == [
        "--app-name=Spartito",
        f"--icon={desktop.LINUX_ICON}",
        "--hint=string:desktop-entry:spartito",
        "--wait",
        "--action=default=Open",
        "--",
        "Spartito",
        "Decision needed",
    ]
    assert started == [["--decision", "5"]]
    desktop.linux_helper(str(notify_send.path), BODY, "a-1")
    assert started[-1] == ["--agent", "a-1"]


@pytest.mark.parametrize("why", ["dismissed", "no display"])
def test_linux_without_a_click_nothing_opens(notify_send, started, monkeypatch, why):
    monkeypatch.setenv("FAKE_ACTIONS", "1")
    if why == "no display":
        monkeypatch.delenv("DISPLAY")
        monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
        monkeypatch.setenv("FAKE_INVOKED", "default")
    desktop.linux_helper(str(notify_send.path), BODY, "a-1")
    assert started == []
    assert ("--wait" in sent(notify_send)) == (why == "dismissed")


def test_linux_without_actions_the_text_names_the_command(notify_send, started, monkeypatch):
    monkeypatch.setenv("FAKE_INVOKED", "default")  # not asked for: ignored
    desktop.linux_helper(str(notify_send.path), "Decision needed", "decision:5")
    args = sent(notify_send)
    assert "--wait" not in args and not any(a.startswith("--action") for a in args)
    assert args[-1] == "Decision needed — open it: spartito app --decision 5"
    desktop.linux_helper(str(notify_send.path), BODY, "a-1")
    assert sent(notify_send)[-1] == f"{BODY} — open it: spartito app --agent a-1"
    desktop.linux_helper(str(notify_send.path), BODY, "")
    assert sent(notify_send)[-1] == f"{BODY} — open it: spartito app"
    assert started == []


def test_linux_targets_are_checked_before_use(notify_send, started, monkeypatch):
    monkeypatch.setenv("FAKE_ACTIONS", "1")
    monkeypatch.setenv("FAKE_INVOKED", "default")
    desktop.linux_helper(str(notify_send.path), BODY, "--port=1; rm -rf x")
    assert started == [[]]  # the plain window
    assert desktop.open_command("decision:12") == ["--decision", "12"]
    assert desktop.open_command("decision:x") == []


def test_linux_helper_has_a_time_limit(notify_send, started, monkeypatch):
    """notify-send killed after the wait (0.5 s), not let run for its 120 s: the bound is far
    from both, so a slow machine does not matter."""
    monkeypatch.setenv("FAKE_ACTIONS", "1")
    monkeypatch.setenv("FAKE_SLEEP", "120")
    monkeypatch.setattr(desktop, "LINUX_WAIT_SECONDS", 0.5)
    begin = time.monotonic()
    desktop.linux_helper(str(notify_send.path), BODY, "a-1")
    assert time.monotonic() - begin < 60 and started == []


def test_linux_session_bus_fallback(notify_send, started, monkeypatch, tmp_path):
    """Without the variable: the bus at its usual place, only when a bus is there."""
    monkeypatch.delenv("DBUS_SESSION_BUS_ADDRESS", raising=False)
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    desktop.linux_helper(str(notify_send.path), BODY, "a-1")
    assert notify_send.buses()[-1] == ""  # no bus there: none invented
    (tmp_path / "bus").touch()
    desktop.linux_helper(str(notify_send.path), BODY, "a-1")
    assert notify_send.buses()[-1] == f"unix:path={tmp_path}/bus"
    monkeypatch.setenv("DBUS_SESSION_BUS_ADDRESS", "unix:path=/elsewhere")
    desktop.linux_helper(str(notify_send.path), BODY, "a-1")
    assert notify_send.buses()[-1] == "unix:path=/elsewhere"


def test_linux_icon_prefers_the_installed_one(notify_send, tmp_path):
    assert desktop.linux_icon() == str(desktop.LINUX_ICON) and desktop.LINUX_ICON.is_file()
    icon = tmp_path / "data" / "icons" / "hicolor" / "192x192" / "apps" / "spartito.png"
    icon.parent.mkdir(parents=True)
    icon.write_bytes(b"png")
    assert desktop.linux_icon() == str(icon)


# -- no test reaches the real desktop --------------------------------------------------------


def test_the_tests_and_their_children_find_only_fake_notifiers(
    fake_notify_send: FakeNotifySend, fake_osascript: Path
):
    """Every process of a test resolves notify-send and osascript to the fakes of conftest.py,
    and finds no session bus or display, even a child given no PATH of its own."""
    assert shutil.which("notify-send") == str(fake_notify_send.path)
    assert shutil.which("osascript") == str(fake_osascript.parent / "osascript")
    bus = os.environ["DBUS_SESSION_BUS_ADDRESS"].removeprefix("unix:path=")
    assert not Path(bus).exists() and Path(bus).is_relative_to(os.environ["XDG_RUNTIME_DIR"])
    assert not desktop.display()
    show = "import shutil; print(shutil.which('notify-send')); print(shutil.which('osascript'))"
    done = subprocess.run([sys.executable, "-c", show], capture_output=True, text=True, check=True)
    assert done.stdout.split() == [
        str(fake_notify_send.path),
        str(fake_osascript.parent / "osascript"),
    ]


def helper_pids(text: str) -> list[int]:
    """The live desktop helpers and notify-sends whose command line has `text`."""
    found = []
    for entry in Path("/proc").iterdir():
        try:
            argv = (entry / "cmdline").read_bytes()
        except OSError:
            continue
        if entry.name.isdigit() and text.encode() in argv and b"notify" in argv:
            found.append(int(entry.name))
    return found


@pytest.mark.skipif(not Path("/proc").is_dir(), reason="looks for the helper in /proc")
def test_a_linux_notice_through_the_detached_helper_reaches_only_the_fake(
    orch: Orchestrator, fake_notify_send: FakeNotifySend, monkeypatch
):
    """The real path, as a runner or an MCP server takes it: notify-send looked up on PATH,
    the detached helper started for real. The fake notes the call, and the helper ends."""
    monkeypatch.setattr(desktop, "PLATFORM", "linux")
    monkeypatch.setattr(desktop, "NOTIFY_SEND", "notify-send")  # as outside the tests
    monkeypatch.setattr(desktop, "spawn", REAL_SPAWN)
    text = f"guard-{os.getpid()}-{time.monotonic_ns()} finished"
    desktop.show(orch.store, text, "a-1")
    until(lambda: fake_notify_send.calls(), what="the fake notify-send's call")
    assert fake_notify_send.programs() == [str(fake_notify_send.path)]
    assert fake_notify_send.calls()[0][-2:] == [
        "Spartito",
        f"{text} — open it: spartito app --agent a-1",
    ]
    until(lambda: not helper_pids(text), what="the helper's end")


@pytest.mark.skipif(sys.platform != "linux", reason="the runner notifies as its platform")
def test_a_runner_on_linux_notifies_through_the_fake(
    orch: Orchestrator, project: str, fake_notify_send: FakeNotifySend
):
    """A runner process (the tests' in-process fakes do not reach it) finds the fake
    notify-send on its PATH: the "finished" notice of a turn the owner asked for."""
    agent, _ = orch.spawn(project, "hello", worktree=False)
    until(lambda: fake_notify_send.calls(), what="the runner's notification")
    assert fake_notify_send.programs() == [str(fake_notify_send.path)]
    assert fake_notify_send.calls()[0][-1].startswith(f"{agent.id} finished")
    assert fake_notify_send.buses() == [os.environ["DBUS_SESSION_BUS_ADDRESS"]]


# -- signals: a helper started while a runner ignores SIGTERM still ends ----------------------

STOP_SIGNALS = {"SIGTERM": 15, "SIGINT": 2, "SIGHUP": 1, "SIGPIPE": 13}
on_proc = pytest.mark.skipif(not Path("/proc").is_dir(), reason="reads /proc/<pid>/status")


def status(pid: int) -> dict[str, str]:
    """/proc/<pid>/status as fields; {} when the process is gone."""
    try:
        lines = Path(f"/proc/{pid}/status").read_text().splitlines()
    except OSError:
        return {}
    return dict(line.split(":\t", 1) for line in lines if ":\t" in line)


def ignored(pid: int) -> set[str]:
    """Which of STOP_SIGNALS the process ignores (SigIgn)."""
    mask = int(status(pid)["SigIgn"], 16)
    return {name for name, number in STOP_SIGNALS.items() if mask & (1 << (number - 1))}


def alive(pid: int) -> bool:
    return status(pid).get("State", "Z")[:1] not in ("Z", "X")


def children(pid: int) -> list[int]:
    return [
        int(entry.name)
        for entry in Path("/proc").iterdir()
        if entry.name.isdigit() and status(int(entry.name)).get("PPid") == str(pid)
    ]


# As runner.main ends a turn: SIGTERM and SIGINT ignored while run_ended shows the notice.
IGNORING_RUNNER = """
import signal, sys
from orchestrator import desktop
signal.signal(signal.SIGTERM, signal.SIG_IGN)
signal.signal(signal.SIGINT, signal.SIG_IGN)
sys.exit(0 if desktop.spawn(["linux", sys.argv[1], sys.argv[2], "a-1"]) else 1)
"""


@on_proc
def test_a_helper_spawned_by_an_ignoring_runner_ends_on_sigterm(
    orch: Orchestrator, fake_notify_send: FakeNotifySend, monkeypatch
):
    monkeypatch.setenv("FAKE_SLEEP", "600")  # notify-send waits, as for a click
    text = f"signals-{os.getpid()}-{time.monotonic_ns()} finished"
    subprocess.run(
        [sys.executable, "-c", IGNORING_RUNNER, str(fake_notify_send.path), text],
        check=True,
        timeout=60,
    )
    is_helper = lambda pid: b"desktop.helper" in Path(f"/proc/{pid}/cmdline").read_bytes()  # noqa: E731
    (helper,) = until(lambda: [p for p in helper_pids(text) if is_helper(p)], what="the helper")
    (sender,) = until(lambda: children(helper), what="its notify-send")
    until(lambda: "notify-send-sleep" in Path(f"/proc/{sender}/cmdline").read_text())
    # The fake's sleeper is Python, which ignores SIGPIPE by itself; nothing is inherited.
    assert ignored(helper) == set() and ignored(sender) - {"SIGPIPE"} == set()
    os.kill(helper, signal.SIGTERM)
    until(lambda: not alive(helper), timeout=10, what="the helper's end on SIGTERM")
    os.kill(sender, signal.SIGTERM)
    until(lambda: not alive(sender), timeout=10, what="notify-send's end on SIGTERM")


@on_proc
def test_children_of_an_ignoring_process_start_with_default_signals(monkeypatch):
    """What plain() (osascript) and the helper's own children start through, while this
    process ignores the stop signals as a runner ending its turn does."""
    if threading.current_thread() is not threading.main_thread():
        pytest.skip("signal handlers are set in the main thread only")
    probe = ["grep", "SigIgn", "/proc/self/status"]
    assert desktop.with_default_signals(probe) == probe  # nothing ignored: as it is
    names = ("SIGTERM", "SIGINT", "SIGHUP")
    before = {name: signal.getsignal(getattr(signal, name)) for name in names}
    try:
        for name in names:
            signal.signal(getattr(signal, name), signal.SIG_IGN)
        assert desktop.signals_ignored() == list(names)
        mask = lambda argv: int(  # noqa: E731
            subprocess.run(argv, capture_output=True, text=True, check=True).stdout.split()[-1],
            16,
        )
        assert mask(probe) & (1 << 14)  # the cause: an ignored SIGTERM is inherited
        assert mask(desktop.with_default_signals(probe)) & 0b1000000000011 == 0
        assert mask(desktop.with_default_signals(probe)) & (1 << 14) == 0
    finally:
        for name, handler in before.items():
            signal.signal(getattr(signal, name), handler)


def test_without_the_shims_interpreter_the_notifiers_start_as_they_are(
    notify_send, started, fake_osascript, monkeypatch, tmp_path
):
    """While the stop signals are ignored (a runner ending its turn), osascript and notify-send
    start through EXEC_DEFAULT; when its interpreter cannot be started (the tool environment
    replaced during a long turn), they start as they are, and the notice is still shown."""
    if threading.current_thread() is not threading.main_thread():
        pytest.skip("signal handlers are set in the main thread only")
    monkeypatch.setattr(desktop, "PLATFORM", "darwin")  # plain() shows through osascript
    gone = str(tmp_path / "gone" / "python")
    before = signal.getsignal(signal.SIGTERM)
    try:
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        with monkeypatch.context() as patched:
            patched.setattr(desktop.sys, "executable", gone)
            assert desktop.with_default_signals(["osascript"])[0] == gone  # the shim is used
            shown = desktop.plain(BODY, None)
            assert shown is not None  # not dropped as "osascript could not be started"
            shown.join(30)
            desktop.linux_show(str(notify_send.path), BODY, "a-1")
    finally:
        signal.signal(signal.SIGTERM, before)
    (osascript,) = calls(fake_osascript)
    assert osascript[-1].startswith(BODY)
    assert [call[-1] for call in notify_send.calls()] == [
        f"{BODY} — open it: spartito app --agent a-1"
    ]


@on_proc
def test_a_detached_helper_ends_by_itself_when_notify_send_never_does(
    orch: Orchestrator, fake_notify_send: FakeNotifySend, monkeypatch
):
    """notify-send waiting for ever (no click, never closed): the helper kills it once its
    wait is up, and ends."""
    monkeypatch.setenv("FAKE_ACTIONS", "1")
    monkeypatch.setenv("DISPLAY", ":0")
    forever = f"{100000 + os.getpid() % 1000}.{time.monotonic_ns() % 1000}"
    monkeypatch.setenv("FAKE_SLEEP", forever)
    short = (
        "import sys; from orchestrator import desktop; desktop.LINUX_WAIT_SECONDS = 1.0;"
        " sys.exit(desktop.helper(sys.argv[1:]))"
    )
    began = time.monotonic()
    done = subprocess.run(
        [sys.executable, "-c", short, "linux", str(fake_notify_send.path), BODY, "a-1"],
        timeout=60,
    )
    assert done.returncode == 0 and time.monotonic() - began < 20
    assert "--wait" in fake_notify_send.calls()[-1]
    assert helper_pids(forever) == []  # notify-send (its sleep) was killed with the wait


# -- Linux: bounded helpers -------------------------------------------------------------------


def test_linux_helpers_waiting_for_a_click_are_capped(notify_send, started, monkeypatch):
    monkeypatch.setenv("FAKE_ACTIONS", "1")
    monkeypatch.setattr(desktop, "LINUX_SLOTS", 1)
    assert desktop.LINUX_WAIT_SECONDS <= 300
    with desktop.waiting_slot(True) as got:
        assert got  # the one slot, held as by a helper that waits
        desktop.linux_helper(str(notify_send.path), BODY, "a-1")
        assert "--wait" not in sent(notify_send)
        assert sent(notify_send)[-1] == f"{BODY} — open it: spartito app --agent a-1"
    desktop.linux_helper(str(notify_send.path), BODY, "a-1")
    assert "--wait" in sent(notify_send) and sent(notify_send)[-1] == BODY
    with desktop.waiting_slot(False) as got:
        assert not got


def linux_notices(orch: Orchestrator, spawned: list, count: int) -> list[list[str]]:
    """`count` finished notices recorded on Linux; the helper arguments of each."""
    for n in range(count):
        desktop.show(orch.store, f"a-{n} finished", f"a-{n}")
    assert [args[4] for args in spawned] == [str(n + 1) for n in range(count)]
    return list(spawned)


def traced(orch: Orchestrator, notice: int) -> dict:
    return next(n for n in orch.notice_trace() if n["id"] == notice)


class BurstClock:
    """desktop's clock in the burst tests, so that no window depends on the machine's speed:
    time.time() stands still, and a sleep moves it on by its length at once, after `gate`
    (when set) opens. The sleeps are recorded; everything else is the real time module."""

    def __init__(self) -> None:
        self.now = time.time()
        self.slept: list[float] = []
        self.gate: threading.Event | None = None

    def time(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        if self.gate is not None:
            assert self.gate.wait(30), "the test did not open the gate"
        self.now += seconds

    def __getattr__(self, name: str):
        return getattr(time, name)


@pytest.fixture
def burst_clock(monkeypatch: pytest.MonkeyPatch) -> BurstClock:
    clock = BurstClock()
    monkeypatch.setattr(desktop, "time", clock)
    return clock


def test_a_linux_burst_is_summed_up_in_one_popup(
    orch: Orchestrator, spawned, notify_send, started, burst_clock, monkeypatch
):
    """Held-back notices are traced by their id: `counted` on the path linux-summary, and the
    summing one `shown` as the summary; `spartito notices` says so."""
    monkeypatch.setattr(desktop, "PLATFORM", "linux")
    monkeypatch.setattr(desktop, "BURST_LIMIT", 2)
    monkeypatch.setattr(desktop, "BURST_SECONDS", 60.0)
    helpers = linux_notices(orch, spawned, 5)
    run = lambda args: desktop.linux_helper(*args[1:4], int(args[4]))  # noqa: E731
    for args in helpers[:2]:
        run(args)
    assert [call[-1] for call in notify_send.calls()] == [
        "a-0 finished — open it: spartito app --agent a-0",
        "a-1 finished — open it: spartito app --agent a-1",
    ]
    burst_clock.gate = threading.Event()  # the summing helper waits until the others ran
    summing = threading.Thread(target=run, args=(helpers[2],))
    summing.start()
    state = desktop.config.runtime_dir() / desktop.BURST_STATE
    until(lambda: json.loads(state.read_text())["summary"], what="the summing helper")
    for args in helpers[3:]:  # held: counted, nothing shown now
        run(args)
    assert len(notify_send.calls()) == 2
    burst_clock.gate.set()
    summing.join(30)
    assert not summing.is_alive() and burst_clock.slept == [60.0]  # the window, then shown
    assert len(notify_send.calls()) == 3
    assert sent(notify_send)[-1] == "3 more notices — open it: spartito app"
    lines = {n: traced(orch, n)["summary"] for n in range(1, 6)}
    assert [lines[n]["path"] for n in range(1, 6)] == ["linux"] * 2 + ["linux-summary"] * 3
    assert lines[3]["reason"] == "held back, over 2 popups in 60 s: shows the summary in 60 s"
    assert lines[4]["reason"] == "held back, over 2 popups in 60 s: in the summary popup"
    steps = [s["step"] for s in traced(orch, 3)["steps"]]
    assert steps[-2:] == ["counted", "shown"]
    assert '3 held back, shown as "3 more notices"' in traced(orch, 3)["steps"][-1]["detail"]
    log = desktop.notice_log().read_text()
    assert log.count("counted linux-summary: held back, over 2 popups in 60 s") == 3
    assert "#4 " in log and "#5 " in log
    out = CliRunner().invoke(cli.app, ["notices"]).output
    assert out.count("linux-summary") == 3


def test_a_single_notice_held_by_a_burst_is_shown_as_itself_and_traced_as_late(
    orch: Orchestrator, spawned, notify_send, started, burst_clock, monkeypatch
):
    monkeypatch.setattr(desktop, "PLATFORM", "linux")
    monkeypatch.setattr(desktop, "BURST_LIMIT", 1)
    monkeypatch.setattr(desktop, "BURST_SECONDS", 60.0)
    first, second = linux_notices(orch, spawned, 2)
    desktop.linux_helper(*first[1:4], int(first[4]))
    desktop.linux_helper(*second[1:4], int(second[4]))
    assert burst_clock.slept == [60.0]  # held until the window had passed, then shown
    assert [call[-1] for call in notify_send.calls()] == [
        "a-0 finished — open it: spartito app --agent a-0",
        "a-1 finished — open it: spartito app --agent a-1",
    ]
    late = traced(orch, 2)
    assert [s["step"] for s in late["steps"]][-2:] == ["counted", "shown"]
    assert late["summary"]["path"] == "linux"
    assert (
        late["summary"]["reason"] == "held back, over 1 popups in 60 s: shows the summary in 60 s"
    )
    assert "as itself: the only one held back" in late["steps"][-1]["detail"]
    assert traced(orch, 1)["summary"]["reason"].startswith("notify-send;")  # not held


@pytest.mark.parametrize(
    "content",
    ["not json", '{"shown": "x", "held": "y", "summary": 5}', "[1, 2]", '{"held": -1}'],
)
def test_a_burst_state_that_is_not_one_is_reset(notify_send, started, content):
    path = desktop.config.runtime_dir() / desktop.BURST_STATE
    path.write_text(content)
    desktop.linux_helper(str(notify_send.path), BODY, "a-1")
    assert len(notify_send.calls()) == 1  # shown
    state = json.loads(path.read_text())
    assert desktop.burst_checked(state) is not None and len(state["shown"]) == 1
    assert "linux-burst.json: unusable, reset" in desktop.notice_log().read_text()


@pytest.mark.parametrize("why", ["ended", "is overdue"])
def test_a_count_whose_summing_helper_is_gone_is_discarded(notify_send, started, why):
    """A summing helper killed while it waited leaves its count behind: the next notice
    discards it (logged) instead of adding it to a later burst."""
    if why == "ended":
        dead = subprocess.Popen([sys.executable, "-c", ""])
        dead.wait()
        owner = {"pid": dead.pid, "deadline": time.time() + 60, "token": "t"}
    else:
        owner = {"pid": os.getpid(), "deadline": time.time() - 1, "token": "t"}
    path = desktop.config.runtime_dir() / desktop.BURST_STATE
    path.write_text(json.dumps({"shown": [], "held": 3, "summary": owner}))
    desktop.linux_helper(str(notify_send.path), BODY, "a-1")
    assert len(notify_send.calls()) == 1
    state = json.loads(path.read_text())
    assert state["held"] == 0 and state["summary"] is None
    log = desktop.notice_log().read_text()
    assert (
        f"drop linux-summary: 3 held back; their summing helper (pid {owner['pid']}) {why}" in log
    )
    assert desktop.burst_summarised("t") == 1  # an owner whose count is gone shows itself


def test_the_app_open_target_rule_is_the_deep_link_rule():
    for target in ("decision:5", "a-1", ""):
        assert app_open.check_target(desktop.target_of(*parse(target))) == target


def parse(target: str) -> tuple[str | None, int | None]:
    if target.startswith("decision:"):
        return "a-1", int(target.removeprefix("decision:"))
    return (target or None), None


@pytest.mark.parametrize("marker", ["dead", "junk", "zero", "dead (older code)"])
def test_a_window_that_is_gone_counts_as_none_even_with_a_fresh_marker(
    orch: Orchestrator, fake_osascript, macos, spawned, marker
):
    """A force-quit window leaves its markers behind: its pid tells."""
    dead = subprocess.Popen([sys.executable, "-c", ""])
    dead.wait()
    content = {"dead": str(dead.pid), "junk": "not a pid", "zero": "0"}.get(marker, "")
    if marker == "dead":
        desktop.app_state_path().write_text(json.dumps({"pid": dead.pid, "state": "allowed"}))
    desktop.app_marker().write_text(content or str(dead.pid))
    assert not desktop.app_fresh()
    desktop.show(orch.store, BODY, "a-1")
    until(lambda: calls(fake_osascript), what="the notification")
    assert orch.notices() == []


def test_the_window_marker_names_a_live_process():
    desktop.app_present("denied")
    assert desktop.app_marker().read_text() == f"{os.getpid()}\n" and desktop.app_fresh()
    window = desktop.window_presence()
    assert window is not None and window.state == "denied" and window.taker == "banner"
    desktop.app_gone()
    assert desktop.window_presence() is None


def test_a_window_of_older_code_counts_while_its_marker_is_fresh():
    desktop.app_marker().write_text(f"{os.getpid()}\n")  # older code: no state file
    window = desktop.window_presence()
    assert window is not None and window.alive and window.taker == "window"
    then = time.time() - desktop.PAGE_FRESH_SECONDS - 5
    os.utime(desktop.app_marker(), (then, then))
    assert not desktop.window_presence().alive
