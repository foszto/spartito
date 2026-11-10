"""A provider that runs a tiny script instead of a real coding agent.

The prompt drives it: `FAIL` ends with an error result, `SLEEP <s>` delays, `TOUCH <name>`
creates a file in the working directory, `WHOAMI` answers with `<model>/<effort>`. The answer echoes the prompt and whether the turn
resumed an existing session.
"""

from __future__ import annotations

import json
import sys

from orchestrator.models import Agent, Event
from orchestrator.providers.base import Invocation, RunContext

SCRIPT = r"""
import json, pathlib, sys, time
resumed = sys.argv[1]
prompt = sys.stdin.read()
print(json.dumps({"session": "fake-session"}), flush=True)
words = prompt.split()
for flag, value in zip(words, words[1:]):
    if flag == "SLEEP":
        time.sleep(float(value))
    if flag == "TOUCH":
        pathlib.Path(value).write_text("x")
print(json.dumps({"text": "working"}), flush=True)
if "WHOAMI" in words:
    print(json.dumps({"result": sys.argv[2]}), flush=True)
elif "FAIL" in words:
    print(json.dumps({"result": "boom", "error": True}), flush=True)
else:
    print(json.dumps({"result": f"echo[{resumed}]: {prompt}"}), flush=True)
"""


class FakeProvider:
    name = "fake"
    efforts = ("low", "high")

    def models(self) -> list[str]:
        return ["fake-small", "fake-large"]

    def invocation(self, ctx: RunContext) -> Invocation:
        resumed = "resumed" if ctx.agent.session_id else "new"
        config = f"{ctx.model}/{ctx.effort}"
        return Invocation(argv=[sys.executable, "-c", SCRIPT, resumed, config], stdin=ctx.prompt)

    def parse(self, line: str) -> list[Event]:
        data = json.loads(line)
        if "session" in data:
            return [Event("session", session_id=data["session"])]
        if "text" in data:
            return [Event("text", data["text"])]
        return [Event("result", data["result"], is_error=data.get("error", False))]

    def attach_command(self, agent: Agent) -> list[str]:
        return ["true"]
