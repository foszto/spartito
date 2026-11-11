from __future__ import annotations

import os
import subprocess
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
    monkeypatch.setenv("ORCH_NOTIFY_DELAY", "0")  # notifications start without the delay


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
