from __future__ import annotations

import contextlib
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest
from fakeprovider import FakeProvider

from orchestrator import providers
from orchestrator.core import views
from orchestrator.providers import claude
from orchestrator.service import Orchestrator


@pytest.fixture(autouse=True)
def isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """No test may see the real ~/.orchestrator, even one that never asks for `orch`."""
    monkeypatch.setenv("ORCH_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("ORCH_AGENT_ID", raising=False)
    # Neither the turn's home of an agent running the tests nor the switch: the tests of the
    # desktop paths need them unset (the fake osascript and notify-send keep them off the desktop).
    monkeypatch.delenv("ORCH_TURN_HOME", raising=False)
    monkeypatch.delenv("ORCH_DESKTOP_NOTIFICATIONS", raising=False)
    monkeypatch.setenv("ORCH_NOTIFY_DELAY", "0")  # notifications start without the delay


@pytest.fixture(autouse=True)
def plain_help(monkeypatch: pytest.MonkeyPatch) -> None:
    """Help pages as plain text wherever the tests run: typer's rich help forces a terminal
    (escape codes) when GITHUB_ACTIONS, FORCE_COLOR or PY_COLORS is set, read once when typer
    is imported. Its own switch off, here for this process (whenever typer was imported) and
    in the environment for the children."""
    monkeypatch.setattr("typer.rich_utils.FORCE_TERMINAL", False)
    monkeypatch.setenv("_TYPER_FORCE_DISABLE_TERMINAL", "1")


# Stands in for caffeinate and systemd-inhibit, so no test touches the power settings.
# Lasts until the runner pid it was given (its last argument) has ended, as the real ones
# do. Notes in $FAKE_INHIBIT_LOG "<pid> start <name> <ORCH_TURN or unmarked> <args>", then
# "<pid> end" when the runner was gone, or "<pid> TERM stored|early" when it was ended:
# whether a run's result file existed by then. FAKE_INHIBIT_EXIT=<code>: exits at once
# (no lock to be had); FAKE_INHIBIT_STUBBORN=1: ignores SIGTERM. Its traps are set before it
# notes its start: a SIGTERM before that (a release before the script ran) ends it unlogged,
# which the prompt word AWAKE of the fake provider rules out.
FAKE_INHIBITOR = r"""#!/bin/sh
log() { [ -n "$FAKE_INHIBIT_LOG" ] && echo "$$ $1" >> "$FAKE_INHIBIT_LOG"; }
stored() { ls "$ORCH_HOME"/logs/run-*/result.md >/dev/null 2>&1 && echo stored || echo early; }
trap 'log "TERM $(stored)"; exit 0' TERM
[ -n "$FAKE_INHIBIT_STUBBORN" ] && trap '' TERM
log "start $(basename "$0") ${ORCH_TURN:-unmarked} $*"
[ -n "$FAKE_INHIBIT_EXIT" ] && exit "$FAKE_INHIBIT_EXIT"
eval "runner=\${$#}"
while kill -0 "$runner" 2>/dev/null; do sleep 0.1; done
log end
"""


@pytest.fixture(autouse=True)
def fake_inhibitors(
    tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> Path:
    """Fake sleep inhibitors first on PATH; returns the log they write. Outside tmp_path,
    which some tests list."""
    bin_dir = tmp_path_factory.mktemp("fake-bin")
    for name in ("caffeinate", "systemd-inhibit"):
        path = bin_dir / name
        path.write_text(FAKE_INHIBITOR)
        path.chmod(0o755)
    log = bin_dir / "inhibit.log"
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")
    monkeypatch.setenv("FAKE_INHIBIT_LOG", str(log))
    return log


# Stands in for osascript, so no test shows a real desktop notification. Appends one line per
# call to $FAKE_OSASCRIPT_LOG: its arguments, each followed by \x1f.
FAKE_OSASCRIPT = r"""#!/bin/sh
[ -n "$FAKE_OSASCRIPT_LOG" ] || exit 0
{ for arg in "$@"; do printf '%s\037' "$arg"; done; printf '\n'; } >> "$FAKE_OSASCRIPT_LOG"
"""


@pytest.fixture(autouse=True)
def fake_osascript(
    tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> Path:
    """A fake osascript first on PATH; returns the log it writes."""
    bin_dir = tmp_path_factory.mktemp("fake-osascript")
    path = bin_dir / "osascript"
    path.write_text(FAKE_OSASCRIPT)
    path.chmod(0o755)
    log = bin_dir / "osascript.log"
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")
    monkeypatch.setenv("FAKE_OSASCRIPT_LOG", str(log))
    return log


# Stands in for notify-send (Linux), so no test or child of one (a runner, an MCP server, the
# detached helper) shows a real desktop notification or leaves a helper waiting for a click.
# Appends one line per call to $FAKE_NOTIFY_SEND_LOG: its path, its session bus and its
# arguments, each followed by \x1f; `--help` is not noted. It exits at once unless told:
# FAKE_ACTIONS=1: its help lists --action and --wait; FAKE_INVOKED=<name>: prints it, as an
# invoked action; FAKE_SLEEP=<s>: becomes (exec) `notify-send-sleep <s>` instead, a sleep that
# ends when its pid is killed, as the real notify-send would, and prints nothing.
FAKE_NOTIFY_SEND = r"""#!/bin/sh
if [ "$1" = "--help" ]; then
    echo "Usage: notify-send [OPTION...] <SUMMARY> [BODY]"
    [ -n "$FAKE_ACTIONS" ] && echo "  -A, --action=[NAME=]Text...  Specifies the actions to display."
    [ -n "$FAKE_ACTIONS" ] && echo "  -w, --wait                    Wait for the notification to be closed."
    exit 0
fi
if [ -n "$FAKE_NOTIFY_SEND_LOG" ]; then
    { printf '%s\037%s\037' "$0" "$DBUS_SESSION_BUS_ADDRESS"
      for arg in "$@"; do printf '%s\037' "$arg"; done; printf '\n'; } >> "$FAKE_NOTIFY_SEND_LOG"
fi
[ -n "$FAKE_SLEEP" ] && exec "$(dirname "$0")/notify-send-sleep" "$FAKE_SLEEP"
[ -n "$FAKE_INVOKED" ] && echo "$FAKE_INVOKED"
exit 0
"""


class FakeNotifySend:
    """The fake notify-send first on PATH, and the calls it noted."""

    def __init__(self, path: Path, log: Path) -> None:
        self.path = path
        self.log = log

    def lines(self) -> list[list[str]]:
        text = self.log.read_text() if self.log.exists() else ""
        return [line.split("\x1f")[:-1] for line in text.splitlines()]

    def calls(self) -> list[list[str]]:
        """The arguments of each call."""
        return [line[2:] for line in self.lines()]

    def buses(self) -> list[str]:
        return [line[1] for line in self.lines()]

    def programs(self) -> list[str]:
        """The path each call was made through (always this fake's)."""
        return [line[0] for line in self.lines()]


# A session bus that is not there: even a notify-send that is not the fake reaches no desktop.
NO_BUS = "no-session-bus"


@pytest.fixture(autouse=True)
def fake_notify_send(
    tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> FakeNotifySend:
    """A fake notify-send first on PATH, inherited by every child process; and, should a real
    one run anyway, no session bus or display to reach: DBUS_SESSION_BUS_ADDRESS names a
    socket that does not exist (desktop.linux_env keeps a set value), XDG_RUNTIME_DIR (its
    fallback) is a temporary directory, DISPLAY and WAYLAND_DISPLAY are unset (tests that
    need one set it)."""
    bin_dir = tmp_path_factory.mktemp("fake-notify-send")
    path = bin_dir / "notify-send"
    path.write_text(FAKE_NOTIFY_SEND)
    path.chmod(0o755)
    sleeper = bin_dir / "notify-send-sleep"  # not a link to sleep: uutils checks its name
    python = sys.executable if " " not in sys.executable else "/usr/bin/env python3"
    sleeper.write_text(f"#!{python}\nimport sys, time\ntime.sleep(float(sys.argv[1]))\n")
    sleeper.chmod(0o755)
    log = bin_dir / "notify-send.log"
    runtime = bin_dir / "runtime"
    runtime.mkdir(mode=0o700)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")
    monkeypatch.setenv("FAKE_NOTIFY_SEND_LOG", str(log))
    monkeypatch.setenv("DBUS_SESSION_BUS_ADDRESS", f"unix:path={runtime / NO_BUS}")
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(runtime))
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    return FakeNotifySend(path, log)


# Processes of the desktop's notifications that a test session may leave running for a while
# (a detached helper waiting for a fake's answer, a burst summary): this long at the end.
LEFTOVER_GRACE_SECONDS = 90.0
NOTIFIER_WORDS = (b"desktop.helper", b"notify-send")


def process_start_ticks(pid: int | str) -> int | None:
    """When the process started, in clock ticks since boot (Linux), None: gone."""
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
        return int(stat.rsplit(")", 1)[1].split()[19])
    except OSError, ValueError, IndexError:
        return None


def session_notifiers(base: Path, since: int) -> dict[int, str]:
    """The live processes of desktop notifications (a desktop.helper, a notify-send) this test
    session started: started after it (`since`, in ticks) and with its temporary directory
    `base` in their command line or environment (ORCH_HOME, PATH, HOME: every child of a test
    has one of these). Linux only (/proc); pid: command line."""
    mark = (str(base) + os.sep).encode()
    found: dict[int, str] = {}
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit() or int(entry.name) == os.getpid():
            continue
        try:
            argv = (entry / "cmdline").read_bytes()
            if not any(word in argv for word in NOTIFIER_WORDS):
                continue
            environ = (entry / "environ").read_bytes()
        except OSError:
            continue
        started = process_start_ticks(entry.name)
        if started is None or started < since or (mark not in argv and mark not in environ):
            continue
        found[int(entry.name)] = argv.replace(b"\0", b" ").decode(errors="replace").strip()
    return found


@pytest.fixture(scope="session", autouse=True)
def no_leftover_notifiers(tmp_path_factory: pytest.TempPathFactory):
    """At the end of the session (each xdist worker its own): no notification helper or
    notify-send that it started still runs. Such a process would be a real notification
    waiting on the desktop, or a fake that was never told to end. The ones found after
    LEFTOVER_GRACE_SECONDS are killed and the session errs."""
    if not Path("/proc/self/stat").exists():
        yield
        return
    base = tmp_path_factory.getbasetemp().resolve()
    since = process_start_ticks(os.getpid()) or 0
    yield
    deadline = time.monotonic() + LEFTOVER_GRACE_SECONDS
    while (left := session_notifiers(base, since)) and time.monotonic() < deadline:
        time.sleep(0.5)
    if not left:
        return
    for pid in left:
        with contextlib.suppress(OSError):  # with its children, when it leads its group
            if os.getpgid(pid) == pid:
                os.killpg(pid, signal.SIGKILL)
            else:
                os.kill(pid, signal.SIGKILL)
    listed = "\n".join(f"  {pid}: {argv}" for pid, argv in left.items())
    pytest.fail(f"the tests left notification processes running (now killed):\n{listed}")


# Stands in for the service managers, so no test touches the real launchd or systemd. Appends
# one line per call to $FAKE_SYSTEM_DIR/calls.log: its name and arguments, each followed by
# \x1f. Its subcommand is its first argument that is not an option: it prints
# $FAKE_SYSTEM_DIR/<name>.<subcommand>.out if present and exits with the number in
# <name>.<subcommand>.exit (0 without one).
FAKE_SYSTEM_COMMAND = r"""#!/bin/sh
name=$(basename "$0")
sub=""
for arg in "$@"; do case "$arg" in -*) ;; *) sub="$arg"; break ;; esac; done
{ printf '%s\037' "$name"; for arg in "$@"; do printf '%s\037' "$arg"; done; printf '\n'; } \
    >> "$FAKE_SYSTEM_DIR/calls.log"
[ -f "$FAKE_SYSTEM_DIR/$name.$sub.out" ] && cat "$FAKE_SYSTEM_DIR/$name.$sub.out"
[ -f "$FAKE_SYSTEM_DIR/$name.$sub.exit" ] && exit "$(cat "$FAKE_SYSTEM_DIR/$name.$sub.exit")"
exit 0
"""
SYSTEM_COMMANDS = ("launchctl", "systemctl", "loginctl", "systemd-analyze")


class FakeSystem:
    """The fake service managers' control directory: a machine where nothing is installed
    until a test says otherwise (`launchctl print` and `bootout` fail, nothing is active)."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.set("launchctl", "print", exit=113, out="Could not find service\n")
        self.set("launchctl", "bootout", exit=5, out="Boot-out failed: 5: Input/output error\n")
        self.set("systemctl", "is-active", exit=3, out="inactive\n")

    def set(self, name: str, sub: str, exit: int = 0, out: str = "") -> None:
        (self.path / f"{name}.{sub}.exit").write_text(str(exit))
        (self.path / f"{name}.{sub}.out").write_text(out)

    def calls(self) -> list[list[str]]:
        log = self.path / "calls.log"
        lines = log.read_text().splitlines() if log.exists() else []
        return [line.split("\x1f")[:-1] for line in lines]

    def clear(self) -> None:
        (self.path / "calls.log").unlink(missing_ok=True)


@pytest.fixture(autouse=True)
def fake_system(
    tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> FakeSystem:
    """Fake launchctl, systemctl, loginctl and systemd-analyze first on PATH, and HOME and
    XDG_CONFIG_HOME in temporary directories: no test reaches the real service managers or
    ~/Library/LaunchAgents, ~/.config/systemd. Git keeps the real global configuration."""
    bin_dir = tmp_path_factory.mktemp("fake-system")
    for name in SYSTEM_COMMANDS:
        path = bin_dir / name
        path.write_text(FAKE_SYSTEM_COMMAND)
        path.chmod(0o755)
    xdg = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    git_config = [p for p in (Path.home() / ".gitconfig", xdg / "git" / "config") if p.is_file()]
    if "GIT_CONFIG_GLOBAL" not in os.environ and git_config:
        monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(git_config[0]))
    home = tmp_path_factory.mktemp("user-home")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")
    monkeypatch.setenv("FAKE_SYSTEM_DIR", str(bin_dir))
    return FakeSystem(bin_dir)


CLAUDE_INITIALIZE = Path(__file__).parent / "fixtures" / "claude_initialize.jsonl"


@pytest.fixture(autouse=True)
def recorded_claude_models(monkeypatch: pytest.MonkeyPatch) -> None:
    """The Claude model list comes from a recorded answer, not the installed CLI."""
    monkeypatch.setattr(claude, "request_model_list", CLAUDE_INITIALIZE.read_text)
    monkeypatch.setitem(providers.PROVIDERS, "claude", claude.ClaudeProvider())


@pytest.fixture
def orch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Orchestrator:
    """An orchestrator with an isolated home; runners load the fake provider via env."""
    monkeypatch.setenv("ORCH_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("ORCH_PROVIDERS", "fake=fakeprovider:FakeProvider")
    monkeypatch.setenv("PYTHONPATH", str(Path(__file__).parent))
    monkeypatch.setitem(providers.PROVIDERS, "fake", FakeProvider())
    monkeypatch.setattr(views, "WAIT_POLL_SECONDS", 0.1)
    return Orchestrator()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    path = tmp_path / "repo"
    path.mkdir()
    for args in (
        ["init", "-q", "-b", "main"],
        [
            "-c",
            "user.name=t",
            "-c",
            "user.email=t@example.com",
            "commit",
            "-q",
            "--allow-empty",
            "-m",
            "init",
        ],
    ):
        subprocess.run(["git", "-C", str(path), *args], check=True)
    return path


@pytest.fixture
def project(orch: Orchestrator, repo: Path) -> str:
    return orch.add_project(str(repo), "demo", "fake").name


@pytest.fixture(autouse=True)
def no_real_launcher(tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch):
    """`spartito app` and its launcher look in a temporary directory, never ~/Applications or
    the real XDG data home, and never call lsregister."""
    from orchestrator.gui import launcher

    apps = tmp_path_factory.mktemp("launchers")
    monkeypatch.setattr(launcher, "default_dir", lambda kind: apps / kind)
    monkeypatch.setattr(launcher, "unregister", lambda bundle: None)
    # The interpreter copy of a macOS launcher: a small file instead of Python, never signed
    # or run; by default it "does not work", so the launcher uses CFProcessPath. Tests of the
    # copy set their own fakes.
    source = apps / "fake-python"
    source.write_bytes(b"not a real interpreter\n")
    monkeypatch.setattr(launcher, "interpreter_source", lambda: source)
    monkeypatch.setattr(launcher, "codesign", lambda path, identity: None)
    monkeypatch.setattr(launcher, "check_copy", lambda copy, env: "not checked in tests")
    return apps


@pytest.fixture(autouse=True)
def no_real_notifications(tmp_path_factory: pytest.TempPathFactory, monkeypatch):
    """No test reaches a real notification path: `open` (the launcher's app, macOS) and
    `notify-send` (Linux) are names that find nothing unless a test installs a fake, and the
    notification centre cannot be had (notifier.MacCenter raises)."""
    from orchestrator import desktop
    from orchestrator.gui import notifier

    missing = tmp_path_factory.mktemp("no-commands")
    monkeypatch.setattr(desktop, "OPEN", str(missing / "open"))
    monkeypatch.setattr(desktop, "NOTIFY_SEND", "orch-test-no-notify-send")
    monkeypatch.setattr(desktop, "ROLE", None)  # a window test sets it for its process

    def no_center(*args, **kwargs):
        raise RuntimeError("no notification centre in tests")

    monkeypatch.setattr(notifier, "MacCenter", no_center)


@pytest.fixture(autouse=True)
def no_detached_helpers(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    """desktop.spawn notes the detached helper's arguments instead of starting it, so no test
    leaves a process behind that escalates a notice later; a test runs the helper itself
    (desktop.helper) or starts it on purpose. Returns the notes."""
    from orchestrator import desktop

    noted: list[list[str]] = []
    monkeypatch.setattr(desktop, "spawn", lambda args: noted.append(args) or True)
    return noted


@pytest.fixture(autouse=True)
def no_real_window(monkeypatch: pytest.MonkeyPatch) -> None:
    """No test can open a real window or browser (and so no WebKit data in ~/Library): no
    pywebview (`import webview` fails; tests install a fake one), no Chromium-family browser
    found, a default browser that opens nothing. Tests that need one install a fake."""
    import sys

    from orchestrator.gui import window

    monkeypatch.setitem(sys.modules, "webview", None)
    monkeypatch.setattr(window, "mac_app_dirs", lambda: [])
    monkeypatch.setattr(window, "LINUX_BROWSERS", ())
    monkeypatch.setattr(window.webbrowser, "open", lambda url: True)
