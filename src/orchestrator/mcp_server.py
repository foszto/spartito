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

from . import board, roles
from .core.access import Caller
from .core.delivery import delivery_floor as _delivery_floor  # noqa: F401 (old import path)
from .core.delivery import wait_for_agents as _wait  # noqa: F401 (old import path)
from .replies import clip, structured_page
from .service import ORCHESTRATOR_ID, Orchestrator, OrchestratorError, result_file, run_usage

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


def _queued(
    orch: Orchestrator, agent_id: str, run_id: int, notify: str | None = None
) -> dict[str, Any]:
    """The reply to a dispatch, led by `next`: what happens now. The caller is woken only
    when it is an agent and the target's parent; the interactive session never is."""
    agent = orch.agent(agent_id)
    view = orch.view(agent)
    keys = ("id", "project", "role", "provider", "status", "branch", "cwd")
    caller = Caller.from_env(orch).id
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


def _lean(name: str, *fields: str) -> None:
    """Drop the title and the default of new optional parameters from a tool's listed input
    schema, so that adding one costs every agent only `"field": {"type": …}`: not listed in
    "required", it stays optional. Validation uses the function's model, not this."""
    properties = server._tool_manager.get_tool(name).parameters["properties"]
    for field in fields:
        properties[field].pop("title", None)
        properties[field].pop("default", None)


@server.tool()
def list_projects() -> list[dict[str, Any]]:
    """List registered projects with the number of agents in each (archived ones not)."""
    orch = _orch()
    return [
        {
            **vars(project),
            "agents": orch.agent_count(project.name),
        }
        for project in Caller.from_env(orch).projects()
    ]


@server.tool()
def add_project(
    path: str, name: str | None = None, default_provider: str = "claude"
) -> dict[str, Any]:
    """Register a git repository as a project. `path` may be any directory inside the repo."""
    orch = _orch()
    if Caller.from_env(orch).scoped():
        raise OrchestratorError("only the top-level orchestrator registers projects")
    project = orch.add_project(path, name, default_provider)
    return {**vars(project), "note": orch.project_note(project)}


@server.tool()
def list_roles(project: str | None = None) -> list[dict[str, Any]]:
    """List the roles an agent can be spawned with (worker, reviewer, planner, ...)."""
    orch = _orch()
    access = Caller.from_env(orch)
    path = orch.project(access.project(project)).path if project or access.scoped() else None
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
    access = Caller.from_env(orch)
    caller = access.agent()
    agent, run = orch.spawn(
        access.project(project),
        task,
        name=name,
        role=role,
        provider=provider,
        model=model,
        effort=effort,
        worktree=worktree,
        share_with=share_worktree_with and access.own(share_worktree_with),
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
    access = Caller.from_env(orch)
    if access.scoped():
        raise OrchestratorError("only the top-level orchestrator talks to project leads")
    lead = orch.lead(project, adopt=access.sender())
    run = orch.send(lead.id, message, sender=access.sender(), notify=notify)
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
    access = Caller.from_env(orch)
    run = orch.send(
        access.own(agent_id),
        message,
        after,
        output_schema,
        not freeform,
        sender=access.sender(),
        notify=notify,
    )
    return _queued(orch, agent_id, run.id, notify)


@server.tool()
def withdraw_message(run_id: int, replacement: str | None = None) -> dict[str, Any]:
    """Withdraw a message you sent (its `run_id`) that has not started, or with
    `replacement` replace its text. Refused once it has started."""
    orch = _orch()
    sender = Caller.from_env(orch).acts_as()  # the interactive session may act on any run
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
    access = Caller.from_env(orch)
    ids = [access.own(agent_id) for agent_id in agent_ids]
    if caller := access.agent():
        above = orch.lineage(caller.id)
        if inside := [agent_id for agent_id in ids if agent_id in above]:
            raise OrchestratorError(
                f"cannot wait for {', '.join(inside)}: yourself or an agent above you"
                " (it waits for you); end your turn instead"
            )
    timeout = max(1, min(timeout_seconds, MAX_WAIT_SECONDS))
    return await anyio.to_thread.run_sync(lambda: orch.wait_for_agents(ids, timeout, result_chars))


@server.tool()
def list_agents(project: str | None = None, archived: bool = False) -> list[dict[str, Any]]:
    """List agents with status, hierarchy (parent_id), branch and a short last result.

    `model`/`effort` are what the agent's next turn runs with; `cost_usd` and `tokens`
    are the totals of its runs. `archived`: only the archived agents instead.
    """
    orch = _orch()
    access = Caller.from_env(orch)
    scope = access.project(project) if project or access.scoped() else None
    return orch.overview(scope, archived=archived)


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
    orch.reconcile()
    agent = orch.agent(Caller.from_env(orch).own(agent_id))
    view = orch.view(agent, detail=True)
    key = "last_run"
    if run_id is not None:
        run = orch.get_run(run_id)
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
                "usage": run_usage(orch.get_usage(run.id)),
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
    Caller.from_env(orch).own(agent_id)
    if event_id is not None:
        event = orch.get_event(event_id)
        if event is None or event.agent_id != agent_id:
            raise OrchestratorError(f"agent '{agent_id}' has no event {event_id}")
        return [_log_line(event, 0)]
    events = orch.events(agent_id, tail=tail)
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
    access = Caller.from_env(orch)
    stopped = orch.stop_runs(access.own(agent_id))
    # The caller knows these ended: no notification and no wait_for_agents repeats them.
    orch.mark_delivered(access.id, stopped)
    return {"stopped_runs": len(stopped)}


@server.tool()
def archive_agent(agent_id: str, confirm: bool = False, restore: bool = False) -> dict[str, Any]:
    """Archive an agent you no longer need: hidden from list_agents, all kept (branch,
    worktree, log). Unmerged or uncommitted work is refused unless `confirm`. `restore`
    brings one back; so does a message to it."""
    orch = _orch()
    agent_id = Caller.from_env(orch).own(agent_id)
    return orch.restore(agent_id) if restore else orch.archive(agent_id, confirm)


DECISION_NEXT = (
    "the owner's answer arrives as a user message; continue with whatever does not depend"
    " on it (withdraw_decision if it is no longer needed)"
)


@server.tool()
def request_decision(
    question: str, options: list[str] | None = None, recommendation: str | None = None
) -> dict[str, Any]:
    """Ask the owner for a decision only they can make; it waits in their decisions list.
    `question` at most 500 chars; `options` up to 6 short choices (they may also answer
    freely); `recommendation`: what you would choose and why, in a sentence."""
    orch = _orch()
    caller = Caller.from_env(orch).agent()
    if caller is None:
        raise OrchestratorError("this session talks to the user directly: ask them here")
    decision = orch.request_decision(caller.id, question, options, recommendation)
    return {"decision_id": decision.id, "next": DECISION_NEXT}


@server.tool()
def withdraw_decision(decision_id: int) -> dict[str, Any]:
    """Withdraw one of your open decisions that is no longer needed."""
    orch = _orch()
    caller = Caller.from_env(orch).id
    return {"withdrawn": orch.withdraw_decision(decision_id, caller).id}


# list_board: the whole reply within LOG_CHARS; the active work first.
BOARD_ORDER = (board.IN_PROGRESS, board.READY, board.IDEA, board.DONE)
BOARD_CLIPS = {"title": 100, "description": 160, "agent_id": 80, "branch": 80}


def _short(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _actor(access: Caller) -> board.Actor:
    """The calling agent as the one who changes the board (no id: the `orch chat` session)."""
    return board.Actor(board.AGENT_KIND, access.id or None)


def _board_line(item: Any) -> dict[str, Any]:
    line: dict[str, Any] = {"id": item.id, "state": item.state}
    for key, limit in BOARD_CLIPS.items():
        if value := getattr(item, key):
            line[key] = _short(value, limit)
    return line


def _live_line(live: dict[str, Any]) -> dict[str, Any]:
    """What `live` adds to an item's line: `run` (its agent's status) and `git`, if known."""
    line: dict[str, Any] = {}
    if live["agent"]:
        line["run"] = live["agent"]["status"]
    if live["git"]:
        line["git"] = board.git_text(live["git"])
    return line


@server.tool()
def list_board(project: str, state: str | None = None, live: bool = False) -> dict[str, Any]:
    """The project's task board (ideas and tasks kept across sessions): in_progress, ready,
    idea, then done, each in its order; `state` lists one state only. Short: titles and
    descriptions are cut, and what does not fit in about 6000 chars is only counted
    (`counts` has every state's total). Leads: their own project only. `live` adds agent
    and git status."""
    orch = _orch()
    name = Caller.from_env(orch).project(project)
    if live:
        orch.board_sync()
    items = orch.board_items(name, state)
    lives = orch.board_live(items) if live else {}
    items.sort(key=lambda item: BOARD_ORDER.index(item.state))  # stable: keeps the order
    counts = {key: n for key in BOARD_ORDER if (n := sum(i.state == key for i in items))}
    reply: dict[str, Any] = {"project": name, "counts": counts, "items": []}
    left = LOG_CHARS - len(json.dumps(reply)) - 200  # room for the note
    for item in items:
        line = _board_line(item)
        if live:
            line.update(_live_line(lives[item.id]))
        left -= len(json.dumps(line)) + 2
        if left < 0:
            break
        reply["items"].append(line)
    hidden = items[len(reply["items"]) :]
    if hidden:
        by_state = {key: sum(i.state == key for i in hidden) for key in BOARD_ORDER}
        where = ", ".join(f"{key} {n}" for key, n in by_state.items() if n)
        reply["note"] = f"{len(hidden)} not shown ({where}); state= lists one state"
    return reply


_lean("list_board", "live")


@server.tool()
def add_board_item(
    project: str, title: str, description: str = "", state: str = "idea"
) -> dict[str, Any]:
    """Put an idea or task on the project's board (at the end of its state). `title` at most
    200 chars, `description` at most 4000; `state`: idea (default), ready, in_progress, done.
    Leads: their own project only."""
    orch = _orch()
    access = Caller.from_env(orch)
    item = orch.add_board_item(
        access.project(project), title, description, state, actor=_actor(access)
    )
    return {"id": item.id, "project": item.project, "state": item.state}


@server.tool()
def update_board_item(
    item_id: int,
    state: str | None = None,
    title: str | None = None,
    description: str | None = None,
    agent: str | None = None,
    branch: str | None = None,
) -> dict[str, Any]:
    """Change a board item: its state (idea, ready, in_progress, done; it goes to the end
    of that state), title, description, or the agent id / branch working on it ("" clears
    one). Leads: items of their own project only."""
    orch = _orch()
    access = Caller.from_env(orch)
    access.project(orch.board_item(item_id).project)  # a lead stays within its project
    given = {
        "state": state,
        "title": title,
        "description": description,
        "agent_id": agent,
        "branch": branch,
    }
    changes = {key: value for key, value in given.items() if value is not None}
    if not changes:
        raise OrchestratorError(
            "nothing to change: pass state, title, description, agent or branch"
        )
    return _board_line(orch.update_board_item(item_id, changes, _actor(access)))


# A tuple and no output schema: the shortest listing (every orchestrator turn pays for it).
@server.tool(structured_output=False)
def dispatch_board(project: str, items: tuple[int, ...] = ()) -> dict[str, Any]:
    """Send board items to the project's lead as one message (they become in_progress).
    `items`: ids, idea or ready; default: all ready."""
    orch = _orch()
    access = Caller.from_env(orch)
    if access.scoped():
        raise OrchestratorError("only the top-level orchestrator dispatches board items")
    actor = _actor(access)
    if not items:
        result = orch.dispatch_board(project, actor=actor)
        sent = {"items": result["items"], "agent_id": result["agent_id"]}
        run_id, skipped = result["run_id"], []
    else:
        result = orch.dispatch_items(list(items), project, actor)
        sent = result["results"][0] if result["results"] else {"items": [], "agent_id": None}
        run_id, skipped = sent.get("run_id"), result["skipped"]
    reply: dict[str, Any] = {"sent": [item["id"] for item in sent["items"]]}
    if skipped:
        reply["skipped"] = {str(s["id"]): s["reason"] for s in skipped}
    return {**reply, "lead": sent["agent_id"], "run_id": run_id}


def _without_titles(name: str) -> None:
    """Drop the titles pydantic writes into a tool's input schema (they repeat the names):
    the listing is sent in every turn. Validation uses the function's model, not this."""
    schema = server._tool_manager.get_tool(name).parameters
    schema.pop("title", None)
    for field in schema.get("properties", {}).values():
        field.pop("title", None)


_without_titles("dispatch_board")


# Tools only the top-level orchestrator (and the interactive session) sees: every other
# agent pays for each tool's description in every turn.
ORCHESTRATOR_TOOLS = ("dispatch_board",)


def main() -> None:
    caller = os.environ.get("ORCH_AGENT_ID")
    if caller and caller != ORCHESTRATOR_ID:
        for name in ORCHESTRATOR_TOOLS:
            server.remove_tool(name)
    server.run("stdio")


if __name__ == "__main__":
    main()
