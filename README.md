# Spartito

Spartito runs Claude Code and Codex agents headless and in parallel, each in its own git
worktree and branch. You steer them through one orchestrator agent (shown as *Maestro*) from a
web dashboard or the command line, instead of managing many terminal sessions by hand.

```
you ── orchestrator (Maestro)
        └─ project ── project lead (in the project's main checkout)
                        ├─ worker    (own git worktree, branch orch/<agent-id>)
                        ├─ reviewer  (reads the worker's worktree after it finishes)
                        └─ ...
```

The command is `orch`; the Python package is `orchestrator`.

## Features

- One conversation with an orchestrator agent that briefs a project lead per project; leads
  start and coordinate their own agents.
- Agents run headless (`claude -p`, `codex exec`) in parallel, each in its own git worktree and
  branch, or in a worktree shared with another agent (review, test, fix).
- Packaged roles: orchestrator, lead, planner, worker, reviewer, tester; custom roles are
  Markdown files.
- Results travel up automatically: a parent is woken with its agents' answers when they are
  done; tasks can depend on each other and embed another agent's answer.
- Web dashboard: conversations, tool calls, queued messages, model and effort per agent,
  context size, tokens and cost.
- Model and effort per tier (orchestrator, lead, worker), per provider, globally or per
  project; per-agent overrides.
- Persistent task board per project, and a list of decisions that wait for you.
- Automatic compaction of long conversations, automatic archiving of finished agents, keeping
  the machine awake while agents work, desktop notifications (macOS).
- No daemon: every turn is its own process; all state is in one SQLite database.

## Requirements

- macOS or Linux (see [Limitations and platform support](#limitations-and-platform-support)).
- Python 3.14 and [uv](https://docs.astral.sh/uv/).
- `git`.
- The `claude` (Claude Code) and/or `codex` CLI, installed and logged in. `orch chat` needs
  `claude`.

## Installation

From a checkout of this repository:

```sh
uv tool install --editable .    # puts `orch` on the PATH
orch --help
```

Without installing, prefix every command with `uv run` inside the checkout (`uv run orch …`).

Spartito keeps its state in `~/.orchestrator/`. Set `ORCH_HOME` to use another directory.

## Quick start

Register a git repository as a project. Its currently checked-out branch becomes the
project's base branch, from which agent branches start:

```sh
orch project add ~/src/my-app
# added my-app  /home/dev/src/my-app  (base branch: main)
```

### Through the orchestrator

```sh
orch web          # dashboard at http://127.0.0.1:8787
```

Select **Maestro** and describe the goal, for example: *"In my-app, fix the login redirect
loop. Have a worker implement it with Claude and a reviewer check it with Codex."* The
orchestrator passes the brief to the project lead, which starts the agents. They appear in the
left-hand list as they start; click one to follow its conversation. When the lead's agents are
done, the lead reports to the orchestrator, which reports to you.

Alternatively, `orch chat` opens the orchestrator as an interactive Claude Code session in the
terminal: a separate conversation with the same tools and the same state.

### Directly from the command line

```sh
orch spawn -p my-app -n fix-login "Fix the redirect loop after login. Add a regression test."
# fix-login-3f2a  run 1  /home/dev/.orchestrator/worktrees/my-app/fix-login-3f2a

orch logs fix-login-3f2a -f     # follow what it does
orch wait fix-login-3f2a        # block until it is done
orch status fix-login-3f2a      # result, commits and changes in its worktree
```

The worker commits on branch `orch/fix-login-3f2a`. Review and merge it yourself; agents do
not merge:

```sh
cd ~/src/my-app
git log --oneline main..orch/fix-login-3f2a
git merge orch/fix-login-3f2a
orch archive fix-login-3f2a     # hide it from the lists; everything is kept
```

## Core concepts

**Orchestrator.** The top-level agent, displayed as *Maestro*, agent id `orchestrator`. It
belongs to no project and works through the `orch` MCP tools: it registers projects, briefs
project leads and answers status questions. In the dashboard it is a headless agent like any
other; `orch chat` is a separate interactive Claude Code session with the same tools and the
same state.

**Project lead.** One long-lived agent per project (role `lead`), created by the first message
to it (`orch ask`, the dashboard's lead conversation, or the orchestrator's
`ask_project_lead`). It works in the project's main checkout, knows the codebase, splits work
into tasks and starts its own agents. A lead is confined to its own project.

**Agents and roles.** Every agent has a role, a Markdown prompt with a few settings:

| role | works in | purpose |
|---|---|---|
| `orchestrator` | its own workspace | top-level coordination (lead role) |
| `lead` | main checkout | owns one project, runs its agents (lead role) |
| `planner` | main checkout, read-only | turns a goal into a plan and task breakdown |
| `worker` | own worktree | implements one task and commits it |
| `reviewer` | main checkout or a shared worktree, read-only | reviews changes, reports findings |
| `tester` | own or shared worktree | writes and runs tests for a change |

Roles with `lead: true` get the `orch` MCP tools and can start agents. `orch roles` lists the
roles available; custom roles are described in [Configuration](#roles).

**Worktrees and branches.** An agent whose role works in a worktree gets
`~/.orchestrator/worktrees/<project>/<agent-id>` on a new branch `orch/<agent-id>`, started
from the project's base branch (or from another ref or agent's branch with `--base`). Agent
ids are the given name (or role) plus four hex characters, e.g. `fix-login-3f2a`. An agent can
also work in another agent's worktree (`--share`), or in the main checkout (`--no-worktree`).
Agents do not merge; merging into the base branch is left to you.

**Turns.** A message to an agent starts a turn: one headless CLI invocation that continues the
agent's session (`claude --resume`, `codex exec resume`), so the agent keeps its conversation.
An agent runs one turn at a time; further messages queue in order and can be edited or
withdrawn until they start. Each turn has a run id (`run 12` in command output, `#12` in
`orch status`), which commands such as `orch withdraw` take.

**Results and notifications.** Leads and the orchestrator do not wait for their agents: they
hand out work and end their turn. When an agent and all of its sub-agents are idle, its parent
receives an automatic `[orch notification]` turn with the agents' answers. A lead's answer
therefore reaches the orchestrator once the lead and all its agents are done. Agents started
through the MCP tools answer in their role's compact JSON schema (worker, planner, reviewer,
tester) or in prose (leads); agents started from the command line or the dashboard answer in
prose. Tasks can depend on each other (`--after`) and embed another agent's final answer
(`{{result:<agent-id>}}`). Details: [docs/agents.md](docs/agents.md).

**Decisions.** What needs your decision is recorded as a decision instead of being buried in a
conversation; the agent carries on with what does not depend on it. See
[Task board and decisions](#task-board-and-decisions).

## Everyday use

### Command line

| command | purpose |
|---|---|
| `orch project add/list/rm` | register, list and unregister projects |
| `orch chat` | interactive orchestrator session (Claude Code) |
| `orch web` | dashboard |
| `orch ask <project> "…"` | message the project lead (creates it) |
| `orch spawn -p <project> "…"` | start an agent (`-r` role, `-n` name, `--provider`, `--after`, `--share`, `--base`) |
| `orch send <agent> "…"` | follow-up message, queued if the agent is busy |
| `orch ps` | tree of projects and agents: state, model/effort, context, tokens, cost |
| `orch status <agent>` | one agent: last result, queue, context, commits and changes |
| `orch logs <agent> -f` | activity: messages, tool calls, errors |
| `orch wait <agent>…` | block until idle; exit code 0 done, 1 failed, 2 timeout |
| `orch withdraw <run>`, `orch edit <run> "…"` | withdraw or rewrite a queued message |
| `orch stop <agent>` | stop its running and queued turns |
| `orch attach <agent>` | continue its session interactively in `claude` / `codex` |
| `orch model <agent>` | show or set its own model and effort |
| `orch compact <agent>` | compact its conversation |
| `orch archive`, `orch restore`, `orch rm` | archive, bring back, delete for good |
| `orch board …`, `orch decisions`, `orch decide` | task board and decisions |
| `orch config …` | settings |
| `orch roles` | available roles |

Every command and option: [docs/usage.md](docs/usage.md#command-reference), or
`orch <command> --help`.

### Dashboard

`orch web` serves the dashboard on `http://127.0.0.1:8787` (`--host`, `--port`). The left
column lists Maestro, the projects with their agents as a tree, and the archive. From there:
talk to Maestro or a project lead, start an agent (**+**), open a project's task board, add or
remove projects, open the **Decisions** list and the settings (**Models and effort**). An
agent's page shows its conversation and tool calls, its queued messages, its model and effort,
context size and cost, with **Stop**, **Compact** and **Archive**.

The dashboard has no login. Bound to a loopback address (the default), it answers only requests
addressed to that host and rejects writes from other websites. Binding it to another address
(`--host 0.0.0.0`) gives anyone who can reach it control over agents that run code. Details:
[docs/usage.md](docs/usage.md#dashboard).

### MCP tools

The orchestrator and the project leads (roles with `lead: true`) get an MCP server named
`orch` (`orch mcp`), with these tools:

| group | tools |
|---|---|
| projects and roles | `list_projects`, `add_project`, `list_roles` |
| starting work | `spawn_agent`, `ask_project_lead`, `send_message`, `withdraw_message` |
| following work | `wait_for_agents`, `list_agents`, `get_agent`, `agent_log` |
| managing agents | `stop_agent`, `archive_agent` |
| decisions | `request_decision`, `withdraw_decision` |
| task board | `list_board`, `add_board_item`, `update_board_item` |

A lead sees only its own project; `add_project` and `ask_project_lead` are for the
orchestrator. Parameters and behaviour: [docs/agents.md](docs/agents.md#mcp-tools).

## Configuration

`orch config show [-p <project>]` prints the current settings. Full reference:
[docs/configuration.md](docs/configuration.md).

### Model and effort tiers

Model and effort are set per tier and per provider: `orchestrator`, `lead` (project leads) and
`worker` (every other role). A project can override the global values.

```sh
orch config set lead --model opus --effort high
orch config set worker --model sonnet --effort medium
orch config set worker --provider codex --effort low
orch config set worker --model haiku -p my-app        # only in my-app
orch model fix-login-3f2a --model opus                # one agent, from its next turn
```

For each turn the first value found applies: the agent's own value → project setting → global
setting → the role's `model` → the CLI's default. Effort levels: Claude `low`, `medium`,
`high`, `xhigh`, `max`; Codex `low`, `medium`, `high`, `xhigh`.

### Settings

| setting | command | scope | default |
|---|---|---|---|
| model and effort per tier | `orch config set <tier>` | global, project | the CLI's default |
| `compact_at_tokens`: automatic compaction threshold | `orch config compact-at <tokens>` | global, project | `256000` |
| `archive_after_hours`: automatic archiving | `orch config archive-after <hours>` | global, project | `24` |
| `keep_awake`: prevent idle sleep while a turn runs | `orch config keep-awake on\|off` | global | `on` |
| `desktop_notifications` (macOS) | `orch config desktop-notifications on\|off` | global | `on` |

`0` or `off` turns compaction and archiving off; an empty value (`""`) clears a setting so
that the global value or the default applies. All settings can also be changed in the
dashboard's **Models and effort** window.

- **Automatic compaction.** When a turn ends with the agent's context above the threshold, a
  compaction is queued before the agent's next turn. If a compaction leaves the context above
  the threshold, automatic compaction pauses for that agent until a later compaction brings it
  below the threshold or the threshold is raised.
- **Automatic archiving.** Archives idle agents whose work is safe (no worktree of their own,
  or everything merged into the base branch and no uncommitted changes) after the given number
  of idle hours; never leads or the orchestrator.
- **Keep-awake.** `caffeinate -i` on macOS, `systemd-inhibit --what=idle` on Linux; prevents
  idle sleep only, not a closed lid or a forced sleep.
- **Desktop notifications.** When a decision waits for you, and when an agent you messaged
  directly has finished together with all its agents.

The limit on review rounds is not a setting: the packaged `lead` role prompt allows at most
two review rounds per branch (the review and one re-review of the fixes), after which the lead
reports the findings still open. Override the `lead` role to change it.

### Loadout

Each role declares what its CLI loads besides the role prompt, so that agents do not inherit
your whole global Claude Code or Codex configuration (plugins, skills, connectors, MCP
servers). By default, agents load only the `orch` MCP server (leads), the project's own
`CLAUDE.md` and `.claude/` settings, and a fixed set of built-in tools; skills and slash
commands are off and Codex ignores `~/.codex/config.toml`.

If you rely on `~/.claude/settings.json` (for example `env` for Bedrock, Vertex or a proxy,
`apiKeyHelper`, `model`) or on Codex's `model_provider` or profiles, restore them in
`~/.orchestrator/loadout.toml`:

```toml
[default]
setting_sources = "user,project,local"   # Claude: ~/.claude/settings.json applies again
codex_user_config = true                 # Codex: ~/.codex/config.toml applies again
```

Keys, per-role defaults and per-project overrides (`<project>/.orch/loadout.toml`):
[docs/configuration.md](docs/configuration.md#loadout).

### Roles

A role is a Markdown file with YAML-style frontmatter (`description`, `lead`, `worktree`,
`provider`, `model`, `output_schema` and the loadout keys). Files in
`<project>/.orch/roles/<name>.md` and `~/.orchestrator/roles/<name>.md` add roles or override
the packaged ones by name, in that order of precedence. Reference:
[docs/configuration.md](docs/configuration.md#roles).

## Task board and decisions

**Task board.** Each project has a persistent board of items in four states: `idea`, `ready`,
`in_progress`, `done`. You and the agents (orchestrator, the project's lead) keep it up to
date; it survives across sessions. **Dispatch** sends all `ready` items to the project lead in
one message and moves them to `in_progress`.

```sh
orch board add -p my-app "Password reset" -d "Email link, expires in 1 hour" --state ready
orch board -p my-app
orch board dispatch -p my-app
```

**Decisions.** The orchestrator and the leads record questions for you with
`request_decision` (a question, up to six options, a recommendation). Open decisions from all
projects are listed in the dashboard (the question-mark button) and on the command line:

```sh
orch decisions
orch decide 12 2                     # option 2
orch decide 12 "Use Postgres"        # or a free-text answer
```

The answer reaches the agent as a message from you. Details:
[docs/agents.md](docs/agents.md#task-board).

## How it works

- **No daemon.** `orch`, the dashboard and the MCP server write to a SQLite database and start
  one detached runner process (`python -m orchestrator.runner <run-id>`) per turn. The runner
  waits for dependencies, creates the worktree if needed, runs `claude -p --output-format
  stream-json` or `codex exec --json`, and turns the output into uniform events.
- **State** lives under `~/.orchestrator/` (or `$ORCH_HOME`): `orch.db` (projects, agents,
  turns, events, usage, settings, board, decisions), `worktrees/<project>/<agent-id>/`, and
  `workspace/` (the orchestrator's working directory).
- **Logs** per turn in `logs/run-<id>/`: the CLI's raw output stream, stderr, `result.md`
  (the full final answer, or the error of a failed turn) and `result.json` for a structured
  answer.
- **Permissions.** Claude agents run with `--permission-mode auto`, Codex agents in the
  `workspace-write` sandbox; `orch spawn --permission` overrides this per agent.
- **Providers** implement a small protocol (`providers/base.py`); further providers can be
  registered with `ORCH_PROVIDERS="name=package.module:Class"`.

Source layout:

```
src/orchestrator/
  cli.py           the `orch` command
  service.py       the core operations, used by the CLI, the MCP server and the dashboard
  store.py         SQLite (WAL): the only shared state
  runner.py        one turn in its own detached process
  providers/       base.py (protocol), claude.py, codex.py
  mcp_server.py    the MCP tools of the orchestrator and the leads
  notify.py        notification turns to parents
  replies.py       results cut to a size budget for agents
  models.py        Project, Agent, Run (one turn), Event
  roles.py         role loading; roles/*.md are the packaged roles
  loadout.py       what an agent loads (tools, MCP servers, setting sources)
  memory_guard.py  hook that confines the `memory` key's file writes to the memory directory
  settings.py      tiers and settings, global and per project
  board.py         task board
  decisions.py     decisions
  keep_awake.py    sleep inhibitor
  desktop.py       desktop notifications (macOS)
  git.py           worktree handling
  web/             dashboard: one page and a JSON API
```

## Limitations and platform support

- **Platforms.** Developed and used on macOS. On Linux, the sleep inhibitor
  (`systemd-inhibit`) and the cleanup of a stopped turn's processes have not been tested on a
  real machine, and there are no desktop notifications. Windows is not supported: process
  handling relies on POSIX process groups and sessions.
- **Codex.** Codex reports no cost. The context size is known only from the session's rollout
  file under `$CODEX_HOME/sessions/`. `/compact` does not work in `codex exec resume`, so a
  compaction is a turn with a lowered `model_auto_compact_token_limit`; it takes no
  instructions and needs a known context size. Codex reports token counts per session, so
  per-turn figures are derived by subtraction; the tokens of a turn that failed or was stopped
  without a result are counted in the agent's next turn (the agent's total is correct).
- **Notifications.** A lead's answer reaches its parent only once the lead and all of its
  agents are idle.
- **Role prompts.** A Codex agent receives its role prompt in the first turn of its session
  and keeps that version; a running `orch chat` session keeps the prompt it started with.
  Headless Claude agents receive the current role prompt with every turn.
- **Upgrades.** A running `orch web` keeps the code it was started with; restart it after an
  upgrade. New turns start new runner processes and use the new code.
- **Automatic compaction** pauses for an agent when a compaction did not bring its context
  below the threshold.
- **Loadout changes** do not shrink existing sessions: the earlier tool and skill lists stay in
  their conversation history.

## Development

```sh
uv sync
uv run pytest                  # uses a fake provider and real git worktrees; no claude/codex calls
uv run ruff check . && uv run ruff format --check .
node --check src/orchestrator/web/static/app.js
```
