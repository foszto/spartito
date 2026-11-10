"""Decisions the owner is asked for: one list across projects instead of questions buried in
conversations.

The orchestrator and the leads record one with the MCP tool `request_decision` (table
`decisions`); it stays `open` until the owner answers it (dashboard, `orch decide`), or the
agent withdraws it (`withdraw_decision`), or the agent is removed. The answer reaches the
agent as an ordinary user message (Orchestrator.send, sender None):

    Decision #12 — <question>
    Answer: <answer>

A message to an archived agent restores it, as any message does. Recording one shows a
desktop notification (desktop.py).
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from typing import Any

from . import desktop
from .models import Agent
from .store import Store, now

OPEN, ANSWERED, WITHDRAWN = "open", "answered", "withdrawn"
QUESTION_CHARS = 500
OPTION_CHARS = 100
MAX_OPTIONS = 6
RECOMMENDATION_CHARS = 1000
ANSWER_CHARS = 4000
REMOVED = "the agent was removed"


@dataclass
class Decision:
    id: int
    agent_id: str
    project: str | None
    question: str
    options: list[str] = field(default_factory=list)
    recommendation: str | None = None
    state: str = OPEN
    answer: str | None = None
    created_at: str = ""
    closed_at: str | None = None
    run_id: int | None = None
    note: str | None = None

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> Decision:
        data = dict(row)
        data["options"] = json.loads(data.get("options") or "[]")
        names = cls.__dataclass_fields__
        return cls(**{key: value for key, value in data.items() if key in names})

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _text(value: Any, what: str, limit: int, required: bool = True) -> str:
    if value is None:
        value = ""
    if not isinstance(value, str):
        raise ValueError(f"{what} must be a string")
    value = value.strip()
    if required and not value:
        raise ValueError(f"{what} must not be empty")
    if len(value) > limit:
        raise ValueError(f"{what} is too long ({len(value)} chars; at most {limit})")
    return value


def check(question: Any, options: Any, recommendation: Any) -> tuple[str, list[str], str | None]:
    """A decision request as stored. Raises ValueError."""
    question = _text(question, "question", QUESTION_CHARS)
    if options is None:
        options = []
    if not isinstance(options, list):
        raise ValueError("options must be a list of short strings")
    if len(options) > MAX_OPTIONS:
        raise ValueError(f"at most {MAX_OPTIONS} options")
    cleaned = [_text(option, f"option {n}", OPTION_CHARS) for n, option in enumerate(options, 1)]
    if len(set(cleaned)) < len(cleaned):
        raise ValueError("options must differ from each other")
    return (
        question,
        cleaned,
        _text(recommendation, "recommendation", RECOMMENDATION_CHARS, False) or None,
    )


def record(
    store: Store, agent: Agent, question: Any, options: Any, recommendation: Any
) -> Decision:
    """Record an open decision for the agent and its project, and say so on the desktop.
    Raises ValueError."""
    question, options, recommendation = check(question, options, recommendation)
    with store.connection() as conn:
        cursor = conn.execute(
            "INSERT INTO decisions (agent_id, project, question, options, recommendation,"
            " state, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (agent.id, agent.project, question, json.dumps(options), recommendation, OPEN, now()),
        )
        decision_id = cursor.lastrowid
    decision = get(store, decision_id)
    assert decision is not None
    where = agent.project or desktop.display_name(store, agent.id)
    desktop.show(store, f"Decision needed ({where}): {question}")
    return decision


def get(store: Store, decision_id: int) -> Decision | None:
    with store.connection() as conn:
        row = conn.execute("SELECT * FROM decisions WHERE id = ?", (decision_id,)).fetchone()
    return Decision.from_row(row) if row else None


def listing(store: Store, closed: bool = False) -> list[Decision]:
    """Open decisions, oldest first; with `closed` all of them."""
    query = "SELECT * FROM decisions" + ("" if closed else " WHERE state = ?") + " ORDER BY id"
    with store.connection() as conn:
        rows = conn.execute(query, () if closed else (OPEN,)).fetchall()
    return [Decision.from_row(row) for row in rows]


def _close(
    store: Store,
    decision_id: int,
    state: str,
    answer: str | None = None,
    note: str | None = None,
    agent_id: str | None = None,
) -> bool:
    """Close an open decision (of `agent_id` only, when given); False if it was not open."""
    query = (
        "UPDATE decisions SET state = ?, answer = ?, note = ?, closed_at = ?"
        " WHERE id = ? AND state = ?"
    )
    args: tuple[Any, ...] = (state, answer, note, now(), decision_id, OPEN)
    if agent_id is not None:
        query, args = query + " AND agent_id = ?", (*args, agent_id)
    with store.connection() as conn:
        return conn.execute(query, args).rowcount == 1


def refusal(decision: Decision | None, decision_id: int) -> str:
    if decision is None:
        return f"there is no decision #{decision_id}"
    return f"decision #{decision_id} is already {decision.state}"


def withdraw(store: Store, decision_id: int, agent_id: str) -> Decision:
    """Withdraw one of the agent's own open decisions. Raises ValueError."""
    decision = get(store, decision_id)
    if decision is None or decision.agent_id != agent_id:
        raise ValueError(f"you have no decision #{decision_id}")
    if not _close(store, decision_id, WITHDRAWN, agent_id=agent_id):
        raise ValueError(refusal(get(store, decision_id), decision_id))
    return get(store, decision_id) or decision


def withdraw_all(store: Store, agent_id: str, note: str) -> None:
    """Close the agent's open decisions without an answer (it is being removed)."""
    with store.connection() as conn:
        conn.execute(
            "UPDATE decisions SET state = ?, note = ?, closed_at = ?"
            " WHERE agent_id = ? AND state = ?",
            (WITHDRAWN, note, now(), agent_id, OPEN),
        )


def choose(decision: Decision, answer: Any) -> str:
    """The answer as given, or the option its number names (1-based). Raises ValueError."""
    text = _text(answer, "answer", ANSWER_CHARS)
    if text.isdigit() and 1 <= int(text) <= len(decision.options):
        return decision.options[int(text) - 1]
    return text


def message(decision: Decision, answer: str) -> str:
    return f"Decision #{decision.id} — {decision.question}\nAnswer: {answer}"


def answer(
    store: Store, decision_id: int, given: Any, send: Callable[[str, str], int]
) -> tuple[Decision, int]:
    """Answer an open decision: close it as answered, then deliver the answer to its agent
    with `send(agent_id, text)` (returns the run id). A closed decision is refused; one
    whose agent no longer exists is closed with a note and refused. Raises ValueError."""
    decision = get(store, decision_id)
    if decision is None or decision.state != OPEN:
        raise ValueError(refusal(decision, decision_id))
    text = choose(decision, given)
    if store.get_agent(decision.agent_id) is None:
        _close(store, decision_id, WITHDRAWN, note=REMOVED)
        raise ValueError(
            f"decision #{decision_id}: agent '{decision.agent_id}' no longer exists;"
            " the decision was closed without an answer"
        )
    if not _close(store, decision_id, ANSWERED, answer=text):  # someone else was quicker
        raise ValueError(refusal(get(store, decision_id), decision_id))
    try:
        run_id = send(decision.agent_id, message(decision, text))
    except BaseException:
        with store.connection() as conn:  # not delivered: open again
            conn.execute(
                "UPDATE decisions SET state = ?, answer = NULL, closed_at = NULL WHERE id = ?",
                (OPEN, decision_id),
            )
        raise
    with store.connection() as conn:
        conn.execute("UPDATE decisions SET run_id = ? WHERE id = ?", (run_id, decision_id))
    return get(store, decision_id) or decision, run_id
