# Command line and dashboard

- [Commands at a glance](#commands-at-a-glance)
- [Command reference](#command-reference)
- [Dashboard](#dashboard)
- [Upgrading from `orch`](#upgrading-from-orch)

Terms used here (see [concepts.md](concepts.md)): a *turn* is one prompt and
the agent's answer; its *run id* is the number commands print as `run 12` and `spartito status`
lists as `#12`. *Project lead* or *lead* is the agent with role `lead` of a project.

## Commands at a glance

| command | purpose |
|---|---|
| `spartito project add/list/rm` | register, list and unregister projects |
| `spartito chat` | interactive orchestrator session (Claude Code) |
| `spartito web` | dashboard |
| `spartito app` | dashboard in its own window |
| `spartito menubar` | running agents and open decisions in the macOS menu bar |
| `spartito service install/uninstall/status/logs` | dashboard as a background service |
| `spartito notices` | how the recent desktop notifications were shown, and why |
| `spartito ask <project> "…"` | message the project lead (creates it) |
| `spartito spawn -p <project> "…"` | start an agent (`-r` role, `-n` name, `--provider`, `--after`, `--share`, `--base`) |
| `spartito send <agent> "…"` | follow-up message, queued if the agent is busy |
| `spartito ps` | tree of projects and agents: state, model/effort, context, tokens, cost |
| `spartito status <agent>` | one agent: last result, queue, context, commits and changes |
| `spartito logs <agent> -f` | activity: messages, tool calls, errors |
| `spartito wait <agent>…` | block until idle; exit code 0 done, 1 failed, 2 timeout |
| `spartito withdraw <run>`, `spartito edit <run> "…"` | withdraw or rewrite a queued message |
| `spartito stop <agent>` | stop its running and queued turns |
| `spartito attach <agent>` | continue its session interactively in `claude` / `codex` |
| `spartito model <agent>` | show or set its own model and effort |
| `spartito compact <agent>` | compact its conversation |
| `spartito archive`, `spartito restore`, `spartito rm` | archive, bring back, delete for good |
| `spartito board …`, `spartito decisions`, `spartito decide` | task board and decisions |
| `spartito config …` | settings |
| `spartito roles` | available roles |

## Command reference

`spartito <command> --help` shows the same options. Agent ids are those printed by `spartito spawn` and
listed by `spartito ps`; the top-level orchestrator's id is `orchestrator`.

### Projects

```sh
spartito project add [PATH] [--name NAME] [--provider claude|codex]
spartito project list
spartito project rm NAME
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
spartito chat [--continue|-c] [--here] [--model MODEL] [--effort EFFORT]
spartito web [--host 127.0.0.1] [--port 8787]
spartito mcp
```

- `chat` opens the orchestrator as an interactive Claude Code session (requires `claude`). It
  runs in `~/.orchestrator/workspace/` unless `--here` is given; `--continue` continues the
  last conversation there. Model and effort default to the global Claude setting of the
  `orchestrator` tier. The session gets the `orch` MCP tools and may run `spartito …` commands
  through Bash. It is separate from the dashboard's orchestrator agent but sees the same state.
  Being interactive, it is never woken by notifications: use `wait_for_agents` there.
- `web` serves the [dashboard](#dashboard). With a non-loopback `--host` it prints a warning:
  the dashboard has no authentication.
- `mcp` runs the `orch` MCP server on stdio. `spartito chat` and the agents with a lead role
  start the same server themselves, as `<python> -m orchestrator.mcp_server` (not through this
  command); there is no need to run it by hand.

### Dashboard window, menu bar and background service

```sh
spartito app [--port PORT] [--agent ID | --decision N]
spartito app install | uninstall
spartito menubar
spartito service install [--host 127.0.0.1] [--port 8787]
spartito service status
spartito service logs [--lines|-n 20]
spartito service uninstall
spartito notices [--limit|-n 20] [--verbose|-v] [--json]
```

- `app` shows the dashboard in its own window: a native one with the extra `app`, else a
  Chromium-family browser in app mode, else the default browser. It uses the running
  `spartito web` (or service) of this home, or serves the dashboard on `127.0.0.1` (a free port,
  or `--port`) until the window is closed. One window per home: a second `spartito app` brings it
  to the front, at `--agent` or `--decision` when given.
- `app install` adds a launcher with Spartito's name and icon (macOS:
  `~/Applications/Spartito.app`, which also posts notifications itself; Linux: a desktop
  entry); `app uninstall` removes it.
- `menubar` (macOS, extra `menubar`) shows the running agents and open decisions of this home
  in the menu bar; its menu opens a decision or `spartito app`. One item per home.
- `service install` runs `spartito web --host … --port …` in the background, started at login and
  restarted when it stops (macOS: launchd agent; Linux: systemd user unit). Running it again
  replaces the unit and restarts the service. `status` shows whether it is installed and
  running and whether the dashboard answers; `logs` shows where its output goes (on macOS
  also the last lines); `uninstall` stops and removes it.
- `notices` lists the recent desktop notifications of this home, newest first, one line each:
  its number, when it was recorded, its kind, the way it was shown (`window-system`,
  `window-banner`, `window-count`, `launcher-helper`, `page`, `linux`, `osascript`, `waiting`,
  `dropped`), the process that showed or last moved it (`<role>:<pid>`), when, and the reason
  of its final outcome (for an older finished notice of many that waited: `dropped`, counted
  in the text of a newer one; `window-count`: counted in the window's banner, not shown in
  one). `-v` adds each notice's text and steps, `--json` gives all of it.
  How to read it: [desktop.md](desktop.md#reading-the-trace).

Details: [desktop.md](desktop.md).

### Starting and talking to agents

```sh
spartito spawn TASK -p PROJECT [-n NAME] [-r ROLE] [--provider P] [--model M] [--effort E]
           [--worktree|--no-worktree] [--share AGENT] [--base REF_OR_AGENT]
           [--after AGENT_OR_RUN ...] [--permission MODE]
spartito ask PROJECT MESSAGE
spartito send AGENT MESSAGE [--after AGENT_OR_RUN ...]
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
spartito spawn -p my-app -r planner "Plan the password reset flow."
spartito spawn -p my-app -n api --after planner-9c1e "Implement this plan: {{result:planner-9c1e}}"
spartito spawn -p my-app -r reviewer --provider codex --share api-b7d0 --after api-b7d0 "Review the change."
spartito send api-b7d0 "Address the review findings: {{result:reviewer-41aa}}"
spartito ask my-app "Add rate limiting to the login endpoint."
```

The text forms `{{result:<agent-id>}}` and `{{result:<agent-id>:full}}` are described in
[agents.md](agents.md#dependencies-and-results).

### Following agents

```sh
spartito ps [-p PROJECT] [--archived|-a]
spartito status AGENT
spartito logs AGENT [--follow|-f] [--tail|-n 50]
spartito wait AGENT... [--timeout 3600]
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
spartito withdraw RUN_ID
spartito edit RUN_ID MESSAGE
spartito stop AGENT
spartito attach AGENT
```

- `withdraw` removes a queued message or compaction before it starts; `edit` replaces a queued
  message's text. Both are refused once the turn has started. A compaction can be withdrawn but
  not edited; a notification turn can be neither (its text is written when it starts).
- `stop` stops the agent's running and queued turns; the agent and its worktree stay.
- `attach` continues the agent's session interactively (`claude --resume` or `codex resume`)
  in its working directory. The agent must be idle.

### Model, effort and compaction of one agent

```sh
spartito model AGENT [--model MODEL] [--effort EFFORT]
spartito compact AGENT [INSTRUCTIONS]
```

- `model` without options shows the effective, own and inherited values; with options it sets
  the agent's own value (`""` clears it). The change applies from the next turn. In an existing
  session a model change starts a new prompt cache (the next turn reads the whole conversation
  at full price); an effort change keeps the cache with Codex and may start a new one with
  Claude. The orchestrator can be set too (`spartito model orchestrator --effort high`).
- `compact` queues a compaction of the agent's conversation. See
  [agents.md](agents.md#context-and-compaction).

A model name can be any single word: names with whitespace or control characters, a leading
`-`, or more than 200 characters are refused (`spartito spawn`, `spartito config set`, `spartito model`
and the MCP `spawn_agent` check this).

### Archive

```sh
spartito archive AGENT [--yes|-y]
spartito archive --safe [-p PROJECT]
spartito restore AGENT
spartito rm AGENT [--delete-branch]
```

- `archive` hides an agent from `spartito ps`, the dashboard and `list_agents`; its record, turns,
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
spartito board [-p PROJECT] [--state|-s STATE] [--live]
spartito board add TITLE -p PROJECT [--description|-d TEXT] [--state|-s idea]
spartito board edit ITEM_ID [--title T] [-d TEXT] [-s STATE] [--agent AGENT] [--branch BRANCH]
spartito board move ITEM_ID up|down
spartito board dispatch -p PROJECT
spartito board dispatch ITEM_ID...
spartito board show ITEM_ID
spartito board link ITEM_ID DECISION_ID [--remove]

spartito decisions [--all|-a]
spartito decide DECISION_ID ANSWER
```

States: `idea`, `ready`, `in_progress`, `done`. `spartito board` without `-p` lists every
project's board. In `board edit`, `""` clears `--agent` or `--branch`. `decide` takes an
option number or free text. `--live`, `show`, `link` and dispatching named items:
[board.md](board.md#command-line-mcp-and-api). See [agents.md](agents.md#task-board) and
[agents.md](agents.md#decisions).

### Settings and roles

```sh
spartito config show [-p PROJECT]
spartito config set TIER [--provider claude] [--model M] [--effort E] [-p PROJECT]
spartito config compact-at TOKENS [-p PROJECT]
spartito config archive-after HOURS [-p PROJECT]
spartito config keep-awake on|off
spartito config desktop-notifications on|off
spartito roles
```

See [configuration.md](configuration.md).

## Dashboard

`spartito web` serves a single page on `http://127.0.0.1:8787`. It reads the same database as the
command line, so both can be used side by side.

**Left column.** **Maestro** (the orchestrator) at the top; then each project with its agents
as a tree, and the **Archive** entry when something is archived. A project's row has buttons
to talk to its lead, start a new agent, open its task board, archive every finished agent
whose work is safe, and remove the project. **+** next to *Projects* adds a project through a
folder picker that marks git repositories. The header has the **Decisions** button (with the
number of open decisions), the **Models and effort** settings and the theme switch. The
summary at the bottom left counts the listed agents, but its tokens and cost are totals over
every run ever recorded, archived agents included, and it keeps what agents deleted for good
(`spartito rm`, or **Delete** in the archive) had spent. An agent deleted by an older version
of Spartito running on the same database still takes its spend out of the total.

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
from the page itself (same origin, JSON body), so other websites cannot write to it. Binding
it to another address (for example `--host 0.0.0.0`) gives anyone who can reach it control
over agents that run code.

**Installing.** The page has a web app manifest: Chrome offers Install in the address bar,
Safari **File › Add to Dock** ([desktop.md](desktop.md#installable-page)).

**JSON API.** The page uses a small JSON API under `/api/` (see `web/app.py`). Calls that are
convenient from scripts:

- `GET /api/ping`: `{"tool": "orchestrator", "version": "…", "home": "…"}`, where `home` is a
  digest of the resolved `ORCH_HOME`, never the path.

- `POST /api/agents/<id>/model` with `{"model": "...", "effort": null}`: `null` clears a value,
  a missing key leaves it unchanged.
- `POST /api/agents/<id>/compact` with `{"instructions": "..."}`.

## Upgrading from `orch`

The command used to be `orch`; it is now `spartito`, and `orch` is gone (there is no alias).
Everything else keeps its name: the home `~/.orchestrator` with `orch.db`, the `ORCH_*`
variables, the `orch` MCP server and its `mcp__orch__…` tools, the agents' `orch/` branches
and the Python package `orchestrator`.

A command's script is written when the tool is installed. Until it is installed again, an
existing installation keeps its `orch`, which runs the new code and warns on every call, and
has no `spartito`. Once the checkout has the new code, from the checkout:

```sh
uv tool install --editable '.[app,menubar]'   # the extras you installed before; no --force
spartito --help
```

This removes `orch` and adds `spartito`. In a checkout used with `uv run`, `uv sync` does the
same for `.venv`.

- **Running agents**: nothing to do. A turn running during the reinstall finishes, and agents
  created before take new messages as before: they reach the tool through its MCP server,
  started as `<python> -m orchestrator.mcp_server`, never through the command.
- **Background service and launcher**: they need no reinstall. Both run
  `<python> -m orchestrator` of the tool's environment, whose path the reinstall keeps, and
  the launcher's copy of Python stays current. A dashboard that is already running keeps the
  code it started with while the page comes from the new code, so restart it: for the service,
  `spartito service install` (with the `--host` and `--port` you used) restarts it and
  rewrites its unit; `spartito web` or `spartito app` started by hand: stop it and start it
  again. `spartito app install` only refreshes the comments of the launcher's script.
- **An open `orch chat` session** was allowed `Bash(orch *)`, and `orch` no longer exists.
  Leave it and continue it with `spartito chat --continue`, which allows `Bash(spartito *)`.
  Its conversation still shows `orch …` commands: if it tries one, tell it the new name.
- **Your own scripts, aliases and notes** that call `orch` need the new name.
