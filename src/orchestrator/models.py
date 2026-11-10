"""Domain records. Plain dataclasses that mirror the SQLite rows."""

from __future__ import annotations

from dataclasses import dataclass, field, fields
from enum import StrEnum
from typing import Any


class RunStatus(StrEnum):
    WAITING = "waiting"  # queued behind dependencies or an earlier run of the same agent
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    STOPPED = "stopped"
    SKIPPED = "skipped"  # a dependency did not succeed

    @property
    def active(self) -> bool:
        return self in (RunStatus.WAITING, RunStatus.RUNNING)


ACTIVE = (RunStatus.WAITING.value, RunStatus.RUNNING.value)


@dataclass
class Project:
    name: str
    path: str
    base_branch: str
    default_provider: str = "claude"
    created_at: str = ""


@dataclass
class Agent:
    id: str
    project: str | None  # None for the top-level orchestrator agent
    name: str
    role: str
    provider: str
    cwd: str
    parent_id: str | None = None
    model: str | None = None
    branch: str | None = None  # set when the agent owns a worktree
    base_ref: str | None = None
    session_id: str | None = None  # provider-side conversation id, known after the first run
    permission: str | None = None
    created_at: str = ""
    effort: str | None = None  # explicit override; otherwise from settings

    @property
    def owns_worktree(self) -> bool:
        return self.branch is not None


@dataclass
class Run:
    """One turn: a prompt sent to an agent and the answer it produced."""

    id: int
    agent_id: str
    prompt: str
    status: RunStatus = RunStatus.WAITING
    depends_on: list[int] = field(default_factory=list)
    output_schema: dict[str, Any] | None = None
    result: str | None = None
    structured: Any = None
    error: str | None = None
    cost_usd: float | None = None
    pid: int | None = None
    created_at: str = ""
    started_at: str | None = None
    ended_at: str | None = None


TOKEN_FIELDS = ("input_tokens", "output_tokens", "cache_read_tokens", "cache_creation_tokens")


@dataclass
class Usage:
    """Tokens and cost of one run, as the provider CLI reported them.

    `input_tokens` excludes cache reads and cache writes for every provider, so
    input + cache_read + cache_creation is the whole prompt. `cumulative` marks figures
    that are totals for the whole session/thread rather than for this run; the runner
    turns those into per-run values before storing them.
    """

    input_tokens: int | None = None
    output_tokens: int | None = None
    cache_read_tokens: int | None = None
    cache_creation_tokens: int | None = None
    cost_usd: float | None = None
    model: str | None = None  # the model the CLI says it used
    cumulative: bool = False

    def __add__(self, other: Usage) -> Usage:
        def add(a: Any, b: Any) -> Any:
            return b if a is None else a if b is None else a + b

        return Usage(
            *(add(getattr(self, key), getattr(other, key)) for key in (*TOKEN_FIELDS, "cost_usd")),
            model=other.model or self.model,
        )

    def since(self, earlier: Usage) -> Usage:
        """Per-run figures from a session total and the total stored before this run.

        Falls back to the reported figures when they are lower than what was stored before
        (the session or thread was started over).
        """
        keys = (*TOKEN_FIELDS, "cost_usd")
        delta = {key: (getattr(self, key), getattr(earlier, key) or 0) for key in keys}
        if any(now is not None and now < before - 1e-9 for now, before in delta.values()):
            return Usage(**{key: getattr(self, key) for key in keys}, model=self.model)
        values = {
            key: None if now is None else now - before for key, (now, before) in delta.items()
        }
        if values["cost_usd"] is not None:
            values["cost_usd"] = round(values["cost_usd"], 8)
        return Usage(**values, model=self.model)

    def as_dict(self) -> dict[str, Any]:
        cost = None if self.cost_usd is None else round(self.cost_usd, 4)
        return {**{key: getattr(self, key) for key in TOKEN_FIELDS}, "cost_usd": cost}


def from_row[T](cls: type[T], row: Any) -> T:
    """Build a record from a database row, ignoring columns a newer version added."""
    names = {f.name for f in fields(cls)}
    return cls(**{key: value for key, value in dict(row).items() if key in names})


@dataclass
class Event:
    """A normalised, provider-independent line of agent activity."""

    kind: str  # session | text | tool | tool_result | result | error | info
    text: str = ""
    session_id: str | None = None
    cost_usd: float | None = None
    is_error: bool = False
    structured: Any = None
    model: str | None = None  # the model the CLI reports, when it does (session events)
    usage: Usage | None = None  # on result events
    id: int = 0
    run_id: int = 0
    agent_id: str = ""
    ts: str = ""
