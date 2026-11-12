# Command line and dashboard

- [Command reference](#command-reference)
- [Dashboard](#dashboard)

Terms used here (see the [README](../README.md#core-concepts)): a *turn* is one prompt and
the agent's answer; its *run id* is the number commands print as `run 12` and `orch status`
lists as `#12`. *Project lead* or *lead* is the agent with role `lead` of a project.

## Command reference

`orch <command> --help` shows the same options. Agent ids are those printed by `orch spawn` and
listed by `orch ps`; the top-level orchestrator's id is `orchestrator`.

### Projects

```sh
orch project add [PATH] [--name NAME] [--provider claude|codex]
orch project list
orch project rm NAME
```

- `add` registers the git repository containing `PATH` (default: the current directory). The
  name defaults to the repository's directory name; `--provider` is the default provider for
  the project's agents (default `claude`). The branch checked out at that moment becomes the
  project's base branch. A repository without commits is accepted with a note: the first agent
  that needs a worktree creates an empty initial commit, and worktree agents only see
  committed files.
- `rm` unregisters a project that has no agents (archived ones included). The repository is not
  touched.

### Orchestrator

```sh
orch chat [--continue|-c] [--here] [--model MODEL] [--effort EFFORT]
orch web [--host 127.0.0.1] [--port 8787]
orch mcp
```

- `chat` opens the orchestrator as an interactive Claude Code session (requires `claude`). It
  runs in `~/.orchestrator/workspace/` unless `--here` is given; `--continue` continues the
  last conversation there. Model and effort default to the global Claude setting of the
  `orchestrator` tier. The session gets the `orch` MCP tools and may run `orch …` commands
  through Bash. It is separate from the dashboard's orchestrator agent but sees the same state.
  Being interactive, it is never woken by notifications: use `wait_for_agents` there.
- `web` serves the [dashboard](#dashboard). With a non-loopback `--host` it prints a warning:
  the dashboard has no authentication.
- `mcp` runs the `orch` MCP server on stdio. `orch chat` and the agents with a lead role
  start the same server themselves; there is no need to run it by hand.

### Dashboard window, menu bar and background service

```sh
orch app [--port PORT] [--agent ID | --decision N]
orch app install | uninstall
orch menubar
orch service install [--host 127.0.0.1] [--port 8787]
orch service status
orch service logs [--lines|-n 20]
orch service uninstall
```

- `app` shows the dashboard in its own window: a native one with the extra `app`, else a
  Chromium-family browser in app mode, else the default browser. It uses the running
  `orch web` (or service) of this home, or serves the dashboard on `127.0.0.1` (a free port,
  or `--port`) until the window is closed. One window per home: a second `orch app` brings it
  to the front, at `--agent` or `--decision` when given.
- `app install` adds a launcher with Spartito's name and icon (macOS:
  `~/Applications/Spartito.app`, which also posts notifications itself; Linux: a desktop
  entry); `app uninstall` removes it.
- `menubar` (macOS, extra `menubar`) shows the running agents and open decisions of this home
  in the menu bar; its menu opens a decision or `orch app`. One item per home.
- `service install` runs `orch web --host … --port …` in the background, started at login and
  restarted when it stops (macOS: launchd agent; Linux: systemd user unit). Running it again
  replaces the unit and restarts the service. `status` shows whether it is installed and
  running and whether the dashboard answers; `logs` shows where its output goes (on macOS
  also the last lines); `uninstall` stops and removes it.

Details: [desktop.md](desktop.md).

### Starting and talking to agents

```sh
orch spawn TASK -p PROJECT [-n NAME] [-r ROLE] [--provider P] [--model M] [--effort E]
           [--worktree|--no-worktree] [--share AGENT] [--base REF_OR_AGENT]
           [--after AGENT_OR_RUN ...] [--permission MODE]
orch ask PROJECT MESSAGE
orch send AGENT MESSAGE [--after AGENT_OR_RUN ...]
```

- `spawn` starts a new agent and returns at once, printing its id, run id and working
  directory. `TASK` must be self-contained: the agent knows nothing else. Defaults: role
  `worker`, the project's provider, the tier's model and effort, a worktree if the role works
  in one. `--share` works in another agent's worktree; `--base` branches from a ref or from
  another agent's branch; `--after` (repeatable) waits until those agents or turns (by run id)
  have finished successfully, and skips this agent if one fails. `--permission` is the Claude
  permission mode or the Codex sandbox mode (defaults: `auto`, `workspace-write`).
- `ask` sends a message to the project's lead, creating it on first use.
- `send` sends a follow-up to an agent; it continues its conversation. Messages to a busy agent
  queue and run in order. A message to an archived agent restores it.

Examples:

```sh
orch spawn -p my-app -r planner "Plan the password reset flow."
orch spawn -p my-app -n api --after planner-9c1e "Implement this plan: {{result:planner-9c1e}}"
orch spawn -p my-app -r reviewer --provider codex --share api-b7d0 --after api-b7d0 "Review the change."
orch send api-b7d0 "Address the review findings: {{result:reviewer-41aa}}"
orch ask my-app "Add rate limiting to the login endpoint."
```

The text forms `{{result:<agent-id>}}` and `{{result:<agent-id>:full}}` are described in
[agents.md](agents.md#dependencies-and-results).

### Following agents

```sh
orch ps [-p PROJECT] [--archived|-a]
orch status AGENT
orch logs AGENT [--follow|-f] [--tail|-n 50]
orch wait AGENT... [--timeout 3600]
```

- `ps` prints the orchestrator (once it exists), then each project with its agents as a tree:
  state, role, provider, model/effort (`*` marks a value set on the agent), context, tokens,
  cost and branch. In the state column, `idle·3↓` means the agent has no turn of its own but 3
  sub-agents are working (it is notified when they finish); `running·3↓` means its own turn
  runs meanwhile; `failed·3↓` that its last turn failed while they work; `+2` that two more
  turns are queued. `--archived` lists the archive instead.
- `status` shows one agent: state, model and effort (and whether set on the agent), usage,
  context and automatic compaction state, the last compaction, the queue (with run ids), the
  commits and uncommitted changes in its worktree, and the last turn's result or error.
- `logs` prints the agent's events (messages, tool calls, errors, info); `-f` follows.
- `wait` blocks until the agents and their sub-agents are idle and prints their results. Exit
  code: 0 all done, 1 at least one did not succeed, 2 timeout.

### Changing queued turns and stopping

```sh
orch withdraw RUN_ID
orch edit RUN_ID MESSAGE
orch stop AGENT
orch attach AGENT
```

- `withdraw` removes a queued message or compaction before it starts; `edit` replaces a queued
  message's text. Both are refused once the turn has started. A compaction can be withdrawn but
  not edited; a notification turn can be neither (its text is written when it starts).
- `stop` stops the agent's running and queued turns; the agent and its worktree stay.
- `attach` continues the agent's session interactively (`claude --resume` or `codex resume`)
  in its working directory. The agent must be idle.

### Model, effort and compaction of one agent

```sh
orch model AGENT [--model MODEL] [--effort EFFORT]
orch compact AGENT [INSTRUCTIONS]
```

- `model` without options shows the effective, own and inherited values; with options it sets
  the agent's own value (`""` clears it). The change applies from the next turn. In an existing
  session a model change starts a new prompt cache (the next turn reads the whole conversation
  at full price); an effort change keeps the cache with Codex and may start a new one with
  Claude. The orchestrator can be set too (`orch model orchestrator --effort high`).
- `compact` queues a compaction of the agent's conversation. See
  [agents.md](agents.md#context-and-compaction).

A model name can be any single word: names with whitespace or control characters, a leading
`-`, or more than 200 characters are refused (`orch spawn`, `orch config set`, `orch model`
and the MCP `spawn_agent` check this).

### Archive

```sh
orch archive AGENT [--yes|-y]
orch archive --safe [-p PROJECT]
orch restore AGENT
orch rm AGENT [--delete-branch]
```

- `archive` hides an agent from `orch ps`, the dashboard and `list_agents`; its record, turns,
  logs, branch and worktree are kept. It asks for confirmation when the branch has commits not
  in the base branch or the worktree has uncommitted changes (`--yes` skips the question). An
  agent that, or whose sub-agents, has a queued or running turn cannot be archived.
- `archive --safe` archives every idle agent whose work is safe, in one project or all.
- `restore` brings an agent back; so does any message to it.
- `rm` deletes an archived agent for good, with its worktree (uncommitted work is lost);
  `--delete-branch` deletes its branch too. Only archived agents can be deleted, and only from
  the command line or the dashboard, never by an agent.

Safe work and automatic archiving: [agents.md](agents.md#archiving).

### Task board and decisions

```sh
orch board [-p PROJECT] [--state|-s STATE]
orch board add TITLE -p PROJECT [--description|-d TEXT] [--state|-s idea]
orch board edit ITEM_ID [--title T] [-d TEXT] [-s STATE] [--agent AGENT] [--branch BRANCH]
orch board move ITEM_ID up|down
orch board dispatch -p PROJECT

orch decisions [--all|-a]
orch decide DECISION_ID ANSWER
```

States: `idea`, `ready`, `in_progress`, `done`. `orch board` without `-p` lists every
project's board. In `board edit`, `""` clears `--agent` or `--branch`. `decide` takes an
option number or free text. See [agents.md](agents.md#task-board) and
[agents.md](agents.md#decisions).

### Settings and roles

```sh
orch config show [-p PROJECT]
orch config set TIER [--provider claude] [--model M] [--effort E] [-p PROJECT]
orch config compact-at TOKENS [-p PROJECT]
orch config archive-after HOURS [-p PROJECT]
orch config keep-awake on|off
orch config desktop-notifications on|off
orch roles
```

See [configuration.md](configuration.md).

## Dashboard

`orch web` serves a single page on `http://127.0.0.1:8787`. It reads the same database as the
command line, so both can be used side by side.

**Left column.** **Maestro** (the orchestrator) at the top; then each project with its agents
as a tree, and the **Archive** entry when something is archived. A project's row has buttons
to talk to its lead, start a new agent, open its task board, archive every finished agent
whose work is safe, and remove the project. **+** next to *Projects* adds a project through a
folder picker that marks git repositories. The header has the **Decisions** button (with the
number of open decisions), the **Models and effort** settings and the theme switch. The footer
sums agents, tokens and cost.

**Links.** `#<agent-id>` selects an agent and `#decision:<id>` opens the **Decisions** window
on that decision, for example `http://127.0.0.1:8787/#decision:12`.

**Conversation.** Selecting Maestro, a lead or an agent shows its conversation with tool calls
and results; Enter sends a message. Notification turns appear as faint system messages.
Messages that have not started yet are listed under **Queued**, where they can be edited
(pencil) or withdrawn (**✕**). A state such as *idle · 3 agents working* means the agent
itself is idle while agents it started are working; you can still write to it.

**Agent header.** Status, context bar (for example `780k/1M 78%`, with the automatic
compaction threshold), tokens and cost, and the buttons **Stop**, **Compact** (with optional
instructions) and **Archive** / **Restore**. The model and effort drop-downs:

- list the provider's models (for Claude, the models the installed Claude Code offers, read
  once per process without a model request; if that fails, the aliases, marked
  `model list unavailable: showing aliases`), the model the last turn actually used (`→ …`),
  `other…` for any name (Enter applies, Esc discards), and `inherit (…)`, which clears the
  agent's own value and shows what applies instead (the tier setting or `CLI default`);
- mark the agent's own value as `own`; a change applies from the next turn.

**New agent** (**+** in a project's row): task, role, provider, name, model, effort,
workspace (own worktree, main checkout or another agent's worktree) and *Start after*
dependencies.

**Models and effort** window: the tier settings per provider, the automatic compaction
threshold and the archive hours, globally or for one project; keep-awake and desktop
notifications globally. Under **Notifications**, **Allow in this browser** lets the page show
the notifications itself, so that a click opens the agent or decision (see
[desktop.md](desktop.md#notifications-with-click-through)).

**Decisions** window: every open decision across projects with its project, agent, question,
options, recommendation and age; answer with an option or free text.

**Task board** window: one list per state; add, edit, move up or down, change state, and
**Dispatch ready items** (after a confirmation).

**Security.** There is no login. Bound to a loopback address (the default), the dashboard
answers only requests addressed to `127.0.0.1`, `localhost` or `::1`, and every write must come
from the page itself (same origin, JSON body). Anyone who can reach a dashboard bound to
another address can start agents that run code.

**Installing.** The page has a web app manifest: Chrome offers Install in the address bar,
Safari **File › Add to Dock** ([desktop.md](desktop.md#installable-page)).

**JSON API.** The page uses a small JSON API under `/api/` (see `web/app.py`). Calls that are
convenient from scripts:

- `GET /api/ping`: `{"tool": "orchestrator", "version": "…", "home": "…"}`, where `home` is a
  digest of the resolved `ORCH_HOME`, never the path.

- `POST /api/agents/<id>/model` with `{"model": "...", "effort": null}`: `null` clears a value,
  a missing key leaves it unchanged.
- `POST /api/agents/<id>/compact` with `{"instructions": "..."}`.
