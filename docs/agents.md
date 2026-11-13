# Agents in detail

- [MCP tools](#mcp-tools)
- [Turns and the queue](#turns-and-the-queue)
- [Dependencies and results](#dependencies-and-results)
- [Notifications](#notifications)
- [Task board](#task-board)
- [Decisions](#decisions)
- [Archiving](#archiving)
- [Context and compaction](#context-and-compaction)
- [Tokens and cost](#tokens-and-cost)

A *turn* is one prompt and the agent's answer, stored as a run with a run id. A *lead role* is
a role with `lead: true`: the orchestrator and the project leads.

## MCP tools

Lead roles get the `orch` MCP server. The CLI of each lead agent, and `spartito chat`, start it
as `<python> -m orchestrator.mcp_server` with the tool's own Python, never through the command
(`spartito mcp` runs the same server by hand; there is no need to). An agent's own id reaches the server in
`ORCH_AGENT_ID`: agents it starts become its children, and a project lead is confined to its
own project. The orchestrator (and the interactive `spartito chat` session, which has no agent id)
can act on every project.

| tool | purpose |
|---|---|
| `list_projects()` | projects with their number of agents |
| `add_project(path, name, default_provider)` | register a repository (orchestrator only) |
| `list_roles(project)` | roles available, with `lead` and `worktree` flags |
| `spawn_agent(task, project, name, role, provider, model, effort, worktree, share_worktree_with, base, after, output_schema, freeform, notify)` | start an agent; returns at once |
| `ask_project_lead(project, message, notify)` | message a project's lead, creating it (orchestrator only) |
| `send_message(agent_id, message, after, output_schema, freeform, notify)` | follow-up to an agent; queued if busy |
| `withdraw_message(run_id, replacement)` | withdraw a message you sent that has not started, or replace its text |
| `wait_for_agents(agent_ids, timeout_seconds, result_chars)` | block until the agents and their sub-agents are idle (default 120 s, at most 1800 s) |
| `list_agents(project, archived)` | agents with status, parent, branch, model/effort, context, `cost_usd`, short `tokens` |
| `get_agent(agent_id, result_chars, result_offset, run_id, max_commits, max_status)` | one agent in detail: result, commits, changes, overrides, queue, usage of the last 10 turns |
| `agent_log(agent_id, tail, event_chars, max_chars, event_id)` | recent events, one line each |
| `stop_agent(agent_id)` | stop running and queued turns |
| `archive_agent(agent_id, confirm, restore)` | archive (unmerged work only with `confirm`), or restore |
| `request_decision(question, options, recommendation, item)` | ask the owner (headless lead roles only); `item`: the board item it blocks |
| `withdraw_decision(decision_id)` | close one of your own open decisions |
| `list_board(project, state, live)` | the project's task board, shortened to fit; `live` adds agent and git status |
| `add_board_item(project, title, description, state)` | add an item |
| `update_board_item(item_id, state, title, description, agent, branch)` | change an item |
| `dispatch_board(project, items)` | send board items to the project's lead (default: all `ready`; orchestrator only) |

Replies are kept short: results and logs are cut to a budget, and say how to read the rest
(`result_offset`, `event_id`, the `result_file` path in `get_agent`).

## Turns and the queue

- A message continues the agent's session (`claude --resume`, `codex exec resume`); the agent
  keeps its conversation.
- An agent runs one turn at a time. Messages run in the order they were sent; a waiting
  notification turn comes after the messages that are ready to run.
- `get_agent` shows the waiting turns in `queue`; every view shows the number of working
  descendants in `children_running`. `spartito status` lists the queue with run ids.
- A queued message can be edited or withdrawn until it starts (`spartito edit`, `spartito withdraw`,
  `withdraw_message`, the dashboard's **Queued** list). Through `withdraw_message` an agent
  can change only the messages it sent itself.

## Dependencies and results

- **`after`** (`--after`): the turn starts only after the given agents (or turns, by run id) have
  finished successfully. If one fails, the turn is `skipped`.
- **`{{result:<agent-id>}}`** in a task or message is replaced, when the turn starts, with
  that agent's last successful answer (not a compaction). A structured answer is inserted
  whole as compact JSON; a text answer is cut at 4000 characters, followed by a line with the
  full length and the path of the result file. `{{result:<agent-id>:full}}` inserts the full
  text. A reference to an unknown agent or one without a finished answer is left as it is.
- **`base`** (`--base`): the new worktree branches from that ref or agent's branch.
- **`share_worktree_with`** (`--share`): the agent works in another agent's worktree (review,
  test, fix).
- **`output_schema`**: the final answer follows a JSON Schema and is returned as `structured`
  data. Without one, `spawn_agent` and `send_message` use the role's schema (compact, fixed
  fields for worker, planner, reviewer, tester; prose for the lead roles); `freeform: true`
  asks for prose. The command line and the dashboard always get prose.

## Notifications

Lead roles do not wait for their agents: they hand out the work and end the turn with a short
status, so they can receive messages meanwhile. When a child agent and all of its sub-agents
are idle, the parent gets an automatic `[orch notification]` turn: a short header stating that
it is a system event (not a message from the user or the parent, and no approval), then
compact JSON per agent with the answer of its last finished turn (up to 4000 characters per
agent and 10000 in total; the rest through `get_agent`) and the list of children still
running.

- Only turns the parent sent (`spawn_agent`, `send_message`, `ask_project_lead`) are reported,
  and the child's own notification turns that report on work done for that parent. Work
  requested from the command line or the dashboard is not.
- `notify: "each"` (default) reports each agent as soon as it is done; `notify: "all"` only
  once every agent the parent sent work to is done. The `next` field of the reply says whether
  the caller will be woken. The interactive `spartito chat` session never is: it uses
  `wait_for_agents`.
- The notification text is written when its turn starts, after a short delay
  (`ORCH_NOTIFY_DELAY`, 5 seconds by default), so results that arrive together share one turn.
  If by then there is nothing to report, or the turn never starts (stopped, runner gone), it is
  deleted without a trace. If a notification turn fails, its results come with the next
  notification (there is no automatic retry); until then `wait_for_agents` returns them.
- Only the end of a turn triggers a notification, never waiting (`spartito wait`,
  `wait_for_agents`). A stopped agent stays silent until its next real finish.
- Each result reaches the parent once: what `wait_for_agents` returned does not come again in
  a notification, and `wait_for_agents` marks a result already received in a notification as
  `already_delivered`. `wait_for_agents` returns early when a message has arrived for the
  caller, and refuses to wait for the caller itself or an agent above it.

## Task board

Each project has a board of items that survives across sessions. An item has a title (at most
200 characters), a description (at most 4000), one of the states `idea`, `ready`,
`in_progress`, `done`, and optionally the agent and branch working on it. Items are ordered
within their state; a new item, and an item that changes state, goes to the end.

- The orchestrator and the project's lead use `list_board`, `add_board_item` and
  `update_board_item`; a lead only on its own project. `list_board` puts `in_progress` first,
  then `ready`, `idea` and `done`, and counts what does not fit in its reply.
- In the dashboard, the board button in a project's row opens it: one list per state, add,
  edit, move up or down, change state.
- **Board** in the dashboard's left column shows every project's board in one view: items
  move by drag or keyboard, a selection is dispatched to the leads, each card shows its
  agent's status and its branch's git state, and an item whose branch is merged moves to
  `done` by itself. See [board.md](board.md).
- On the command line: `spartito board`, `spartito board add`, `spartito board edit`, `spartito board move`.

```sh
spartito board add -p my-app "Password reset" -d "Email link, expires in 1 hour" --state ready
spartito board -p my-app
spartito board dispatch -p my-app
```

**Dispatch** (`spartito board dispatch -p <project>`, or the dashboard after a confirmation) sends
all of the project's `ready` items to its lead in one message, as if you had written it: each
item's id, title and description, and the request to keep their states up to date with
`update_board_item`. The items move to `in_progress`, linked to the lead, so a second dispatch
does not send them again. With no `ready` item nothing is sent. Dispatch never happens on its
own.

## Decisions

The headless orchestrator and the project leads record what needs the owner's decision with
`request_decision(question, options, recommendation)`: a question of at most 500 characters,
at most 6 short options, one recommendation. They continue with whatever does not depend on
the answer, and close a decision that has become moot with `withdraw_decision(decision_id)`
(only their own, only while open). The interactive `spartito chat` session asks you directly
instead.

Open decisions from all projects are listed in one place: the dashboard's question-mark button
(with their number) opens the **Decisions** window (project, agent, question, options,
recommendation, age); on the command line:

```sh
spartito decisions            # open decisions; --all includes closed ones
spartito decide 12 2          # option 2
spartito decide 12 "Use Postgres"
```

The answer reaches the agent as an ordinary message from you (`Decision #12 — <question>` and
`Answer: <answer>`) and closes the decision; a closed decision takes no further answer. The
message restores an archived agent; if the agent was deleted, the decision closes without an
answer, with a note. A waiting decision also triggers a desktop notification (see
[configuration.md](configuration.md#desktop-notifications-desktop_notifications)).

## Archiving

An archived agent disappears from `spartito ps`, the dashboard's list and `list_agents`; its
record, turns, logs, branch and worktree are kept. The archive is shown by
`spartito ps --archived`, the dashboard's **Archive** entry and `list_agents(archived=true)`. An
agent comes back with `spartito restore`, **Restore** in the dashboard, `archive_agent(restore=true)`,
or any message to it (command line, dashboard, `send_message`, `ask_project_lead`, an answered
decision).

- An agent that, or one of whose sub-agents, has a queued or running turn cannot be archived.
- An agent's work is **safe** if it has no worktree of its own (planner, reviewer, a shared
  worktree), or if every commit of its branch and of the HEAD checked out in its worktree is
  contained in the project's base branch (not the case after a squash merge) and the worktree
  has no uncommitted changes (or no longer exists). Unsafe work is archived only after a
  confirmation (`--yes`, `confirm`).
- **Automatic archiving** (`archive_after_hours`, default 24) archives only idle agents whose
  work is safe, whose parent has already received their result, and in whose worktree no other
  agent is working; never a lead or the orchestrator, and never on a git error or timeout.
  There is no background process: the listings (the dashboard's polling, `spartito ps`,
  `list_agents`) run the check, at most every 5 minutes across processes and for at most about
  5 seconds per pass.
- Permanent deletion (`spartito rm`, or the dashboard's archive) is possible only for archived
  agents and only by you; agents can archive but not delete.

## Context and compaction

### Context size

Every turn records how large the conversation was at its end and the size of the model's
context window.

- **Claude:** the prompt size of the turn's last main request (input + cache read + cache
  write; subagent and synthetic requests excluded); the window is the model's `contextWindow`.
- **Codex:** the last `token_count` event in the session's rollout file
  (`$CODEX_HOME/sessions/…/rollout-…-<thread>.jsonl`), read only. With a missing file or an
  unknown format there is no figure.

`spartito ps`, `spartito status`, `list_agents`, `get_agent` (`context` field) and the dashboard show
it, for example `780k/1M 78%`; above the automatic compaction threshold (or from 80% of the
window when automatic compaction is off) in a warning colour. After a compaction the size is
unknown until the next request and `compacted` is shown.

### Compaction

```sh
spartito compact fix-login-3f2a ["what the summary must keep"]
```

The dashboard's **Compact** button does the same. A compaction is queued like a message and
runs the CLI's own compaction in the same session:

- **Claude:** the `/compact [instructions]` command, passed as is (no `{{result:…}}`
  substitution, no output schema, slash commands allowed for this turn; otherwise the agent's
  usual loadout).
- **Codex:** `/compact` does not work in `codex exec resume`. Instead a turn runs with
  `model_auto_compact_token_limit` set to half the current size and a prompt that asks for no
  work. It takes no instructions and can start only when the context size is known.

The result is a short summary such as `compacted 91k -> 1k tokens`. A compaction is not the
agent's answer: it is not delivered to the parent, wakes nobody, and does not change the
agent's status, its last turn or what `{{result:<agent>}}` refers to. Its outcome is shown
separately (`spartito status`, `get_agent`'s `last_compaction`, the dashboard). A queued
compaction can be withdrawn, not edited. Compaction can lose details from earlier in the
conversation.

For lead roles, a compaction without instructions (automatic or manual) uses short default
instructions: keep the owner's standing rules, the current state, open decisions and open
items, and work in progress with agent ids, branches and run ids. Other roles get the CLI's
default summary; Codex takes no instructions.

Claude Code also compacts on its own, even mid-turn, when the conversation nears the window
(window − 33k tokens); the turn's `info` event and usage fields record it.

### Automatic compaction

When a turn ends successfully and the context is above `compact_at_tokens` (default 256000),
the runner queues a compaction before the turn closes, while the prompt cache is still warm.
It runs before the agent's next turn and ahead of messages already queued, which then run
normally, even if the compaction fails, is withdrawn or is stopped. Until it ends, the agent
counts as working: the parent's notification and `wait_for_agents` wait for it and report the
agent's real answer.

No compaction is queued after a compaction, after a failed or stopped turn, while one is
already queued or running, or when the agent cannot be compacted. The log's `info` event
records it (`automatic compaction: context 301k is above the threshold of 256k`), and
`last_compaction` has `automatic: true`.

**Pause.** Whether a compaction helped is judged by the size actually measured afterwards,
not by the CLI's own post-compaction figure: the compacting turn's own measurement if it made
one, otherwise the first request of the next turn (Claude), otherwise the size at the end of
that next turn (Codex). If that is still above the threshold, automatic compaction pauses for
the agent: an `info` event (`automatic compaction paused: …`) is written once, and the
`context.compact_paused` field, `spartito status` and the dashboard show it. A compaction that
brings the size below the threshold, or a threshold above the measured size, lifts the pause.
While no size has been measured since the last compaction, nothing is queued.

With Codex, automatic compaction works like a manual one: it is queued only when the context
size is known, and runs the turn with the lowered limit. If the model does not compact during
that turn, the compaction fails (`nothing was compacted`), the context stays above the
threshold, and automatic compaction pauses for that agent as described above.

If `spartito stop`
arrives while a compaction is being queued, the turn becomes `stopped` and the compaction is
withdrawn.

## Tokens and cost

Every turn stores its own tokens (input, output, cache read, cache write), its cost and the
model the CLI actually used. When a CLI reports totals for the whole session instead (Codex
always, Claude in some cases), the sum of the session's earlier turns is subtracted. Known
inaccuracy: the tokens of a turn that failed or was stopped without a result are counted in
the agent's next turn; the agent's total is correct. Codex reports no cost.

`spartito ps`, `spartito status`, `list_agents` and `get_agent` (with the last 10 turns) and the
dashboard show the figures. `model` and `effort` in these views are the values the next turn
will use; the agent's own values appear in the detailed views as
`model_override`/`effort_override`. The lists give per agent only `cost_usd` and a short
`tokens` object (`in`, `out`, `cache_read`, `cache_write`).

Spend over time (per project, day or agent) and budgets: `spartito cost`, `spartito budget`
and the dashboard's **Cost** view ([usage.md](usage.md#cost-and-budgets),
[configuration.md](configuration.md#budgets)).

Per-turn files are in `~/.orchestrator/logs/run-<id>/`: the raw output stream, stderr,
`result.md` (the full final answer; for a failed turn the last text or the error) and
`result.json` for a structured answer. A stopped turn or a crashed runner writes no
`result.md`. Its path is `result_file` in `get_agent`.
