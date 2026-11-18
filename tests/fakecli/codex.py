"""Codex's dialect: `codex exec --json` (providers/codex.py, tests/fixtures/codex_*.jsonl).

- No private files: the `orch` MCP server comes through `-c` overrides in argv.
- The prompt is stdin as it is.
- The thread is the one of `exec resume <id>`, else a new one (`fake-codex-thread`).
- Reports no compaction (`codex exec --json` has no event for it) and no cost; its usage
  counts cached tokens inside `input_tokens`. The figures are those of this turn (the real
  CLI reports the thread's totals on resume): one turn with TOKENS per thread.
"""

from __future__ import annotations

SESSION = "fake-codex-thread"


class Dialect:
    def __init__(self, argv: list[str]) -> None:
        self.argv = argv

    def record(self, seen: dict) -> None:
        pass

    def before_input(self) -> None:
        pass

    @staticmethod
    def prompt(raw: str) -> str:
        return raw

    def session(self) -> list[dict]:
        resumed = self.argv[self.argv.index("resume") + 1] if "resume" in self.argv else None
        return [{"type": "thread.started", "thread_id": resumed or SESSION}]

    @staticmethod
    def compaction(trigger: str, pre: int, post: int) -> list[dict]:
        return []

    @staticmethod
    def result(text: str, error: bool, usage: dict | None) -> list[dict]:
        if error:
            return [{"type": "turn.failed", "error": {"message": text}}]
        message = {"type": "agent_message", "id": "item_0", "text": text}
        completed: dict = {"type": "turn.completed"}
        if usage:
            completed["usage"] = {
                "input_tokens": usage["input_tokens"] + usage["cache_read_tokens"],
                "cached_input_tokens": usage["cache_read_tokens"],
                "cache_write_input_tokens": usage["cache_creation_tokens"],
                "output_tokens": usage["output_tokens"],
                "reasoning_output_tokens": 0,
            }
        return [{"type": "item.completed", "item": message}, completed]
