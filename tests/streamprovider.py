"""Plugin providers (ORCH_PROVIDERS) with a per-turn stream (`stream()`), for
test_stream_hook.py. Loaded by the runners a test starts, so it imports only what a plugin
would."""

from __future__ import annotations

import sys

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


class HeldBack:
    """A turn stream that answers only at the end: every line is text, the result is the
    turn's lines joined, and a non-zero exit code is an error."""

    def __init__(self) -> None:
        self.lines: list[str] = []

    def feed(self, line: str) -> list[Event]:
        self.lines.append(line.strip())
        return [Event("text", line.strip())]

    def finish(self, exit_code: int | None) -> list[Event]:
        if exit_code == 0:
            return [Event("result", " ".join(self.lines), session_id="held-back-session")]
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
