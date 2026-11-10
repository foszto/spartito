"""Domain records. Plain dataclasses that mirror the SQLite rows."""

from __future__ import annotations

from dataclasses import dataclass, field
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


@dataclass
class Event:
    """A normalised, provider-independent line of agent activity."""

    kind: str  # session | text | tool | tool_result | result | error | info
    text: str = ""
    session_id: str | None = None
    cost_usd: float | None = None
    is_error: bool = False
    structured: Any = None
    id: int = 0
    run_id: int = 0
    agent_id: str = ""
    ts: str = ""
