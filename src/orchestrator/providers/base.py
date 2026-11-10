"""The provider contract: how one coding-agent CLI is launched and how its output is read.

Adding an agent (Cursor, opencode, ...) means implementing this protocol and registering
the class in providers/__init__.py. Nothing else in the orchestrator knows provider details.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from ..loadout import Loadout
from ..models import Agent, Event

# Event kind of a result the CLI produced on its own rather than in answer to the prompt:
# a background command it had started ended (or, on resume, was found stopped) and it took
# another turn to report that. The runner counts it as a result, but it does not end a
# lead's turn the way the answer to the prompt does.
NOTIFICATION_RESULT = "notification_result"


@dataclass
class McpServer:
    """The orchestrator's own MCP server, handed to agents that may run sub-agents."""

    command: str
    args: list[str]
    env: dict[str, str]


@dataclass
class RunContext:
    agent: Agent
    prompt: str
    system_prompt: str
    scratch: Path  # per-run directory for files the CLI needs (schemas, configs)
    mcp: McpServer | None = None
    lead: bool = False  # the role runs sub-agents and must stay reachable between turns
    output_schema: dict[str, Any] | None = None
    model: str | None = None  # resolved from the agent and the settings
    effort: str | None = None
    writable_dirs: list[str] = field(default_factory=list)  # beyond the cwd, e.g. the shared .git
    loadout: Loadout = field(default_factory=Loadout)  # the default restricts nothing


@dataclass
class Invocation:
    argv: list[str]
    stdin: str
    env: dict[str, str] = field(default_factory=dict)
    unset_env: list[str] = field(default_factory=list)
    # A file the CLI creates (SessionStart hook) as proof that its hooks run; the runner
    # sends stdin only after it appears, otherwise it relaunches without `memory`.
    canary: Path | None = None


class Provider(Protocol):
    name: str
    efforts: tuple[str, ...]  # reasoning-effort levels the CLI accepts

    def models(self) -> Sequence[str | tuple[str, str]]:
        """Models worth offering in a picker: names, or (value for the CLI, label) pairs.

        Any other name is still accepted. A provider whose list is a stand-in may say so in
        an optional `models_note` attribute (read after this call).
        """
        ...

    def invocation(self, ctx: RunContext) -> Invocation:
        """Command line for one headless turn; resumes agent.session_id when it is set."""
        ...

    def parse(self, line: str) -> list[Event]:
        """Translate one line of the CLI's output stream into normalised events."""
        ...

    def attach_command(self, agent: Agent) -> list[str]:
        """Interactive command a person can run to take over the agent's conversation."""
        ...


def model_choices(provider: Provider) -> dict[str, Any]:
    """A provider's picker list as {"models": [{"value", "label"}], "models_note"}."""
    models = [
        {"value": m, "label": m} if isinstance(m, str) else {"value": m[0], "label": m[1]}
        for m in provider.models()
    ]
    return {"models": models, "models_note": getattr(provider, "models_note", None)}


def brief(value: Any, limit: int = 300) -> str:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
    text = " ".join(text.split())
    return text if len(text) <= limit else text[:limit] + "…"
