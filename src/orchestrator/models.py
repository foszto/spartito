"""Domain records. Plain dataclasses that mirror the SQLite rows."""

from __future__ import annotations

from collections.abc import Collection, Iterable
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
# How full the conversation was after the run, and a compaction the run saw (see Usage).
CONTEXT_FIELDS = (
    "context_tokens",
    "context_window",
    "compact_trigger",
    "compact_pre_tokens",
    "compact_post_tokens",
    "first_context_tokens",
)


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
    session_id: str | None = None  # session/thread the run belonged to, when reported
    # The conversation's size after the run: the prompt of its last model request (Claude:
    # input + cache read + cache write of the last main-chain message; Codex: the input of
    # the last request), and the model's context window. Unknown (None) after a compaction
    # that no request followed. Not summed: a property of the session at the run's end.
    context_tokens: int | None = None
    context_window: int | None = None
    # A compaction seen in the run: 'manual' | 'auto', with the sizes the CLI reported
    # (Claude's post_tokens counts the conversation only, not system prompt and tools).
    compact_trigger: str | None = None
    compact_pre_tokens: int | None = None
    compact_post_tokens: int | None = None
    # The conversation's size at the run's first model request (Claude): after a compaction,
    # what it really left, system prompt and tools included. Unknown for Codex.
    first_context_tokens: int | None = None

    def __add__(self, other: Usage) -> Usage:
        def add(a: Any, b: Any) -> Any:
            return b if a is None else a if b is None else a + b

        return Usage(
            *(add(getattr(self, key), getattr(other, key)) for key in (*TOKEN_FIELDS, "cost_usd")),
            model=other.model or self.model,
        )

    def since(self, earlier: Usage) -> Usage:
        """Per-run figures from a session total and the running total before this run.

        Never negative: when a reported figure is lower than what came before (the session
        or thread was started over) the reported figures are taken as this run's own.
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

    def tokens(self) -> dict[str, int] | None:
        """The four token counts with short keys, or None when none were reported."""
        if all(getattr(self, key) is None for key in TOKEN_FIELDS):
            return None
        short = ("in", "out", "cache_read", "cache_write")
        return {
            name: getattr(self, key) or 0 for name, key in zip(short, TOKEN_FIELDS, strict=True)
        }

    @property
    def cost(self) -> float | None:
        return None if self.cost_usd is None else round(self.cost_usd, 4)


def running_total(runs: Iterable[Run], usage: dict[int, Usage]) -> Usage:
    """Tokens and cost of an agent's runs so far, walking them in id order.

    A run with a usage record adds its own figures. A run without one but with a cost was
    written by code from before usage records (possibly still running next to newer code):
    its `cost_usd` is Claude's running total for the session, so it raises the total to at
    least that value instead of adding to it.
    """
    total = Usage()
    for run in sorted(runs, key=lambda run: run.id):
        if run.id in usage:
            total = total + usage[run.id]
        elif run.cost_usd:
            total.cost_usd = max(total.cost_usd or 0, run.cost_usd)
    return total


def answers(runs: Iterable[Run], compactions: Collection[int]) -> list[Run]:
    """The runs that answer something: all but compaction runs (`compactions`, from
    Store.compactions, read after the runs). A compaction is housekeeping: it is never an
    agent's answer, last run or status, nor the result a `{{result:…}}` reference means."""
    return [run for run in runs if run.id not in compactions]


def turn_order(run: Run) -> tuple[str, int]:
    """Sort key for the order in which runs took their turns: an automatic compaction runs
    ahead of messages queued before it (lower ids). By start time, then id (a tie within
    one second is taken in id order)."""
    return (run.started_at or "", run.id)


def context_now(
    runs: Iterable[Run], usage: dict[int, Usage], session_id: str | None
) -> Usage | None:
    """The usage record that tells how full the agent's conversation is now: that of its
    latest run (in the order they ran, see turn_order) in the current session with a size
    or a compaction. A compaction that no request followed leaves the size unknown
    (context_tokens None, compact_trigger set): an older figure would be stale. None when
    no run in the session reported either."""
    if not session_id:
        return None
    for run in sorted(runs, key=turn_order, reverse=True):
        record = usage.get(run.id)
        if record is None or record.session_id != session_id:
            continue
        if record.context_tokens is not None or record.compact_trigger:
            return record
    return None


def from_row[T](cls: type[T], row: Any) -> T:
    """Build a record from a database row, ignoring columns a newer version added."""
    names = {f.name for f in fields(cls)}
    return cls(**{key: value for key, value in dict(row).items() if key in names})


@dataclass
class Event:
    """A normalised, provider-independent line of agent activity."""

    # session | text | tool | tool_result | result | error | info; parsers also yield
    # `context` (usage.context_tokens/model of a model request) and `compact` (a compaction,
    # text and usage.compact_*), which the runner keeps on the run's usage, not as events.
    kind: str
    text: str = ""
    session_id: str | None = None
    cost_usd: float | None = None
    is_error: bool = False
    structured: Any = None
    model: str | None = None  # the model the CLI reports, when it does (session events)
    usage: Usage | None = None  # on result, context and compact events
    windows: dict[str, int] | None = None  # on result events: context window per model
    id: int = 0
    run_id: int = 0
    agent_id: str = ""
    ts: str = ""
