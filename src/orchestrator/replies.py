"""Bounded texts for replies to agents: results cut to a budget, saying how to get the rest.

Used by the MCP tools and by the notifications the orchestrator sends to parent agents.
"""

from __future__ import annotations

import json
from typing import Any

from .models import Run


def clip(
    text: str, limit: int, offset: int = 0, run_id: int | None = None, paged: bool = True
) -> str:
    """`limit` chars of `text` from `offset` (limit <= 0: all), saying what was left out.

    The note names the get_agent call for the rest: with `run_id` that run's, and with
    `paged` the `result_offset` to continue from (errors are not paged).
    """
    total = len(text)
    offset = max(0, offset)
    end = total if limit <= 0 else min(total, offset + limit)
    if offset == 0 and end == total:
        return text
    if offset >= total:
        return f"[result_offset {offset} is past the end: {total} chars]"
    part = text[offset:end]
    if offset:
        part = f"[chars {offset}-{end} of {total}] {part}"
    if end < total:
        call = f"get_agent run_id={run_id} " if run_id else ""
        page = f"{call}result_offset={end}; " if paged and run_id else call
        part += f"… [cut at {end} of {total} chars; {page}result_chars=0 for all]"
    return part


def structured_page(data: Any, limit: int, offset: int = 0, run_id: int | None = None) -> Any:
    """Structured output as is when its JSON fits in `limit` and no offset is asked for,
    else a page of that JSON."""
    text = json.dumps(data, ensure_ascii=False)
    if offset <= 0 and (limit <= 0 or len(text) <= limit):
        return data
    return clip(text, limit, offset, run_id)


def run_reply(run: Run, budget: int | None, answer: bool = True) -> tuple[dict[str, Any], int]:
    """A finished run for wait_for_agents: its error and its answer (structured output
    when it has one, else its text), together cut to `budget` chars of JSON (None: no
    limit).

    Returns the reply and the budget left. A text that gets no room at all is left out
    and the reply points to get_agent instead.
    """
    reply: dict[str, Any] = {"id": run.id, "status": run.status.value}
    left = budget
    texts: list[tuple[str, str, Any]] = []
    if run.error:
        texts.append(("error", run.error, None))
    if answer and run.structured is not None:
        texts.append(("structured", json.dumps(run.structured, ensure_ascii=False), run))
    elif answer and run.result is not None:
        texts.append(("result", run.result, None))
    for key, text, structured in texts:
        if left is not None and left <= 0:
            reply["more"] = f"get_agent run_id={run.id}"
            continue
        size = len(json.dumps(structured.structured)) if structured else 0
        if structured and (left is None or size <= left):
            reply[key] = structured.structured
            left = None if left is None else left - size
        elif left is None:
            reply[key] = text
        elif chars := fitting(text, left):
            reply[key] = clip(text, chars, run_id=run.id, paged=key != "error")
            left -= json_len(text[:chars])
        else:  # not even one char fits (clip would read a limit of 0 as "all")
            reply["more"] = f"get_agent run_id={run.id}"
            left = 0
    return reply, (0 if left is None else left)


def json_len(text: str) -> int:
    """Length of `text` once quoted in a JSON reply, without the quotes (escaping non-ASCII
    too, the longer of the two ways to serialize it)."""
    return len(json.dumps(text)) - 2


def fitting(text: str, budget: int) -> int:
    """How many leading chars of `text` take at most `budget` chars of JSON."""
    low, high = 0, len(text)
    while low < high:
        middle = (low + high + 1) // 2
        if json_len(text[:middle]) <= budget:
            low = middle
        else:
            high = middle - 1
    return low
