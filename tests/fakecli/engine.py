"""The fake CLI's engine: `python engine.py <dialect> <the CLI's arguments>` (see __init__).

A dialect module defines `Dialect(argv)` with:
- `record(seen)`: notes the private files of its options in `seen`;
- `before_input()`: what the CLI does before it reads stdin (Claude: its SessionStart hooks);
- `prompt(raw)`: the prompt in stdin;
- `session()`, `compaction(trigger, pre, post)`, `result(text, error, usage)`: the CLI's
  output lines (dicts) for these events; `usage` has the keys of TOKENS (see __init__).
Optional: `command()`, the exit code of a call that is no turn (it has answered it; None: a
turn), and `exit_code`, the turn's (default 0).
"""

import importlib
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # the fakecli package


def emit(lines: list[dict]) -> None:
    for line in lines:
        print(json.dumps(line), flush=True)


def main() -> None:
    name, argv = sys.argv[1], sys.argv[2:]
    cli = importlib.import_module(f"fakecli.{name}").Dialect(argv)
    command = getattr(cli, "command", None)
    code = command() if command else None
    if code is not None:
        sys.exit(code)
    seen: dict = {"argv": argv}
    cli.record(seen)
    names = [n for n in os.environ.get(f"FAKE_{name.upper()}_ENV", "").split(",") if n]
    seen["env"] = {n: os.environ.get(n) for n in names}
    cli.before_input()
    prompt = seen["prompt"] = cli.prompt(sys.stdin.read())
    with open(os.environ[f"FAKE_{name.upper()}_LOG"], "a") as log:
        log.write(json.dumps(seen) + "\n")
    emit(cli.session())
    words = prompt.split()
    usage = None
    for flag, value in zip(words, words[1:], strict=False):
        if flag == "SLEEP":
            time.sleep(float(value))
        if flag == "TOKENS":
            n = int(value)
            usage = {
                "input_tokens": n,
                "output_tokens": n // 10,
                "cache_read_tokens": 2 * n,
                "cache_creation_tokens": n // 2,
                "cost_usd": n / 100000,
            }
    if prompt.startswith("/compact"):
        emit(cli.compaction("manual", 48450, 901))
        emit(cli.result("", False, usage))
    elif "FAIL" in words:
        emit(cli.result("boom", True, usage))
    else:
        emit(cli.result(f"done: {prompt}", False, usage))
    sys.exit(getattr(cli, "exit_code", 0))


if __name__ == "__main__":
    main()
