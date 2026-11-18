"""The provider contract: how one coding-agent CLI is launched and how its output is read.

Adding an agent (Cursor, opencode, ...) means implementing this protocol and registering
the class in providers/__init__.py. Nothing else in the orchestrator knows provider details.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
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


@dataclass(frozen=True)
class Wiring:
    """What one capability (capabilities.py) adds to one provider's turn: arguments and
    environment variables. Each provider decides where the arguments go on its command
    line; the environment is set for the CLI's process. `mcp_tools`: what it adds brings
    MCP tools (Claude in Chrome's browser tools do), which a provider may load on demand
    rather than with every request (Claude: tool search, loadout `tool_search`)."""

    argv: tuple[str, ...] = ()
    env: Mapping[str, str] = field(default_factory=dict)
    mcp_tools: bool = False


@dataclass(frozen=True)
class Support:
    """How one provider carries a capability: `on` when the agent's turn has it, `off`
    when it does not (e.g. to keep the user's own default of the CLI away from agents)."""

    on: Wiring
    off: Wiring = Wiring()


def wired(wirings: Sequence[Wiring]) -> tuple[list[str], dict[str, str]]:
    """The arguments and environment of a turn's wirings, in order (a later one wins)."""
    argv: list[str] = []
    env: dict[str, str] = {}
    for wiring in wirings:
        argv += wiring.argv
        env.update(wiring.env)
    return argv, env


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
    # A compaction turn (Codex): have the CLI compact once the conversation exceeds this
    # many tokens, which is below its current size.
    compact_below: int | None = None
    # What the turn's capabilities add (capabilities.resolve); a provider places each
    # wiring's argv on its command line and its env in the CLI's environment. A provider
    # that ignores them gets none of the capabilities.
    wirings: tuple[Wiring, ...] = ()


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

    # Optional self-description, looked up with getattr (a plugin may leave all of it out):
    #   label: str                      the name people read ("Claude Code"); default: name
    #   description: str                one line on what it is and how it is run; default ""
    #   executable: str | None          the CLI's command name, checked by the `providers` command;
    #                                   None (default): not checked, its status is unknown
    #   version_args: tuple[str, ...]   what the check runs; default ("--version",)
    #   login_args: tuple[str, ...] | None
    #                                   a command that exits non-zero when not logged in; it
    #                                   must be offline and never prompt. None (default): the
    #                                   login is not checked
    #   reports_cost: bool | None       whether its turns report a cost; None: not said
    #   role_prompt: str | None         "every_turn" or "first_turn": when it gets the role
    #                                   prompt; None: not said
    #   limits: tuple[str, ...]         what it does not support, one phrase each
    #
    # Optional behaviour, looked up with getattr:
    #   compact_prompt(instructions: str | None) -> str
    #       The prompt of a turn that compacts the conversation (Orchestrator.compact); the
    #       runner passes it as is. Raises ValueError for instructions it cannot take.
    #       A provider without it cannot compact.
    #   compact_needs_size: bool
    #       True: a compaction turn needs the conversation's current size (compact_below).
    #   context_after(session_id: str, since: str) -> Usage | None
    #       The conversation's size (Usage.context_*) and a compaction since `since` (an
    #       ISO time; Usage.compact_*) when the output stream does not report them; read
    #       after the turn, best effort.


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
