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
from .models import Run, running_total
from .service import Orchestrator, OrchestratorError, agent_state, result_file, run_usage

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


def clip(
    text: str, limit: int, offset: int = 0, run_id: int | None = None, paged: bool = True
) -> str:
    """`limit` chars of `text` from `offset` (limit <= 0: all), saying what was left out.

    The note names the get_agent call for the rest: with `run_id` that run's, and with
    `paged` the `result_offset` to continue from (errors are not paged).
    """
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
        call = f"get_agent run_id={run_id} " if run_id else ""
        page = f"{call}result_offset={end}; " if paged and run_id else call
        part += f"… [cut at {end} of {total} chars; {page}result_chars=0 for all]"
    return part


def _structured(data: Any, limit: int, offset: int = 0, run_id: int | None = None) -> Any:
    """Structured output as is when its JSON fits in `limit` and no offset is asked for,
    else a page of that JSON."""
    text = json.dumps(data, ensure_ascii=False)
    if offset <= 0 and (limit <= 0 or len(text) <= limit):
        return data
    return clip(text, limit, offset, run_id)


def _run_reply(run: Run, budget: int | None, answer: bool = True) -> tuple[dict[str, Any], int]:
    """A finished run for wait_for_agents: its error and its answer (structured output
    when it has one, else its text), together cut to `budget` chars of JSON (None: no
    limit).

    Returns the reply and the budget left. A text that gets no room at all is left out
    and the reply points to get_agent instead.
    """
    reply: dict[str, Any] = {"id": run.id, "status": run.status.value}
    left = budget
    texts: list[tuple[str, str, Any]] = []
    if run.error:
        texts.append(("error", run.error, None))
    if answer and run.structured is not None:
        texts.append(("structured", json.dumps(run.structured, ensure_ascii=False), run))
    elif answer and run.result is not None:
        texts.append(("result", run.result, None))
    for key, text, structured in texts:
        if left is not None and left <= 0:
            reply["more"] = f"get_agent run_id={run.id}"
            continue
        size = len(json.dumps(structured.structured)) if structured else 0
        if structured and (left is None or size <= left):
            reply[key] = structured.structured
            left = None if left is None else left - size
        elif left is None:
            reply[key] = text
        elif chars := _fitting(text, left):
            reply[key] = clip(text, chars, run_id=run.id, paged=key != "error")
            left -= _json_len(text[:chars])
        else:  # not even one char fits (clip would read a limit of 0 as "all")
            reply["more"] = f"get_agent run_id={run.id}"
            left = 0
    return reply, (0 if left is None else left)


def _json_len(text: str) -> int:
    """Length of `text` once quoted in a JSON reply, without the quotes (escaping non-ASCII
    too, the longer of the two ways to serialize it)."""
    return len(json.dumps(text)) - 2


def _fitting(text: str, budget: int) -> int:
    """How many leading chars of `text` take at most `budget` chars of JSON."""
    low, high = 0, len(text)
    while low < high:
        middle = (low + high + 1) // 2
        if _json_len(text[:middle]) <= budget:
            low = middle
        else:
            high = middle - 1
    return low


EARLIER_WITH_TEXT = 2  # undelivered runs before the latest that wait_for_agents shows
LISTED_RUNS = 20  # further undelivered runs it lists by id and status
LISTED_IDS = 200  # and beyond those, by id only


def _agent_runs(shown: Run | None, claimed: list[Run], limit: int) -> dict[str, Any]:
    """The run part of one agent's wait_for_agents reply, within one budget of `limit`
    chars (0: no limit) for all its texts, newest first.

    `last_run` is the latest finished run, with its answer only if this call claimed it.
    Up to EARLIER_WITH_TEXT earlier claimed runs share what is left (`earlier_runs`);
    the rest are only listed (`other_runs`) — they count as delivered all the same.
    """
    budget = None if limit <= 0 else limit
    parts: dict[str, Any] = {}
    last = None
    if shown:
        is_new = any(run.id == shown.id for run in claimed)
        last, left = _run_reply(shown, budget, answer=is_new)
        if not is_new:
            last["already_delivered"] = "get_agent result_chars=0 reads it again"
        budget = None if budget is None else left
    earlier = [run for run in claimed if not shown or run.id != shown.id]
    with_text, listed = [], []
    for run in reversed(earlier):
        if len(with_text) < EARLIER_WITH_TEXT and (budget is None or budget > 0):
            reply, left = _run_reply(run, budget)
            budget = None if budget is None else left
            with_text.append(reply)
        else:
            listed.append({"id": run.id, "status": run.status.value})
    if with_text:
        parts["earlier_runs"] = with_text[::-1]
    if listed:
        parts["other_runs"] = listed[:LISTED_RUNS][::-1]
        note = "results not shown; get_agent run_id=<id> reads one"
        if older := [run["id"] for run in listed[LISTED_RUNS:]]:
            parts["older_run_ids"] = older[:LISTED_IDS][::-1]
            note += "; older ones in older_run_ids"
            if len(older) > LISTED_IDS:
                note += f" (newest {LISTED_IDS}; {len(older) - LISTED_IDS} before not listed)"
        parts["other_runs_note"] = note
    return {**parts, "last_run": last}


def _delivery_floor(orch: Orchestrator, caller: str, agent_id: str) -> int:
    """The first run of an agent a wait_for_agents call may deliver to `caller`: the one
    after the last run it already got, or, for a caller that never got one, the oldest
    run active when the call started (else the latest finished one)."""
    last = orch.store.last_delivered(caller, agent_id)
    if last is not None:
        return last + 1
    _, active, shown = agent_state(orch.store.runs_of(agent_id))
    if active:
        return active[0].id
    return shown.id if shown else 0


def _wait(orch: Orchestrator, ids: list[str], timeout: float, limit: int) -> dict[str, Any]:
    """wait_for_agents in a worker thread: statuses until idle, then the undelivered results.

    Each agent's runs are read once and everything returned is derived from that read, so
    `finished` matches the statuses even when a run is queued after the idle check.
    """
    caller = os.environ.get("ORCH_AGENT_ID", "")
    floors = {agent_id: _delivery_floor(orch, caller, agent_id) for agent_id in ids}
    orch.wait_idle(ids, timeout)
    runs = {agent_id: orch.store.runs_of(agent_id) for agent_id in ids}
    states = {agent_id: agent_state(runs[agent_id]) for agent_id in ids}
    if any(active for _, active, _ in states.values()):
        agents = [
            {"id": agent_id, "status": status, "queued_runs": len(active)}
            for agent_id, (status, active, _) in states.items()
        ]
        return {"finished": False, "agents": agents}
    outcomes = []
    for agent_id in ids:
        status, _, shown = states[agent_id]
        new = [run for run in runs[agent_id] if run.id >= floors[agent_id]]
        claimed = orch.store.claim(caller, [run.id for run in new])
        totals = running_total(runs[agent_id], orch.store.usage_of(agent_id))
        outcome = {
            "id": agent_id,
            "status": status,
            "branch": orch.agent(agent_id).branch,
            "cost_usd": totals.cost or 0,
            "tokens": totals.tokens(),
        }
        claimed_runs = [run for run in new if run.id in claimed]
        outcomes.append({**outcome, **_agent_runs(shown, claimed_runs, limit)})
    return {"finished": True, "agents": outcomes}


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
    freeform: bool = False,
) -> dict[str, Any]:
    """Start a new agent on a task. Returns immediately; use wait_for_agents for the outcome.

    task: the full, self-contained brief. The agent knows nothing else. It may embed
      `{{result:<agent-id>}}`, replaced by that agent's final answer when this agent starts
      (combine with `after`); text is cut at 4000 chars, `{{result:<agent-id>:full}}` is not.
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
      Default: the role's compact schema, if it has one; `freeform` for a prose answer.
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
        role_schema=not freeform,
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
    freeform: bool = False,
) -> dict[str, Any]:
    """Send a follow-up to an existing agent; it continues its conversation with full memory.

    Messages to a busy agent queue up and run in order. Returns immediately.
    `output_schema`/`freeform` as in spawn_agent.
    """
    orch = _orch()
    run = orch.send(_own(orch, agent_id), message, after, output_schema, not freeform)
    return _queued(orch, agent_id, run.id)


@server.tool()
async def wait_for_agents(
    agent_ids: list[str], timeout_seconds: int = 600, result_chars: int = RESULT_CHARS
) -> dict[str, Any]:
    """Block until all the given agents are idle, then return their new results.

    If `finished` is false not all are idle yet: only statuses; call again. Each result
    is returned once per caller, all texts of an agent together cut at `result_chars`
    (0: no limit); later calls mark it `already_delivered`.
    """
    orch = _orch()
    ids = [_own(orch, agent_id) for agent_id in agent_ids]
    timeout = max(1, min(timeout_seconds, MAX_WAIT_SECONDS))
    return await anyio.to_thread.run_sync(lambda: _wait(orch, ids, timeout, result_chars))


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
    run_id: int | None = None,
    max_commits: int = MAX_COMMITS,
    max_status: int = MAX_STATUS,
) -> dict[str, Any]:
    """Full status of one agent: last result (`result_file`: all of it on disk), the
    commits/changes in its worktree, the explicit model/effort overrides, and tokens,
    cost and model of its last 10 runs.

    Result and error are cut to `result_chars` chars (0: all), the result from
    `result_offset`; `run_id` shows that run as `run` instead of `last_run`. Git output
    is cut to `max_commits` commit and `max_status` status lines (0: all).
    """
    orch = _orch()
    orch.store.reconcile()
    agent = orch.agent(_own(orch, agent_id))
    view = orch.view(agent, detail=True)
    key = "last_run"
    if run_id is not None:
        run = orch.store.get_run(run_id)
        if run is None or run.agent_id != agent.id:
            raise OrchestratorError(f"agent '{agent.id}' has no run {run_id}")
        del view["last_run"]
        key, view["run"] = (
            "run",
            {
                "id": run.id,
                "status": run.status.value,
                "result": run.result,
                "structured": run.structured,
                "error": run.error,
                "started_at": run.started_at,
                "ended_at": run.ended_at,
                "usage": run_usage(orch.store.get_usage(run.id)),
                "result_file": result_file(run.id),
            },
        )
    shown = view[key]
    if shown and shown["result"] is not None:
        shown["result"] = clip(shown["result"], result_chars, result_offset, shown["id"])
    if shown and shown["structured"] is not None:
        shown["structured"] = _structured(
            shown["structured"], result_chars, result_offset, shown["id"]
        )
    if shown and shown["error"]:
        shown["error"] = clip(shown["error"], result_chars)
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
    events = orch.store.events(agent_id, tail=tail)
    return _cap_log([_log_line(event, event_chars) for event in events], max_chars)


def _log_line(event: Any, limit: int) -> str:
    text = event.text
    if 0 < limit < len(text):
        text = f"{text[:limit]}… [+{len(text) - limit} chars]"
    return f"#{event.id} {event.ts} run {event.run_id} [{event.kind}] {text}"


def _cost(line: str) -> int:
    """What a line adds to the JSON list sent back: the quoted, escaped string and its
    separator (escaping non-ASCII too, the longer of the two ways to serialize it)."""
    return len(json.dumps(line)) + 2


def _cap_log(lines: list[str], max_chars: int) -> list[str]:
    """The newest lines whose JSON list is at most `max_chars` long (<= 0: no limit),
    with a note on the older ones left out; the newest line itself is cut if it alone
    is too long."""
    if max_chars <= 0 or len(json.dumps(lines)) <= max_chars:
        return lines

    def note(omitted: int) -> str:
        return f"[{omitted} older events omitted; raise max_chars]"

    budget = max_chars - _cost(note(len(lines)))  # a list of n items is sum(cost) long
    kept: list[str] = []
    for line in reversed(lines):
        if _cost(line) > budget:
            break
        kept.append(line)
        budget -= _cost(line)
    if not kept and (cut := _cut(lines[-1], budget)):
        kept.append(cut)
    reply = kept[::-1]
    if omitted := len(lines) - len(kept):
        reply.insert(0, note(omitted))
    while reply and len(json.dumps(reply)) > max_chars:
        reply.pop(0)  # only when max_chars is too small for even the note
    return reply


def _cut(line: str, budget: int) -> str | None:
    """The longest start of `line` plus a marker whose cost fits in `budget`, if any."""
    marker = "… [cut; event_id gives it in full]"
    low, high = 0, len(line)  # escapes make the cost of a start uneven: search for it
    while low < high:
        middle = (low + high + 1) // 2
        if _cost(line[:middle] + marker) <= budget:
            low = middle
        else:
            high = middle - 1
    return line[:low] + marker if low and _cost(line[:low] + marker) <= budget else None


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
