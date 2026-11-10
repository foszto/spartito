"""A provider that runs a tiny script instead of a real coding agent.

The prompt drives it: `FAIL` ends with an error result, `SLEEP <s>` delays, `TOUCH <name>`
creates a file in the working directory, `WHOAMI` answers with `<model>/<effort>`, `LOADOUT`
with `memory` or `plain`. With `memory` in the loadout it behaves like Claude with
`--input-format stream-json`: starts a child, creates the canary (FAKE_HOOKS=off: none, it
waits for input; FAKE_HOOKS=slow: only when it is sent SIGTERM; FAKE_HOOKS=exit: fails
with `Not logged in` first), then accepts exactly one
stream-json user message up to EOF. Each launch appends its mode to `.fake-launches`. The answer echoes the prompt and whether the turn
resumed an existing session.

`TOKENS <n>` makes the result report usage for the turn (n input tokens, n // 10 output,
2n cache reads, n // 2 cache writes, n / 100000 USD). With `CUMULATIVE` it reports the
running total of the session instead, like `codex exec resume` does.

`STALE` first reports a result of its own for a background task (like Claude on resume
after its previous process was ended with a background command running). `TWICE` reports
a second result after the answer (like Claude when a background command ends), with the
same usage again.

Processes, for ending a turn: the script appends its pid and those of the children named
below to `.fake-pids` in the working directory, one per line, its own first.
`LINGER <s>` keeps it alive for s seconds after the answer, with a child in a session of
its own (as Claude starts its shells); with `STUBBORN` that child ignores SIGTERM.
`ORPHAN <s>` before the answer starts a shell-like helper (in a process group of its own,
with `NEWSESSION` in a session of its own) that starts a child living s seconds and exits
at once, like `nohup server &`; then the script answers and exits. `DIE` starts a child
that keeps the script's stdout open for an hour and kills the script before it answers.
"""

from __future__ import annotations

import json
import sys

from orchestrator.models import Agent, Event, Usage
from orchestrator.providers.base import NOTIFICATION_RESULT, Invocation, RunContext
from orchestrator.providers.claude import stream_json_prompt

SCRIPT = r"""
import json, os, pathlib, signal, subprocess, sys, time
def note(pid):
    with open(".fake-pids", "a") as pids:
        pids.write(f"{pid}\n")
note(os.getpid())
resumed = sys.argv[1]
with open(".fake-launches", "a") as launches:
    launches.write(sys.argv[3] + "\n")
server = None
if sys.argv[3] == "memory":  # like Claude with --input-format stream-json
    server = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(3600)"])
    note(server.pid)  # a child it starts at once (an MCP server)
    hooks = os.environ.get("FAKE_HOOKS")
    if hooks == "exit":  # fails before its hooks, e.g. an expired login
        server.kill()
        print("Not logged in", file=sys.stderr)
        sys.exit(1)
    if hooks == "slow":  # the canary comes only as it is being ended
        def late(signum, frame):
            pathlib.Path(sys.argv[4]).touch()
            server.kill()
            sys.exit(0)
        signal.signal(signal.SIGTERM, late)
    elif hooks != "off":
        pathlib.Path(sys.argv[4]).touch()  # the SessionStart canary, before any input
    raw = sys.stdin.read()  # hooks off: waits here, like the real CLI, until it is ended
    try:
        (line,) = raw.splitlines()
        data = json.loads(line)
        assert data["type"] == "user" and data["message"]["role"] == "user"
        ((block,),) = [data["message"]["content"]]
        prompt = block["text"]
    except Exception:
        server.kill()
        print(json.dumps({"result": f"not one stream-json message: {raw!r}", "error": True}))
        sys.exit(1)
else:
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
extra = {"usage": usage, "cumulative": cumulative} if usage else {}
if "STALE" in words:
    print(json.dumps({"result": "", "notification": True, **extra}), flush=True)
print(json.dumps({"text": "working"}), flush=True)
if "DIE" in words:
    note(subprocess.Popen([sys.executable, "-c", "import time; time.sleep(3600)"]).pid)
    os.kill(os.getpid(), signal.SIGKILL)
for flag, value in zip(words, words[1:]):
    if flag == "ORPHAN":
        helper = (  # like `nohup server >/dev/null 2>&1 &`
            "import subprocess, sys; sleep = 'import time; time.sleep(" + value + ")'; "
            "child = subprocess.Popen([sys.executable, '-c', sleep], stdin=subprocess.DEVNULL, "
            "stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL); "
            "open('.fake-pids', 'a').write(f'{child.pid}\\n')"
        )
        apart = {"start_new_session": True} if "NEWSESSION" in words else {"process_group": 0}
        subprocess.run([sys.executable, "-c", helper], **apart)
if "WHOAMI" in words:
    print(json.dumps({"result": sys.argv[2], **extra}), flush=True)
elif "LOADOUT" in words:
    print(json.dumps({"result": sys.argv[3], **extra}), flush=True)
elif "FAIL" in words:
    print(json.dumps({"result": "boom", "error": True, **extra}), flush=True)
else:
    print(json.dumps({"result": f"echo[{resumed}]: {prompt}", **extra}), flush=True)
if "TWICE" in words:
    time.sleep(0.5)
    print(json.dumps({"text": "the background command finished"}), flush=True)
    print(json.dumps({"result": "second answer", "notification": True, **extra}), flush=True)
for flag, value in zip(words, words[1:]):
    if flag == "LINGER":
        ignore = "import signal; signal.signal(signal.SIGTERM, signal.SIG_IGN); "
        code = ("STUBBORN" in words) * ignore + f"import time; time.sleep({value})"
        note(subprocess.Popen([sys.executable, "-c", code], start_new_session=True).pid)
        time.sleep(float(value))
if server:
    server.kill()
"""


class FakeProvider:
    name = "fake"
    efforts = ("low", "high")

    def models(self) -> list[str]:
        return ["fake-small", "fake-large"]

    def invocation(self, ctx: RunContext) -> Invocation:
        resumed = "resumed" if ctx.agent.session_id else "new"
        config = f"{ctx.model}/{ctx.effort}"
        canary = ctx.scratch / "canary" if ctx.loadout.memory else None
        mode = ["memory", str(canary)] if canary else ["plain"]
        argv = [sys.executable, "-c", SCRIPT, resumed, config, *mode]
        stdin = stream_json_prompt(ctx.prompt) if canary else ctx.prompt
        return Invocation(argv=argv, stdin=stdin, canary=canary)

    def parse(self, line: str) -> list[Event]:
        data = json.loads(line)
        if "session" in data:
            return [Event("session", session_id=data["session"], model=data.get("model"))]
        if "text" in data:
            return [Event("text", data["text"])]
        usage = data.get("usage")
        return [
            Event(
                NOTIFICATION_RESULT if data.get("notification") else "result",
                data["result"],
                is_error=data.get("error", False),
                usage=usage and Usage(**usage, cumulative=data["cumulative"]),
            )
        ]

    def attach_command(self, agent: Agent) -> list[str]:
        return ["true"]
