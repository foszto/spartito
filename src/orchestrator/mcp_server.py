"""MCP server (stdio) that exposes the orchestrator to an agent.

The top-level orchestrator session and every `lead` agent get this server. An agent's own
identity arrives in ORCH_AGENT_ID: agents it spawns become its children, and it is confined
to its own project. The top-level orchestrator has no project (or no ORCH_AGENT_ID at all,
for the interactive `orch chat` session) and may act everywhere.
"""

from __future__ import annotations

import json
import os
from typing import Any

import anyio
from mcp.server.mcpserver import MCPServer

from . import roles
from .service import Orchestrator, OrchestratorError, agent_state

MAX_WAIT_SECONDS = 1800
RESULT_CHARS = 4000  # result text (or structured output) per agent in one reply
EVENT_CHARS = 300  # text of one agent_log line
LOG_CHARS = 6000  # a whole agent_log reply
MAX_COMMITS = 20
MAX_STATUS = 30

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


def clip(text: str, limit: int, offset: int = 0) -> str:
    """`limit` chars of `text` from `offset` (limit <= 0: all), saying what was left out."""
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
        part += (
            f"… [truncated at {end} of {total} chars; rest: get_agent with"
            f" result_offset={end}, or result_chars=0 for all]"
        )
    return part


def _structured(data: Any, limit: int, offset: int = 0) -> Any:
    """Structured output as is when its JSON fits in `limit`, else that JSON clipped."""
    text = json.dumps(data, ensure_ascii=False)
    return data if limit <= 0 or len(text) <= limit else clip(text, limit, offset)


def _answer(last_run: dict[str, Any], limit: int) -> dict[str, Any]:
    """A finished run's answer: its structured output when it has one, else its text."""
    if last_run["structured"] is not None:
        return {"structured": _structured(last_run["structured"], limit)}
    if last_run["result"] is not None:
        return {"result": clip(last_run["result"], limit)}
    return {}


def _outcomes(orch: Orchestrator, agent_ids: list[str], limit: int) -> list[dict[str, Any]]:
    """Finished agents for wait_for_agents: each latest result once per caller."""
    caller = os.environ.get("ORCH_AGENT_ID", "")
    views = [orch.view(orch.agent(agent_id)) for agent_id in agent_ids]
    runs = [view["last_run"]["id"] for view in views if view["last_run"]]
    delivered = orch.store.delivered(caller, runs)
    outcomes = []
    for view in views:
        outcome = {key: view[key] for key in ("id", "status", "branch", "cost_usd", "tokens")}
        last = view["last_run"]
        if last:
            run = {"id": last["id"], "status": last["status"]}
            if last["error"]:
                run["error"] = clip(last["error"], limit)
            if last["id"] in delivered:
                run["already_delivered"] = "get_agent reads it again"
            else:
                run.update(_answer(last, limit))
            last = run
        outcomes.append({**outcome, "last_run": last})
    orch.store.mark_delivered(caller, [run for run in runs if run not in delivered])
    return outcomes


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
async def wait_for_agents(
    agent_ids: list[str], timeout_seconds: int = 600, result_chars: int = RESULT_CHARS
) -> dict[str, Any]:
    """Block until all the given agents are idle, then return each one's latest result.

    If `finished` is false the timeout elapsed first: only statuses; call again to keep
    waiting. A result is returned once per caller, cut at `result_chars` (0: no limit);
    later calls mark it `already_delivered`. get_agent reads any result again in full.
    """
    orch = _orch()
    ids = [_own(orch, agent_id) for agent_id in agent_ids]
    timeout = max(1, min(timeout_seconds, MAX_WAIT_SECONDS))
    finished = await anyio.to_thread.run_sync(lambda: orch.wait_idle(ids, timeout))
    if not finished:
        states = [(agent_id, agent_state(orch.store.runs_of(agent_id))) for agent_id in ids]
        agents = [
            {"id": agent_id, "status": status, "queued_runs": len(active)}
            for agent_id, (status, active, _) in states
        ]
        return {"finished": False, "agents": agents}
    outcomes = await anyio.to_thread.run_sync(lambda: _outcomes(orch, ids, result_chars))
    return {"finished": True, "agents": outcomes}


@server.tool()
def list_agents(project: str | None = None) -> list[dict[str, Any]]:
    """List agents with status, hierarchy (parent_id), branch and a short last result.

    `model`/`effort` are what the agent's next turn runs with; `cost_usd` and `tokens`
    are the totals of its runs.
    """
    orch = _orch()
    scope = _project(orch, project) if project or _scoped(orch) else None
    return orch.overview(scope)


@server.tool()
def get_agent(
    agent_id: str,
    result_chars: int = RESULT_CHARS,
    result_offset: int = 0,
    max_commits: int = MAX_COMMITS,
    max_status: int = MAX_STATUS,
) -> dict[str, Any]:
    """Full status of one agent: last result, the commits/changes in its worktree, the
    explicit model/effort overrides, and tokens, cost and model of its last 10 runs.

    The result is cut to `result_chars` chars from `result_offset` (result_chars=0: all);
    the git output to `max_commits` commit and `max_status` status lines (0: all).
    """
    orch = _orch()
    orch.store.reconcile()
    agent = orch.agent(_own(orch, agent_id))
    view = orch.view(agent, detail=True)
    last = view["last_run"]
    if last and last["result"] is not None:
        last["result"] = clip(last["result"], result_chars, result_offset)
    if last and last["structured"] is not None:
        last["structured"] = _structured(last["structured"], result_chars, result_offset)
    return {
        **view,
        "changes": orch.changes(agent_id, max_commits, max_status),
        "recent_runs": orch.run_history(agent, limit=10),
    }


@server.tool()
def agent_log(
    agent_id: str,
    tail: int = 40,
    event_chars: int = EVENT_CHARS,
    max_chars: int = LOG_CHARS,
    event_id: int | None = None,
) -> list[str]:
    """Recent activity of an agent (messages, tool calls, errors), oldest first, one line
    per event: `#<event id> <time> run <run id> [<kind>] <text>`.

    Each text is cut at `event_chars` and the reply at `max_chars`, keeping the newest
    events (0: no limit). `event_id` returns that one event's full text.
    """
    orch = _orch()
    _own(orch, agent_id)
    if event_id is not None:
        event = orch.store.get_event(event_id)
        if event is None or event.agent_id != agent_id:
            raise OrchestratorError(f"agent '{agent_id}' has no event {event_id}")
        return [_log_line(event, 0)]
    lines = [_log_line(event, event_chars) for event in orch.store.events(agent_id, tail=tail)]
    if max_chars <= 0:
        return lines
    kept: list[str] = []
    budget = max_chars - 60  # room for the note on omitted events
    for line in reversed(lines):
        if kept and len(line) > budget:
            break
        kept.append(line)
        budget -= len(line)
    if omitted := len(lines) - len(kept):
        kept.append(f"[{omitted} older events omitted; raise max_chars to see them]")
    return kept[::-1]


def _log_line(event: Any, limit: int) -> str:
    text = event.text
    if 0 < limit < len(text):
        text = f"{text[:limit]}… [+{len(text) - limit} chars]"
    return f"#{event.id} {event.ts} run {event.run_id} [{event.kind}] {text}"


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
