"""Who shows a desktop notification (desktop.show): a page, the Spartito window process, the
launcher's app started to post, the Linux helper, or osascript, and the fallbacks between them.

Nothing real is shown or started: osascript is the fake of conftest.py, `open` and
`notify-send` are fakes written here, and the helper runs in this process unless a test runs
it as the detached process on purpose (with fakes only).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest
from test_decisions import calls
from test_notifications import until

from orchestrator import app_open, desktop
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
    assert claimed.is_set() and calls(fake_osascript) == []
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

FAKE_NOTIFY_SEND = """#!/bin/sh
here=$(dirname "$0")
if [ "$1" = "--help" ]; then
    echo "Usage: notify-send [OPTION...] <SUMMARY> [BODY]"
    [ -n "$FAKE_ACTIONS" ] && echo "  -A, --action=[NAME=]Text...  Specifies the actions to display."
    [ -n "$FAKE_ACTIONS" ] && echo "  -w, --wait                    Wait for the notification to be closed."
    exit 0
fi
printf '%s\\n' "$@" > "$here/notify.args"
echo "$DBUS_SESSION_BUS_ADDRESS" > "$here/notify.bus"
[ -n "$FAKE_SLEEP" ] && sleep "$FAKE_SLEEP"
[ -n "$FAKE_INVOKED" ] && echo "$FAKE_INVOKED"
exit 0
"""


@pytest.fixture
def notify_send(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "nbin" / "notify-send"
    path.parent.mkdir()
    path.write_text(FAKE_NOTIFY_SEND)
    path.chmod(0o755)
    monkeypatch.setattr(desktop, "NOTIFY_SEND", str(path))
    monkeypatch.setenv("DISPLAY", ":0")
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    return path


@pytest.fixture
def started(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    noted: list[list[str]] = []
    monkeypatch.setattr(desktop, "start_app", noted.append)
    return noted


def sent(notify_send: Path) -> list[str]:
    return (notify_send.parent / "notify.args").read_text().splitlines()


def test_linux_with_notify_send_hands_the_claimed_notice_to_the_helper(
    orch: Orchestrator, fake_osascript, spawned, notify_send, monkeypatch
):
    monkeypatch.setattr(desktop, "PLATFORM", "linux")
    desktop.show(orch.store, "Decision needed", "a-1", 9)
    assert spawned == [["linux", str(notify_send), "Decision needed", "decision:9"]]
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
    desktop.linux_helper(str(notify_send), "Decision needed", "decision:5")
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
    desktop.linux_helper(str(notify_send), BODY, "a-1")
    assert started[-1] == ["--agent", "a-1"]


@pytest.mark.parametrize("why", ["dismissed", "no display"])
def test_linux_without_a_click_nothing_opens(notify_send, started, monkeypatch, why):
    monkeypatch.setenv("FAKE_ACTIONS", "1")
    if why == "no display":
        monkeypatch.delenv("DISPLAY")
        monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
        monkeypatch.setenv("FAKE_INVOKED", "default")
    desktop.linux_helper(str(notify_send), BODY, "a-1")
    assert started == []
    assert ("--wait" in sent(notify_send)) == (why == "dismissed")


def test_linux_without_actions_the_text_names_the_command(notify_send, started, monkeypatch):
    monkeypatch.setenv("FAKE_INVOKED", "default")  # not asked for: ignored
    desktop.linux_helper(str(notify_send), "Decision needed", "decision:5")
    args = sent(notify_send)
    assert "--wait" not in args and not any(a.startswith("--action") for a in args)
    assert args[-1] == "Decision needed — open it: spartito app --decision 5"
    desktop.linux_helper(str(notify_send), BODY, "a-1")
    assert sent(notify_send)[-1] == f"{BODY} — open it: spartito app --agent a-1"
    desktop.linux_helper(str(notify_send), BODY, "")
    assert sent(notify_send)[-1] == f"{BODY} — open it: spartito app"
    assert started == []


def test_linux_targets_are_checked_before_use(notify_send, started, monkeypatch):
    monkeypatch.setenv("FAKE_ACTIONS", "1")
    monkeypatch.setenv("FAKE_INVOKED", "default")
    desktop.linux_helper(str(notify_send), BODY, "--port=1; rm -rf x")
    assert started == [[]]  # the plain window
    assert desktop.open_command("decision:12") == ["--decision", "12"]
    assert desktop.open_command("decision:x") == []


def test_linux_helper_has_a_time_limit(notify_send, started, monkeypatch):
    monkeypatch.setenv("FAKE_ACTIONS", "1")
    monkeypatch.setenv("FAKE_SLEEP", "10")
    monkeypatch.setenv("FAKE_INVOKED", "default")
    monkeypatch.setattr(desktop, "LINUX_WAIT_SECONDS", 0.5)
    begin = time.monotonic()
    desktop.linux_helper(str(notify_send), BODY, "a-1")
    assert time.monotonic() - begin < 5 and started == []


def test_linux_session_bus_fallback(notify_send, started, monkeypatch, tmp_path):
    monkeypatch.delenv("DBUS_SESSION_BUS_ADDRESS", raising=False)
    monkeypatch.setenv("XDG_RUNTIME_DIR", "/run/user/1000")
    desktop.linux_helper(str(notify_send), BODY, "a-1")
    assert (notify_send.parent / "notify.bus").read_text().strip() == "unix:path=/run/user/1000/bus"
    monkeypatch.setenv("DBUS_SESSION_BUS_ADDRESS", "unix:path=/elsewhere")
    desktop.linux_helper(str(notify_send), BODY, "a-1")
    assert (notify_send.parent / "notify.bus").read_text().strip() == "unix:path=/elsewhere"


def test_linux_icon_prefers_the_installed_one(notify_send, tmp_path):
    assert desktop.linux_icon() == str(desktop.LINUX_ICON) and desktop.LINUX_ICON.is_file()
    icon = tmp_path / "data" / "icons" / "hicolor" / "192x192" / "apps" / "spartito.png"
    icon.parent.mkdir(parents=True)
    icon.write_bytes(b"png")
    assert desktop.linux_icon() == str(icon)


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
