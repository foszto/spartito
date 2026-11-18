"""Plugin providers (ORCH_PROVIDERS) with a per-turn stream (`stream()`), for
test_stream_hook.py. Loaded by the runners a test starts, so it imports only what a plugin
would."""

from __future__ import annotations

import sys
import time
from pathlib import Path

from orchestrator.models import Agent, Event
from orchestrator.providers import Invocation, RunContext

# Prints each word of the prompt on a line of its own, then exits with the code of an
# `EXIT<n>` word (0 without one).
CLI = (
    "import sys; words = sys.stdin.read().split(); "
    "[print(w, flush=True) for w in words if not w.startswith('EXIT')]; "
    "sys.exit(next((int(w[4:]) for w in words if w.startswith('EXIT')), 0))"
)
NOTE = "effort is not passed on: the test CLI has none"
# A first launch with memory: it prints a line, never creates the canary and waits for
# input, so the runner ends it and launches the turn once more without memory.
MEMORY_CLI = "import sys; print('memory-launch', flush=True); sys.stdin.read()"
# Notes its pid in `.stream-pid` in the working directory, prints a line and lingers.
LINGERING_CLI = (
    "import os, pathlib, time; pathlib.Path('.stream-pid').write_text(str(os.getpid())); "
    "print('hello', flush=True); time.sleep(30)"
)
PID_FILE = ".stream-pid"


class HeldBack:
    """A turn stream that answers only at the end: every line is text, the result is the
    turn's lines joined (after `label: ` when it has a label), and a non-zero exit code is
    an error."""

    def __init__(self, label: str = "") -> None:
        self.label = label
        self.lines: list[str] = []

    def feed(self, line: str) -> list[Event]:
        self.lines.append(line.strip())
        return [Event("text", line.strip())]

    def finish(self, exit_code: int) -> list[Event]:
        if exit_code == 0:
            text = " ".join(self.lines)
            text = f"{self.label}: {text}" if self.label else text
            return [Event("result", text, session_id="held-back-session")]
        return [Event("error", f"exit code {exit_code} after {len(self.lines)} lines")]


class HeldBackProvider:
    name = "held-back"
    efforts = ("low",)

    def models(self) -> list[str]:
        return ["held-back-1"]

    def invocation(self, ctx: RunContext) -> Invocation:
        return Invocation(argv=[sys.executable, "-c", CLI], stdin=ctx.prompt)

    def stream(self) -> HeldBack:
        return HeldBack()

    def parse(self, line: str) -> list[Event]:
        return HeldBack().feed(line)

    def attach_command(self, agent: Agent) -> list[str]:
        return ["true"]


class NotingProvider(HeldBackProvider):
    name = "noting"

    def invocation(self, ctx: RunContext) -> Invocation:
        return Invocation(argv=[sys.executable, "-c", CLI], stdin=ctx.prompt, notes=[NOTE])


class HomeProvider(HeldBackProvider):
    name = "home"
    config_env = ("HELD_BACK_HOME", "CODEX_HOME")


class RelaunchedProvider(HeldBackProvider):
    """With `memory` in the loadout its CLI never proves its hooks run (no canary), so the
    runner relaunches the turn without memory; each stream is labelled with the launch its
    provider last made."""

    name = "relaunched"
    launch = ""

    def invocation(self, ctx: RunContext) -> Invocation:
        if ctx.loadout.memory:
            self.launch = "memory"
            argv = [sys.executable, "-c", MEMORY_CLI]
            return Invocation(argv=argv, stdin=ctx.prompt, canary=ctx.scratch / "canary")
        self.launch = "plain"
        return Invocation(argv=[sys.executable, "-c", CLI], stdin=ctx.prompt)

    def stream(self) -> HeldBack:
        return HeldBack(label=self.launch)


class Lingering:
    """A CLI that lingers after its first line, and a wait until it has noted its pid."""

    name = "lingering"
    efforts = ("low",)
    cwd = "."

    def models(self) -> list[str]:
        return ["lingering-1"]

    def invocation(self, ctx: RunContext) -> Invocation:
        self.cwd = ctx.agent.cwd
        return Invocation(argv=[sys.executable, "-c", LINGERING_CLI], stdin=ctx.prompt)

    def started(self) -> None:
        deadline = time.monotonic() + 10
        while not (Path(self.cwd) / PID_FILE).exists() and time.monotonic() < deadline:
            time.sleep(0.05)

    def attach_command(self, agent: Agent) -> list[str]:
        return ["true"]


class StreamRaisesProvider(Lingering):
    name = "stream-raises"

    def stream(self) -> HeldBack:
        self.started()
        raise RuntimeError("no stream for this turn")

    def parse(self, line: str) -> list[Event]:
        return []


class StreamNotCallableProvider(Lingering):
    name = "stream-not-callable"

    @property
    def stream(self) -> str:
        self.started()
        return "not callable"

    def parse(self, line: str) -> list[Event]:
        return []


class ParseRaisesProvider(Lingering):
    """No stream(): its parse raises on the CLI's first line."""

    name = "parse-raises"

    def parse(self, line: str) -> list[Event]:
        raise RuntimeError("no events for this line")
