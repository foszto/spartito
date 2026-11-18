"""A fake CLI that runs in place of a provider's real one: one engine and a dialect per CLI.

A provider's own command line (its invocation) runs with `python engine.py <dialect>` in
place of its executable (`launch`); the arguments, stdin and environment are the ones the
provider built. The engine (engine.py) records what the CLI was started with and plays the
scenario that the words of the prompt describe; the dialect (claude.py, codex.py) reads the
CLI's own options and input and writes the abstract events as the CLI's own output lines.

What it records: one JSON line per launch in `fake-<dialect>.jsonl` in the run's scratch
directory (FAKE_<DIALECT>_LOG names the file; `runs` reads it): `argv` (without the
executable), `prompt` (stdin, as the dialect reads it), `env` (the values of the variables
that FAKE_<DIALECT>_ENV names, comma separated), and the private files the dialect knows of.

The scenario words of the prompt:
- `SLEEP <s>`: waits s seconds before the answer.
- `TOKENS <n>`: the result reports usage for the turn: n input tokens, n // 10 output,
  2n cache reads, n // 2 cache writes and n / 100000 USD (as FakeProvider's script), each in
  the CLI's own terms.
- `FAIL`: ends with an error result (`boom`).
- a prompt that starts with `/compact`: a manual compaction (48450 -> 901 tokens) and an
  empty result.
Any other prompt is answered with `done: <prompt>`.

FAKE_<DIALECT>_MISSING=1: the command line starts with a name that is not installed.

This module and the dialects use only the standard library: the engine imports them in a
process of its own.
"""

from __future__ import annotations

import dataclasses
import json
import os
import sys
from pathlib import Path

ENGINE = Path(__file__).with_name("engine.py")
DIALECTS = ("claude", "codex")  # a module of this package each


def log_variable(dialect: str) -> str:
    return f"FAKE_{dialect.upper()}_LOG"


def env_variable(dialect: str) -> str:
    return f"FAKE_{dialect.upper()}_ENV"


def log_file(scratch: Path, dialect: str) -> Path:
    return Path(scratch) / f"fake-{dialect}.jsonl"


def launch(invocation, dialect: str, scratch: Path):
    """A provider's invocation of the CLI `dialect` with the engine in place of the CLI, and
    its log in the run's scratch directory."""
    assert invocation.argv[0] == dialect, invocation.argv
    missing = os.environ.get(f"FAKE_{dialect.upper()}_MISSING") == "1"
    start = [f"fake-{dialect}-not-installed"] if missing else [sys.executable, str(ENGINE), dialect]
    return dataclasses.replace(
        invocation,
        argv=[*start, *invocation.argv[1:]],
        env={**invocation.env, log_variable(dialect): str(log_file(scratch, dialect))},
    )


def runs(scratch: Path, dialect: str) -> list[dict]:
    """What the fake CLI saw, one entry per launch."""
    log = log_file(scratch, dialect)
    return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []


def private_file(path: str) -> dict:
    """A file the CLI was given: its parsed JSON content, and its and its directory's mode
    (octal strings)."""
    file = Path(path)
    return {
        "content": json.loads(file.read_text()),
        "mode": oct(file.stat().st_mode & 0o777),
        "dir_mode": oct(file.parent.stat().st_mode & 0o777),
    }
