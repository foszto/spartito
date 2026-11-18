"""A plugin provider (ORCH_PROVIDERS) with only the required members of the Provider
protocol, and no use of providers/kit.py. Loaded by the runners a test starts, so it imports
only what a plugin would."""

from __future__ import annotations

import json
import sys

from orchestrator.models import Agent, Event
from orchestrator.providers import Invocation, RunContext

CLI = "import json, sys; sys.stdin.read(); print(json.dumps({'answer': 'done'}))"


class MinimalProvider:
    name = "minimal"
    efforts = ("low",)

    def models(self) -> list[str]:
        return ["minimal-1"]

    def invocation(self, ctx: RunContext) -> Invocation:
        return Invocation(argv=[sys.executable, "-c", CLI], stdin=ctx.prompt)

    def parse(self, line: str) -> list[Event]:
        return [Event("result", json.loads(line)["answer"])]

    def attach_command(self, agent: Agent) -> list[str]:
        return ["true"]
