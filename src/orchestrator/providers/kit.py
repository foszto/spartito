"""Building blocks shared by the providers. Nothing here knows a provider: each provider
writes its own `invocation()` and its own event mapping, and takes from here what is the
same for every CLI."""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .base import Event, McpServer, RunContext, wired_servers

# The one way to run a CLI briefly. It lives in locate, which probes the CLIs with it and
# holds the install directories it searches; a module of its own would import locate both
# ways round.
from .locate import run_cli as run_cli

# Spartito's own MCP server (`mcp__orch__…`), in every lead's turn; a capability's server of
# that name is left out.
OWN_SERVER = "orch"


# A provider's model list and its note (shown next to the picker; None: nothing to say).
Models = tuple[list[Any], str | None]
MODEL_LIST_RETRY_SECONDS = 600


class ModelCatalog:
    """A provider's model list, looked up once per process behind a lock. `lookup()` gives
    the list and its note, and handles its own failures; `retry_when(models, note)` says
    whether that answer is asked for again once it is `retry_seconds` old."""

    def __init__(
        self,
        lookup: Callable[[], Models],
        retry_when: Callable[[list[Any], str | None], bool],
        retry_seconds: float = MODEL_LIST_RETRY_SECONDS,
    ) -> None:
        self.lookup = lookup
        self.retry_when = retry_when
        self.retry_seconds = retry_seconds
        self.note: str | None = None
        self.at = 0.0  # time.monotonic() of the lookup
        self._models: list[Any] | None = None
        self._lock = threading.Lock()

    def models(self) -> list[Any]:
        with self._lock:
            age = time.monotonic() - self.at
            if self._models is None or (
                self.retry_when(self._models, self.note) and age >= self.retry_seconds
            ):
                self.at = time.monotonic()
                self._models, self.note = self.lookup()
            return self._models


class JsonLines:
    """Reads a CLI's output of one JSON object per line: a line that is no JSON object
    becomes an `info` event (none when blank), an object goes to `on_json`."""

    def feed(self, line: str) -> list[Event]:
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            data = None
        if not isinstance(data, dict):
            return [Event("info", line.strip())] if line.strip() else []
        return self.on_json(data)

    def on_json(self, data: dict) -> list[Event]:
        return []

    def finish(self, exit_code: int | None) -> list[Event]:
        """Events once the output has ended."""
        return []


def own_server_entry(mcp: McpServer) -> dict[str, Any]:
    """Spartito's own MCP server as an entry of the shape of mcp.Server.entry()."""
    return {"command": mcp.command, "args": mcp.args, "env": mcp.env}


def turn_servers(ctx: RunContext) -> dict[str, dict[str, Any]]:
    """The MCP servers of a turn, name -> entry as mcp.Server.entry() gives it: the own
    server first (when the turn has it), then the wirings' servers but one of the own name."""
    servers: dict[str, dict[str, Any]] = {}
    if ctx.mcp:
        servers[OWN_SERVER] = own_server_entry(ctx.mcp)
    for name, entry in wired_servers(ctx.wirings).items():
        if name != OWN_SERVER:
            servers[name] = dict(entry)
    return servers


def schema_file(ctx: RunContext) -> Path:
    """The turn's output schema written to its scratch directory, for a CLI that reads it
    from a file."""
    path = ctx.scratch / "output-schema.json"
    path.write_text(json.dumps(ctx.output_schema), encoding="utf-8")
    return path


def with_role_prefix(ctx: RunContext) -> str:
    """The prompt for a CLI without a system-prompt option: the role prompt opens the first
    turn of a session; a resumed session has it already."""
    if ctx.system_prompt and not ctx.agent.session_id:
        return f"{ctx.system_prompt}\n\n---\n\n{ctx.prompt}"
    return ctx.prompt


# How a commit's Co-Authored-By trailer names Spartito; `{project}` is the project's name.
TRAILER = "`Co-Authored-By: <name> via {project} <email>`"
# The rule for a CLI that asks for a trailer of its own unless that is switched off.
TRAILER_AS_ASKED = (
    "take the Co-Authored-By trailer your CLI asks for, keep its email exactly, and add "
    "` via {project}` right after the name: " + TRAILER + "; if your CLI asks for no "
    "trailer, add none, and do not invent a trailer or an email."
)
# The rule for a CLI without a trailer of its own: the agent may still add one.
TRAILER_IF_ADDED = (
    "when you add a Co-Authored-By trailer, keep its email exactly and add "
    "` via {project}` right after the name: " + TRAILER + "; do not invent a trailer "
    "or an email."
)


@dataclass(frozen=True)
class Credit:
    """How a provider's agents credit its CLI: `link`, after Spartito in the closing line of
    a pull request description, and `trailer_rule`, how a commit's trailer names Spartito."""

    link: str
    trailer_rule: str
