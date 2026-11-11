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


# -- the order -------------------------------------------------------------------------------


def test_a_fresh_page_comes_first(orch: Orchestrator, fake_osascript, macos, spawned, tmp_path):
    desktop.record_grant("local.x.app", fake_bundle(tmp_path))
    desktop.page_present()
    desktop.app_present()
    desktop.show(orch.store, BODY, "a-1")
    time.sleep(0.3)
    assert spawned == [] and calls(fake_osascript) == []
    assert len(orch.notices()) == 1  # left to the page


def test_the_window_process_comes_next(orch: Orchestrator, fake_osascript, macos, spawned):
    desktop.app_present()
    desktop.show(orch.store, BODY, "a-1")
    time.sleep(0.3)
    assert spawned == [] and calls(fake_osascript) == []
    assert len(orch.notices()) == 1  # left to the window process


def test_a_stale_window_marker_counts_as_none(orch: Orchestrator, fake_osascript, macos, spawned):
    desktop.app_present()
    then = time.time() - desktop.PAGE_FRESH_SECONDS - 5
    os.utime(desktop.app_marker(), (then, then))
    desktop.show(orch.store, BODY, "a-1")
    until(lambda: calls(fake_osascript), what="the notification")
    assert spawned == [] and orch.notices() == []


def test_then_the_allowed_launcher_is_started_to_post(
    orch: Orchestrator, fake_osascript, macos, spawned, tmp_path
):
    bundle = fake_bundle(tmp_path)
    desktop.record_grant("local.x.app", bundle)
    desktop.show(orch.store, "Decision needed: which?", "a-1", 7)
    (notice,) = orch.notices()  # unclaimed: the launcher's app claims it
    assert spawned == [
        ["bundle", desktop.OPEN, str(bundle), str(notice["id"]), "Decision needed: which?", "7"]
    ]
    time.sleep(0.3)
    assert calls(fake_osascript) == []


@pytest.mark.parametrize("why", ["no grant", "no bundle", "stale copy", "other platform"])
def test_without_an_allowed_launcher_osascript_shows_it(
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
        return
    until(lambda: calls(fake_osascript), what="the notification")
    assert spawned == [] and texts(fake_osascript) == [f"{BODY} — open Spartito to see it"]
    assert orch.notices() == []


def test_a_launcher_that_cannot_be_started_falls_back_in_the_same_call(
    orch: Orchestrator, fake_osascript, macos, spawned, tmp_path
):
    desktop.record_grant("local.x.app", fake_bundle(tmp_path))
    desktop.spawn.fail = True  # type: ignore[attr-defined]
    desktop.show(orch.store, BODY, "a-1")
    assert len(spawned) == 1
    until(lambda: calls(fake_osascript), what="the notification")
    assert texts(fake_osascript) == [f"{BODY} — open Spartito to see it"]
    assert orch.notices() == []


def test_without_an_agent_nothing_is_recorded_and_osascript_shows_it(
    orch: Orchestrator, fake_osascript, macos, spawned, tmp_path
):
    desktop.record_grant("local.x.app", fake_bundle(tmp_path))
    desktop.app_present()
    desktop.show(orch.store, "hello")
    until(lambda: calls(fake_osascript), what="the notification")
    assert spawned == [] and texts(fake_osascript) == ["hello — open Spartito to see it"]


def test_show_does_not_wait_for_the_helper(orch: Orchestrator, macos, tmp_path, monkeypatch):
    """The real spawn: a detached process, started and left alone."""
    started: list[float] = []
    real_popen = subprocess.Popen

    def slow(argv, **options):
        started.append(time.monotonic())
        assert options["start_new_session"] and options["stdin"] == subprocess.DEVNULL
        return real_popen([sys.executable, "-c", "import time; time.sleep(3)"], **options)

    monkeypatch.setattr(desktop.subprocess, "Popen", slow)
    desktop.record_grant("local.x.app", fake_bundle(tmp_path))
    begin = time.monotonic()
    desktop.show(orch.store, BODY, "a-1")
    assert started and time.monotonic() - begin < 1.5


# -- the osascript text ----------------------------------------------------------------------


def test_the_osascript_text_says_where_to_look(orch: Orchestrator, fake_osascript, macos):
    desktop.show(orch.store, "Decision needed: which?", "a-1", 3)
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
    stale = "Python changed since `orch app install` (it copied 3.14.6)"
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


# -- 3. the bundle helper --------------------------------------------------------------------

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


def test_the_helper_starts_the_bundle_hidden_and_leaves_the_notice_to_it(
    orch: Orchestrator, fake_osascript, macos, fake_open, tmp_path, monkeypatch
):
    bundle = fake_bundle(tmp_path)
    notice = orch.store.add_notice("finished", "a-1", BODY)
    threading.Timer(0.3, orch.claim_notice, (notice,)).start()  # the app's posting process
    desktop.bundle_helper(str(fake_open), bundle, notice, BODY, "")
    args = (fake_open.parent / "open.args").read_text().splitlines()
    assert args == ["-n", "-g", "-j", "-a", str(bundle), "--args", "--notify"]
    time.sleep(0.3)
    assert calls(fake_osascript) == []


@pytest.mark.parametrize("why", ["open fails", "nobody claims"])
def test_the_helper_falls_back_to_osascript(
    orch: Orchestrator, fake_osascript, macos, fake_open, tmp_path, monkeypatch, why
):
    monkeypatch.setattr(desktop, "CLAIM_WAIT_SECONDS", 0.5)
    if why == "open fails":
        monkeypatch.setenv("FAKE_OPEN_EXIT", "1")
    notice = orch.store.add_notice("decision", "a-1", "Decision needed", 4)
    desktop.bundle_helper(str(fake_open), fake_bundle(tmp_path), notice, "Decision needed", "4")
    until(lambda: calls(fake_osascript), what="the notification")
    assert texts(fake_osascript) == ["Decision needed — open Spartito to answer"]
    assert orch.notices() == []


def test_the_detached_helper_runs_as_its_own_process(
    orch: Orchestrator, fake_osascript, fake_open, tmp_path, monkeypatch
):
    """The command line `spawn` starts, run for real (with the fake open, which fails, and
    the fake osascript of the PATH)."""
    monkeypatch.setenv("FAKE_OPEN_EXIT", "1")
    notice = orch.store.add_notice("finished", "a-1", BODY)
    bundle = fake_bundle(tmp_path)
    args = ["bundle", str(fake_open), str(bundle), str(notice), BODY, ""]
    done = subprocess.run([sys.executable, "-c", desktop.HELPER, *args], timeout=60)
    assert done.returncode == 0
    if sys.platform == "darwin":  # osascript is looked for on macOS only
        until(lambda: calls(fake_osascript), what="the notification")  # left running
        assert texts(fake_osascript) == [f"{BODY} — open Spartito to see it"]
        assert orch.notices() == []
    else:
        assert len(orch.notices()) == 1
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
    assert args[-1] == "Decision needed — open it: orch app --decision 5"
    desktop.linux_helper(str(notify_send), BODY, "a-1")
    assert sent(notify_send)[-1] == f"{BODY} — open it: orch app --agent a-1"
    desktop.linux_helper(str(notify_send), BODY, "")
    assert sent(notify_send)[-1] == f"{BODY} — open it: orch app"
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
