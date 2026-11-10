"""MCP server (stdio) that exposes the orchestrator to an agent.

The top-level orchestrator session and every `lead` agent get this server. An agent's own
identity arrives in ORCH_AGENT_ID: agents it spawns become its children, and it is confined
to its own project. The top-level orchestrator has no project (or no ORCH_AGENT_ID at all,
for the interactive `orch chat` session) and may act everywhere.
"""

from __future__ import annotations

import os
from typing import Any

import anyio
from mcp.server.mcpserver import MCPServer

from . import roles
from .service import Orchestrator, OrchestratorError

MAX_WAIT_SECONDS = 1800

server = MCPServer(
    "orch",
    instructions=(
        "Tools to run coding agents (Claude Code, Codex) in git worktrees. Typical flow: "
        "spawn_agent (returns at once) -> wait_for_agents -> read results -> send_message "
        "for follow-ups. Use `after` to chain agents that depend on each other."
    ),
)


def _orch() -> Orchestrator:
    return Orchestrator()


def _caller(orch: Orchestrator):
    agent_id = os.environ.get("ORCH_AGENT_ID")
    return orch.agent(agent_id) if agent_id else None


def _scoped(orch: Orchestrator) -> bool:
    """True when the caller is an agent confined to one project."""
    caller = _caller(orch)
    return bool(caller and caller.project)


def _project(orch: Orchestrator, project: str | None) -> str:
    """Resolve the project argument; agents are confined to their own project."""
    caller = _caller(orch)
    if caller and caller.project:
        if project and project != caller.project:
            raise OrchestratorError(f"you may only act within project '{caller.project}'")
        return caller.project
    if not project:
        raise OrchestratorError("project is required")
    return project


def _own(orch: Orchestrator, agent_id: str) -> str:
    caller = _caller(orch)
    if caller and caller.project and orch.agent(agent_id).project != caller.project:
        raise OrchestratorError(f"agent '{agent_id}' is outside project '{caller.project}'")
    return agent_id


def _queued(orch: Orchestrator, agent_id: str, run_id: int) -> dict[str, Any]:
    view = orch.view(orch.agent(agent_id))
    keys = ("id", "project", "role", "provider", "status", "branch", "cwd")
    return {**{key: view[key] for key in keys}, "run_id": run_id}


@server.tool()
def list_projects() -> list[dict[str, Any]]:
    """List registered projects with the number of agents in each."""
    orch = _orch()
    caller = _caller(orch)
    return [
        {**vars(project), "agents": len(orch.store.list_agents(project.name))}
        for project in orch.store.list_projects()
        if not (caller and caller.project) or project.name == caller.project
    ]


@server.tool()
def add_project(
    path: str, name: str | None = None, default_provider: str = "claude"
) -> dict[str, Any]:
    """Register a git repository as a project. `path` may be any directory inside the repo."""
    orch = _orch()
    if _scoped(orch):
        raise OrchestratorError("only the top-level orchestrator registers projects")
    project = orch.add_project(path, name, default_provider)
    return {**vars(project), "note": orch.project_note(project)}


@server.tool()
def list_roles(project: str | None = None) -> list[dict[str, Any]]:
    """List the roles an agent can be spawned with (worker, reviewer, planner, ...)."""
    orch = _orch()
    path = orch.project(_project(orch, project)).path if project or _scoped(orch) else None
    return [
        {"name": r.name, "description": r.description, "lead": r.lead, "worktree": r.worktree}
        for r in roles.available(path)
    ]


@server.tool()
def spawn_agent(
    task: str,
    project: str | None = None,
    name: str | None = None,
    role: str = "worker",
    provider: str | None = None,
    model: str | None = None,
    effort: str | None = None,
    worktree: bool | None = None,
    share_worktree_with: str | None = None,
    base: str | None = None,
    after: list[str] | None = None,
    output_schema: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Start a new agent on a task. Returns immediately; use wait_for_agents for the outcome.

    task: the full, self-contained brief. The agent knows nothing else. It may embed
      `{{result:<agent-id>}}`, replaced by that agent's final answer when this agent starts
      (combine with `after`).
    name: short label, becomes part of the agent id and branch (`orch/<name>-xxxx`).
    role: see list_roles. `lead` agents can spawn their own sub-agents.
    provider: `claude` or `codex`; defaults to the project's default.
    model, effort: leave unset to use the configured defaults for the agent's tier; set them
      only when the user asks or the task clearly needs a stronger or cheaper setting.
    worktree: own git worktree and branch (default for most roles) or the main checkout.
    share_worktree_with: agent id whose worktree to work in instead (review, test, fix).
    base: branch/ref, or an agent id whose branch to start from; default is the project's
      base branch.
    after: agent ids (or run ids) that must finish successfully first. If one fails, this
      agent is skipped.
    output_schema: JSON Schema; the final answer is then returned as `structured` data.
    """
    orch = _orch()
    caller = _caller(orch)
    agent, run = orch.spawn(
        _project(orch, project),
        task,
        name=name,
        role=role,
        provider=provider,
        model=model,
        effort=effort,
        worktree=worktree,
        share_with=share_worktree_with and _own(orch, share_worktree_with),
        base=base,
        after=after,
        output_schema=output_schema,
        parent_id=caller.id if caller else None,
    )
    return _queued(orch, agent.id, run.id)


@server.tool()
def ask_project_lead(project: str, message: str) -> dict[str, Any]:
    """Send a message to a project's lead agent, creating it on first use.

    The lead works in the project's main checkout, keeps one long conversation about the
    project and can plan work and run its own worker agents. Returns immediately.
    """
    orch = _orch()
    if _scoped(orch):
        raise OrchestratorError("only the top-level orchestrator talks to project leads")
    lead = orch.lead(project)
    return _queued(orch, lead.id, orch.send(lead.id, message).id)


@server.tool()
def send_message(
    agent_id: str,
    message: str,
    after: list[str] | None = None,
    output_schema: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Send a follow-up to an existing agent; it continues its conversation with full memory.

    Messages to a busy agent queue up and run in order. Returns immediately.
    """
    orch = _orch()
    run = orch.send(_own(orch, agent_id), message, after, output_schema)
    return _queued(orch, agent_id, run.id)


@server.tool()
async def wait_for_agents(agent_ids: list[str], timeout_seconds: int = 600) -> dict[str, Any]:
    """Block until all the given agents are idle, then return their status and final answers.

    If `finished` is false the timeout elapsed first: call again to keep waiting.
    """
    orch = _orch()
    ids = [_own(orch, agent_id) for agent_id in agent_ids]
    timeout = max(1, min(timeout_seconds, MAX_WAIT_SECONDS))
    finished, views = await anyio.to_thread.run_sync(lambda: orch.wait(ids, timeout))
    return {"finished": finished, "agents": views}


@server.tool()
def list_agents(project: str | None = None) -> list[dict[str, Any]]:
    """List agents with status, hierarchy (parent_id), branch and a short last result.

    `model`/`effort` are what the agent's next turn runs with (`*_override` when set
    explicitly); `usage` holds its token and cost totals.
    """
    orch = _orch()
    scope = _project(orch, project) if project or _scoped(orch) else None
    return orch.overview(scope)


@server.tool()
def get_agent(agent_id: str) -> dict[str, Any]:
    """Full status of one agent: last result, the commits/changes in its worktree, and the
    tokens, cost and model of its last 10 runs (`recent_runs`)."""
    orch = _orch()
    orch.store.reconcile()
    agent = orch.agent(_own(orch, agent_id))
    return {
        **orch.view(agent),
        "changes": orch.changes(agent_id),
        "recent_runs": orch.run_history(agent, limit=10),
    }


@server.tool()
def agent_log(agent_id: str, tail: int = 40) -> list[str]:
    """Recent activity of an agent (messages, tool calls, errors) — to see what a running agent is doing."""
    orch = _orch()
    events = orch.store.events(_own(orch, agent_id), tail=tail)
    return [f"{event.ts} run {event.run_id} [{event.kind}] {event.text}" for event in events]


@server.tool()
def stop_agent(agent_id: str) -> dict[str, Any]:
    """Stop an agent's running and queued turns. The agent and its worktree are kept."""
    orch = _orch()
    return {"stopped_runs": orch.stop(_own(orch, agent_id))}


@server.tool()
def remove_agent(agent_id: str, delete_branch: bool = False) -> dict[str, Any]:
    """Delete an agent and its worktree. The branch is kept unless delete_branch is true.

    Uncommitted work in the worktree is lost — check get_agent first.
    """
    orch = _orch()
    orch.remove(_own(orch, agent_id), delete_branch)
    return {"removed": agent_id}


def main() -> None:
    server.run("stdio")


if __name__ == "__main__":
    main()
