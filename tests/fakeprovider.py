"""A provider that runs a tiny script instead of a real coding agent.

The prompt drives it: `FAIL` ends with an error result, `SLEEP <s>` delays, `TOUCH <name>`
creates a file in the working directory, `WHOAMI` answers with `<model>/<effort>`. The answer echoes the prompt and whether the turn
resumed an existing session.

`TOKENS <n>` makes the result report usage for the turn (n input tokens, n // 10 output,
2n cache reads, n // 2 cache writes, n / 100000 USD). With `CUMULATIVE` it reports the
running total of the session instead, like `codex exec resume` does.
"""

from __future__ import annotations

import json
import sys

from orchestrator.models import Agent, Event, Usage
from orchestrator.providers.base import Invocation, RunContext

SCRIPT = r"""
import json, pathlib, sys, time
resumed = sys.argv[1]
prompt = sys.stdin.read()
print(json.dumps({"session": "fake-session", "model": "fake-model-1"}), flush=True)
words = prompt.split()
usage = None
for flag, value in zip(words, words[1:]):
    if flag == "SLEEP":
        time.sleep(float(value))
    if flag == "TOUCH":
        pathlib.Path(value).write_text("x")
    if flag == "TOKENS":
        n = int(value)
        usage = {"input_tokens": n, "output_tokens": n // 10, "cache_read_tokens": 2 * n,
                 "cache_creation_tokens": n // 2, "cost_usd": n / 100000}
cumulative = usage is not None and "CUMULATIVE" in words
if cumulative:
    totals = pathlib.Path(".fake-session-usage.json")
    before = json.loads(totals.read_text()) if totals.exists() else {}
    usage = {key: before.get(key, 0) + value for key, value in usage.items()}
    totals.write_text(json.dumps(usage))
print(json.dumps({"text": "working"}), flush=True)
extra = {"usage": usage, "cumulative": cumulative} if usage else {}
if "WHOAMI" in words:
    print(json.dumps({"result": sys.argv[2], **extra}), flush=True)
elif "FAIL" in words:
    print(json.dumps({"result": "boom", "error": True, **extra}), flush=True)
else:
    print(json.dumps({"result": f"echo[{resumed}]: {prompt}", **extra}), flush=True)
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
            return [Event("session", session_id=data["session"], model=data.get("model"))]
        if "text" in data:
            return [Event("text", data["text"])]
        usage = data.get("usage")
        return [
            Event(
                "result",
                data["result"],
                is_error=data.get("error", False),
                usage=usage and Usage(**usage, cumulative=data["cumulative"]),
            )
        ]

    def attach_command(self, agent: Agent) -> list[str]:
        return ["true"]
