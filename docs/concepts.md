# Core concepts

- [Orchestrator](#orchestrator)
- [Project lead](#project-lead)
- [Agents and roles](#agents-and-roles)
- [Worktrees and branches](#worktrees-and-branches)
- [Turns](#turns)
- [Results and notifications](#results-and-notifications)
- [Decisions and the task board](#decisions-and-the-task-board)

```
you ── orchestrator (Maestro)
        └─ project ── project lead (in the project's main checkout)
                        ├─ worker    (own git worktree, branch orch/<agent-id>)
                        ├─ reviewer  (reads the worker's worktree after it finishes)
                        └─ ...
```

## Orchestrator

The top-level agent, displayed as *Maestro*, agent id `orchestrator`. It belongs to no
project and works through the `orch` MCP tools: it registers projects, briefs project leads
and answers status questions. In the dashboard it is a headless agent like any other;
`orch chat` is a separate interactive Claude Code session with the same tools and the same
state.

## Project lead

One long-lived agent per project (role `lead`), created by the first message to it
(`orch ask`, the dashboard's lead conversation, or the orchestrator's `ask_project_lead`). It
works in the project's main checkout, knows the codebase, splits work into tasks and starts
its own agents. A lead is confined to its own project.

## Agents and roles

Every agent has a role, a Markdown prompt with a few settings:

| role | works in | purpose |
|---|---|---|
| `orchestrator` | its own workspace | top-level coordination (lead role) |
| `lead` | main checkout | owns one project, runs its agents (lead role) |
| `planner` | main checkout, read-only | turns a goal into a plan and task breakdown |
| `worker` | own worktree | implements one task and commits it |
| `reviewer` | main checkout or a shared worktree, read-only | reviews changes, reports findings |
| `tester` | own or shared worktree | writes and runs tests for a change |

Roles with `lead: true` get the `orch` MCP tools and can start agents. `orch roles` lists the
roles available; custom roles are Markdown files, described in
[configuration.md](configuration.md#roles).

## Worktrees and branches

An agent whose role works in a worktree gets `~/.orchestrator/worktrees/<project>/<agent-id>`
on a new branch `orch/<agent-id>`, started from the project's base branch (or from another ref
or agent's branch with `--base`). The base branch is the branch checked out when the project
was registered (`orch project add`). Agent ids are the given name (or role) plus four hex
characters, e.g. `fix-login-3f2a`. An agent can also work in another agent's worktree
(`--share`, for review, test or fix), or in the main checkout (`--no-worktree`).

Agents do not merge; merging into the base branch is left to you.

## Turns

A message to an agent starts a turn: one headless CLI invocation (`claude -p`, `codex exec`)
that continues the agent's session (`claude --resume`, `codex exec resume`), so the agent
keeps its conversation. An agent runs one turn at a time; further messages queue in order and
can be edited or withdrawn until they start. Each turn has a run id (`run 12` in command
output, `#12` in `orch status`), which commands such as `orch withdraw` take. Details:
[agents.md](agents.md#turns-and-the-queue).

## Results and notifications

Leads and the orchestrator do not wait for their agents: they hand out work and end their
turn. When an agent and all of its sub-agents are idle, its parent receives an automatic
`[orch notification]` turn with the agents' answers. A lead's answer therefore reaches the
orchestrator only once the lead and all its agents are done.

Agents started through the MCP tools answer in their role's compact JSON schema (worker,
planner, reviewer, tester) or in prose (leads); agents started from the command line or the
dashboard answer in prose. Tasks can depend on each other (`--after`) and embed another
agent's final answer (`{{result:<agent-id>}}`). Details:
[agents.md](agents.md#dependencies-and-results) and
[agents.md](agents.md#notifications).

## Decisions and the task board

What needs your decision is recorded as a decision instead of being buried in a
conversation; the agent carries on with what does not depend on it. Each project also has a
persistent task board of ideas and tasks that you and the agents keep up to date. See
[agents.md](agents.md#decisions), [agents.md](agents.md#task-board) and
[board.md](board.md).
