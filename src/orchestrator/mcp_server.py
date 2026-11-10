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
from .replies import clip, run_reply, structured_page
from .service import Orchestrator, OrchestratorError, agent_state, result_file, run_usage
from .store import ANYONE

MAX_WAIT_SECONDS = 1800
WAIT_SECONDS = 120  # default wait_for_agents timeout
RESULT_CHARS = 4000  # result text (or structured output) per agent in one reply
EVENT_CHARS = 300  # text of one agent_log line
LOG_CHARS = 6000  # a whole agent_log reply
MAX_COMMITS = 20
MAX_STATUS = 30

server = MCPServer(
    "orch",
    instructions=(
        "Tools to run coding agents (Claude Code, Codex) in git worktrees. Flow: "
        "spawn_agent / send_message (return at once) -> end your turn with a short status -> "
        "an automatic `[orch notification]` turn brings the results when your agents are "
        "done. Use `after` to chain agents that depend on each other."
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


def _queued(
    orch: Orchestrator, agent_id: str, run_id: int, notify: str | None = None
) -> dict[str, Any]:
    """The reply to a dispatch, led by `next`: what happens now. The caller is woken only
    when it is an agent and the target's parent; the interactive session never is."""
    agent = orch.agent(agent_id)
    view = orch.view(agent)
    keys = ("id", "project", "role", "provider", "status", "branch", "cwd")
    caller = os.environ.get("ORCH_AGENT_ID")
    woken = notify and caller and agent.parent_id == caller
    return {
        "next": NEXT[notify] if woken else NEXT_UNNOTIFIED,
        **{key: view[key] for key in keys},
        "run_id": run_id,
    }


NEXT = {
    "each": "end your turn; an [orch notification] wakes you when this agent is done",
    "all": "end your turn; an [orch notification] wakes you when all your agents are done",
}
NEXT_UNNOTIFIED = "nothing will wake you: call wait_for_agents"


def _sender() -> str | None:
    return os.environ.get("ORCH_AGENT_ID") or None


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
        last, left = run_reply(shown, budget, answer=is_new)
        if not is_new:
            last["already_delivered"] = "get_agent result_chars=0 reads it again"
        budget = None if budget is None else left
    earlier = [run for run in claimed if not shown or run.id != shown.id]
    with_text, listed = [], []
    for run in reversed(earlier):
        if len(with_text) < EARLIER_WITH_TEXT and (budget is None or budget > 0):
            reply, left = run_reply(run, budget)
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

    def message_waiting() -> bool:
        return bool(caller and orch.store.ready_messages(caller))

    if message_waiting():
        return _statuses(orch, ids, {aid: orch.store.runs_of(aid) for aid in ids}, MESSAGE_NOTE)
    floors = {agent_id: _delivery_floor(orch, caller, agent_id) for agent_id in ids}
    orch.wait_idle(ids, timeout, interrupt=message_waiting)
    runs = {agent_id: orch.store.runs_of(agent_id) for agent_id in ids}
    if message_waiting():
        return _statuses(orch, ids, runs, MESSAGE_NOTE)
    states = {agent_id: agent_state(runs[agent_id]) for agent_id in ids}
    below = {agent_id: orch.store.active_descendants(agent_id) for agent_id in ids}
    if any(active for _, active, _ in states.values()) or any(below.values()):
        return _statuses(orch, ids, runs)
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


def _lineage(orch: Orchestrator, agent_id: str) -> set[str]:
    """The agent and its ancestors."""
    seen: set[str] = set()
    current = orch.store.get_agent(agent_id)
    while current and current.id not in seen:
        seen.add(current.id)
        current = orch.store.get_agent(current.parent_id) if current.parent_id else None
    return seen


MESSAGE_NOTE = "a message for you is waiting: end this turn now with a short status"


def _statuses(
    orch: Orchestrator, ids: list[str], runs: dict[str, list[Run]], note: str | None = None
) -> dict[str, Any]:
    """The reply of an unfinished wait: statuses only, nothing counts as delivered."""
    agents = []
    for agent_id in ids:
        status, active, _ = agent_state(runs[agent_id])
        agents.append(
            {
                "id": agent_id,
                "status": status,
                "queued_runs": len(active),
                "children_running": orch.store.active_descendants(agent_id),
            }
        )
    reply: dict[str, Any] = {"finished": False, "agents": agents}
    if note:
        reply["note"] = note
    return reply


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
    notify: str = "each",
) -> dict[str, Any]:
    """Start a new agent on a task. Returns immediately; end your turn, you are notified.

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
    notify: `each` wakes you when this agent is done, `all` once every agent you sent work
      to is. The reply's `next` tells you whether you will be woken.
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
        sender=caller.id if caller else None,
        notify=notify,
    )
    return _queued(orch, agent.id, run.id, notify)


@server.tool()
def ask_project_lead(project: str, message: str, notify: str = "each") -> dict[str, Any]:
    """Send a message to a project's lead agent, creating it on first use.

    The lead works in the project's main checkout, keeps one long conversation about the
    project and can plan work and run its own worker agents. Returns immediately.
    `notify` as in spawn_agent.
    """
    orch = _orch()
    if _scoped(orch):
        raise OrchestratorError("only the top-level orchestrator talks to project leads")
    lead = orch.lead(project, adopt=_sender())
    run = orch.send(lead.id, message, sender=_sender(), notify=notify)
    return _queued(orch, lead.id, run.id, notify)


@server.tool()
def send_message(
    agent_id: str,
    message: str,
    after: list[str] | None = None,
    output_schema: dict[str, Any] | None = None,
    freeform: bool = False,
    notify: str = "each",
) -> dict[str, Any]:
    """Send a follow-up to an existing agent; it continues its conversation with full memory.

    Messages to a busy agent queue up and run in order. Returns immediately.
    `output_schema`/`freeform`/`notify` as in spawn_agent.
    """
    orch = _orch()
    run = orch.send(
        _own(orch, agent_id),
        message,
        after,
        output_schema,
        not freeform,
        sender=_sender(),
        notify=notify,
    )
    return _queued(orch, agent_id, run.id, notify)


@server.tool()
def withdraw_message(run_id: int, replacement: str | None = None) -> dict[str, Any]:
    """Withdraw a message you sent (its `run_id`) that has not started, or with
    `replacement` replace its text. Refused once it has started."""
    orch = _orch()
    sender = _sender() or ANYONE  # the interactive session may act on any run
    if replacement is None:
        run = orch.withdraw(run_id, sender)
        return {"withdrawn": run.id, "agent_id": run.agent_id}
    run = orch.replace_message(run_id, replacement, sender)
    return {"replaced": run.id, "agent_id": run.agent_id}


@server.tool()
async def wait_for_agents(
    agent_ids: list[str], timeout_seconds: int = WAIT_SECONDS, result_chars: int = RESULT_CHARS
) -> dict[str, Any]:
    """Block until the agents and their sub-agents are idle, then return their new results.

    Only for a result needed in this turn within minutes; otherwise end the turn and get
    notified. `finished: false`: statuses only (timeout, or a message waits for you: then
    end the turn). Each result comes once per caller (notifications count; repeats are
    `already_delivered`), an agent's texts cut at `result_chars` (0: no limit).
    """
    orch = _orch()
    ids = [_own(orch, agent_id) for agent_id in agent_ids]
    if caller := _caller(orch):
        above = _lineage(orch, caller.id)
        if inside := [agent_id for agent_id in ids if agent_id in above]:
            raise OrchestratorError(
                f"cannot wait for {', '.join(inside)}: yourself or an agent above you"
                " (it waits for you); end your turn instead"
            )
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

    The answer is `structured` when the run has it, else `result`; it and the error are
    cut to `result_chars` chars (0: all), the answer from `result_offset`; `run_id` shows that run as `run` instead of `last_run`. Git output
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
    if shown and shown["structured"] is not None:  # the answer once, as wait_for_agents
        del shown["result"]
        shown["structured"] = structured_page(
            shown["structured"], result_chars, result_offset, shown["id"]
        )
    elif shown:
        del shown["structured"]
        if shown["result"] is not None:
            shown["result"] = clip(shown["result"], result_chars, result_offset, shown["id"])
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
    stopped = orch.stop_runs(_own(orch, agent_id))
    # The caller knows these ended: no notification and no wait_for_agents repeats them.
    orch.store.claim(os.environ.get("ORCH_AGENT_ID", ""), stopped)
    return {"stopped_runs": len(stopped)}


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
