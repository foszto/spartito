"""Who calls through the MCP server, and where it may act.

The calling agent's id arrives in ORCH_AGENT_ID. An agent with a project (a lead and the
agents below it) is confined to that project: its agents, board items and runs. The top-level
orchestrator (no project) and the interactive `spartito chat` session (no id) may act everywhere.
"""

from __future__ import annotations

import os
from typing import Any, Protocol

from ..errors import OrchestratorError
from ..models import Agent, Project
from ..store import ANYONE, Store


class Facade(Protocol):
    """What these functions use of the facade (they are its methods)."""

    store: Store

    def agent(self, agent_id: str) -> Agent: ...


class Caller:
    """The caller of one MCP tool call; `id` is "" for the interactive session."""

    def __init__(self, orch: Facade, agent_id: str | None) -> None:
        self.orch = orch
        self.id = agent_id or ""

    @classmethod
    def from_env(cls, orch: Facade) -> Caller:
        return cls(orch, os.environ.get("ORCH_AGENT_ID"))

    def agent(self) -> Agent | None:
        """The calling agent; None for the interactive session."""
        agent_id = self.id
        return self.orch.agent(agent_id) if agent_id else None

    def scoped(self) -> bool:
        """True when the caller is an agent confined to one project."""
        caller = self.agent()
        return bool(caller and caller.project)

    def project(self, project: str | None) -> str:
        """Resolve the project argument; agents are confined to their own project."""
        caller = self.agent()
        if caller and caller.project:
            if project and project != caller.project:
                raise OrchestratorError(f"you may only act within project '{caller.project}'")
            return caller.project
        if not project:
            raise OrchestratorError("project is required")
        return project

    def own(self, agent_id: str) -> str:
        caller = self.agent()
        if caller and caller.project and self.orch.agent(agent_id).project != caller.project:
            raise OrchestratorError(f"agent '{agent_id}' is outside project '{caller.project}'")
        return agent_id

    def projects(self) -> list[Project]:
        """The registered projects the caller may see."""
        caller = self.agent()
        return [
            project
            for project in self.orch.store.list_projects()
            if not (caller and caller.project) or project.name == caller.project
        ]

    def sender(self) -> str | None:
        return self.id or None

    def acts_as(self) -> Any:
        """Whose queued runs the caller may withdraw or replace: its own; the interactive
        session may act on any run."""
        return self.sender() or ANYONE


def lineage(orch: Facade, agent_id: str) -> set[str]:
    """The agent and its ancestors."""
    seen: set[str] = set()
    current = orch.store.get_agent(agent_id)
    while current and current.id not in seen:
        seen.add(current.id)
        current = orch.store.get_agent(current.parent_id) if current.parent_id else None
    return seen
