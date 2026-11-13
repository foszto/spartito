# Command line and dashboard

- [Commands at a glance](#commands-at-a-glance)
- [Command reference](#command-reference)
- [Dashboard](#dashboard)
- [Upgrading](#upgrading)
- [Upgrading from `orch`](#upgrading-from-orch)
- [Upgrading to the distribution name `spartito`](#upgrading-to-the-distribution-name-spartito)

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
| `spartito cost` | spend of the recorded turns per project, day or agent |
| `spartito budget …` | budgets: list, set, remove |
| `spartito state backup/list/restore` | back up the state, list and restore backups |
| `spartito --version`, `spartito version [--check]` | the version, the kind of install, a check for a newer release |
| `spartito upgrade` | prepare an upgrade: backup, the command for this install, what to restart |
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
spartito config bot-coauthor on|off
spartito roles
```

See [configuration.md](configuration.md).

### Cost and budgets

```sh
spartito cost [--by project|day|agent] [--since YYYY-MM-DD] [-p PROJECT] [--json]
spartito budget [list]
spartito budget set [AMOUNT] [-p PROJECT] [--period day|month|total] [--warn-at PERCENT] [--hard-stop|--no-hard-stop]
spartito budget remove [-p PROJECT]
```

- `cost` reports what the recorded turns spent, grouped by project (the default), by day
  (local time) or by agent, with a total row: turns, prompt tokens (input with cache reads and
  writes), output tokens and cost. `--since` starts at local midnight of that day; `-p` keeps
  one project. It is *recorded usage*: turns run by very old versions may have no record, so
  the total can be slightly below the dashboard's summary. Codex reports no cost: its turns
  and tokens are listed, a row none of whose turns reported a cost shows `n/a`, and a cost
  marked `*` leaves such turns out (a footnote counts them). What agents deleted for good had
  spent has rows of its own: one per project (`my-app: removed agents`), one per agent
  (`fix-login-3f2a (removed)`), or one undated row by day, because the dates of its turns are
  not kept; `--since` leaves it out.
- `budget` (or `budget list`) lists the budgets: the global one and one per project, each
  with what is spent in its current period, the percentage of its amount, the threshold, the
  hard stop and the state (`ok`, `warning`, `reached`).
- `budget set` creates or changes the global budget, or a project's with `-p`. A new budget
  needs `AMOUNT` (USD) and defaults to `--period month`, `--warn-at 80` and no hard stop; for
  an existing one, every value not given stays as it was, so `budget set 20` raises the amount
  only and `budget set --no-hard-stop` turns the stop off.
- `budget remove` removes the global budget, or a project's with `-p`.

How budgets count, warn and stop: [configuration.md](configuration.md#budgets).

### State backup and restore

```sh
spartito state backup [--to DIR] [--logs]
spartito state list [--json]
spartito state restore BACKUP [--yes|-y] [--logs]
```

- `backup` copies the database with SQLite's online backup API (safe while agents run),
  checks the copy with `PRAGMA integrity_check`, and adds the home's `roles/` and
  `loadout.toml` (symbolic links left out); `--logs` adds `logs/`. It goes into a new directory
  in the home's `backups/`, named after the UTC time, or into `--to DIR` (not inside a
  directory it copies); directories 0700, files 0600. It
  prints where the backup is and a warning: a backup holds prompts, answers and tool output,
  which can include secrets.
- `list` shows the backups in `backups/`, database files put there by hand (`*.db`) included,
  newest first: name, time, size, and whether the logs are in it.
- `restore` takes a name from `list`, or the path of a backup directory or database file. It
  refuses a file that is not an intact state database and any restore while a turn is running
  or queued. Without `--yes` it only says what it would do and exits with 1. It keeps the
  current state as a new backup (`<time>-before-restore`), complete and printed before the
  replace commits, and replaces the database's content in place under one write lock, so
  nothing another process commits is lost, then copies the
  backup's `roles/` and `loadout.toml` (with `--logs` also its `logs/`) over the home's,
  leaving symbolic links as they are. Tables and columns of a newer version's backup that this
  version does not have are not restored. Restart a running dashboard afterwards.

Details, and moving a home to another directory or machine: [state.md](state.md).

### Version and upgrades

```sh
spartito --version
spartito version [--check]
spartito upgrade [--dry-run]
```

- `--version` prints `spartito <version>` and exits.
- `version` prints the version and the kind of install: `editable checkout` with the
  checkout's directory, or `installed package` (with `(uv tool)` for a `uv tool install`).
  `--check` asks PyPI whether a newer release exists and says one of: up to date, a newer
  release (which one), this install is newer than the latest release, or that `spartito` is not
  published on PyPI (the index's 404 is an answer, exit code 0). A network failure or an
  answer it does not understand is a one-line error with exit code 1. The request is
  `GET https://pypi.org/pypi/spartito/json` with the header `User-Agent: spartito` and no other
  header of Spartito's own (no version, platform, path or home), a 10-second timeout, at most
  512 KB read and no redirect followed. Versions are compared as PEP 440 orders the plain
  cases: by their release numbers (`0.10.0` is newer than `0.9.1`), then
  dev < a < b < rc < final < post, each with its number (`rc1` < `rc2`, `post1` < `post2`);
  a `+local` part is ignored.
- `upgrade` prepares an upgrade and prints how to do it; it runs no package manager and no
  git. It refuses while any turn is queued or running (as `state restore` does; a turn whose
  runner is gone does not count), backs the state up into the home's `backups/` (as
  `state backup`) and prints the backup and the command that restores it, then the kind of
  install with the command that upgrades it, and what to restart afterwards. `--dry-run`
  prints the same plan without a backup and lists the active turns instead of refusing.
  The steps around it: [Upgrading](#upgrading).

**No automatic check.** Spartito makes no network request of its own: nothing checks for
updates in the background, at start or on a schedule, and the dashboard does not either.
`spartito version --check` is the one request, and only when you type it.

## Dashboard

`spartito web` serves a single page on `http://127.0.0.1:8787`. It reads the same database as the
command line, so both can be used side by side.

**Left column.** **Maestro** (the orchestrator) at the top; then each project with its agents
as a tree, the **Archive** entry when something is archived, and the **Cost** entry. A
project's row has buttons
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

**Code view.** **Code** in the header of an agent with a worktree of its own shows its code in
place of the conversation, read-only, and loads it only when opened (**Refresh** loads it
again):

- **Changes**: the files changed against the agent's base (the branch, tag or commit it
  started from; when that no longer exists, the project's base branch, which the page then
  says), counted from the commit the branch shares with it, committed and uncommitted
  together, with added and removed lines; untracked files that are not ignored are listed as
  `new`. Opening a file loads its patch. A submodule is listed by its commit only.
- **Files**: the worktree's files as folders; a click shows a file's content. A submodule is
  named but not opened.

Only files git tracks or would track are shown, and only under a name that is, byte for
byte, both the one the file system has and the one git lists (no case or Unicode variant; on
macOS a name stored decomposed is not shown at all): an ignored file such as `.env` and
anything under `.git` never are. A symbolic link is shown as its target and never followed. Binary files are
named but not shown. Limits, each noted on the page when reached: 1,000 changed files, 5,000
files in the tree, 2,000 lines or 1 MB of one patch, 512 KB of one file, and 4 MB of any
list git gives. Nothing is edited, and neither the repository's nor a submodule's own diff,
textconv, filter or fsmonitor commands are run.

**Cost** (the entry at the bottom of the left column): the report of `spartito cost` with the
same three groupings (by project, by day in local time, by agent), a start day and a project,
and above it each budget with what is spent in its current period, its amount, the percentage
and its state (`ok`, `warning` or `reached`). Codex turns show their tokens and `n/a` for the
cost, with the same footnotes as the command. It is read-only: budgets are set with
`spartito budget set`. It loads when it is opened, when a choice changes and on **Refresh**,
never with the page's regular refresh.

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
- `GET /api/cost?by=project|day|agent&since=YYYY-MM-DD&project=<name>`: the cost report
  and the budgets with their state, as the **Cost** view shows them.
- `GET /api/agents/<id>/code/changes`, `…/code/patch?path=<path>`, `…/code/tree` and
  `…/code/file?path=<path>`: the code view (`web/code_api.py`). Besides the Host check, these
  refuse a request whose `Origin` or `Sec-Fetch-Site` names another site, GET included.

## Upgrading

The dashboard's settings show the installed version, and `spartito version` the version and
the kind of install; `spartito version --check` asks PyPI for a newer release. An upgrade has
three parts.

**Before.** Run `spartito upgrade` (or `spartito upgrade --dry-run` first, which changes
nothing). It refuses while any turn is queued or running: wait for them (`spartito ps` shows
no agent `running` or `waiting`) or `spartito stop` the agents. Then it backs the state up and
prints where, with the command that brings it back, for example:

```
backup: /home/dev/.orchestrator/backups/20261113T142530Z
  to go back: `spartito state restore 20261113T142530Z --yes`
```

**The upgrade.** `spartito upgrade` prints the command for the kind of install it found, with
paths quoted for the shell:

| install | upgrade |
|---|---|
| `uv tool install --python 3.14 'spartito[app,menubar]'` (a package from PyPI) | `uv tool upgrade spartito`: it keeps the extras and the Python of the install |
| a package installed with pip | `<python> -m pip install --upgrade spartito`, with the extras you installed (`'spartito[app,menubar]'`) |
| `uv tool install --editable '.[app,menubar]'` from a checkout | in the checkout: `git pull` (or update its branch as you do), then the same `uv tool install --editable` line with the extras the install has |
| a checkout used with `uv run` (Spartito runs in the checkout's own `.venv`) | in the checkout: `git pull`, then `uv sync` with the extras you synced with ([CONTRIBUTING.md](../CONTRIBUTING.md#setup)) |
| a checkout installed editable into another environment | in the checkout: `git pull`, then `<python> -m pip install -e .` with that environment's Python and the extras you installed (`uv pip install --python <python> -e .` where it has no pip) |

A package installed from a file or URL instead of PyPI is installed again from there. An
environment without the distribution `spartito` (an install from before it was renamed) takes
the steps of [Upgrading to the distribution name `spartito`](#upgrading-to-the-distribution-name-spartito).

**After.** Processes that started before the upgrade keep running the old code until they are
restarted. `spartito upgrade` lists the ones it finds for this home, with how to restart each:

- the background service, if it is installed: `spartito service install` again, with the
  `--host` and `--port` of its unit, which it prints; this restarts it;
- an open `spartito app` window (found by the lock it holds): quit it and open it again; its
  own dashboard server stops with it;
- the menu-bar item (found the same way): quit it and start `spartito menubar` again;
- a dashboard server recorded as running that is neither the service's nor the window's: a
  `spartito web` started by hand, to stop (Ctrl-C) and start again;
- the launcher, if it is installed: `spartito app install` again, which records the new
  version and the install's Python.

What it cannot see it names in words: an open `spartito chat` (leave it, and continue with
`spartito chat --continue`), and a `spartito web` started by hand that is not the last
dashboard server started (only that one is recorded). Then check the version with
`spartito version`, and in the dashboard's settings once the page is reloaded.

**Going back.** Install the previous version (for a package, for example
`uv tool install --python 3.14 'spartito[app,menubar]==0.1.0'`; for a checkout, check out the
earlier commit and install or sync as above), restart as above, and if the newer version
changed the state in a way you want undone, restore the backup:
`spartito state restore <name> --yes`.
Schema changes are additive, so an older version opens a database a newer one has used
([backups and versions](state.md#backups-and-versions)).

## Upgrading from `orch`

The command used to be `orch`; it is now `spartito`, and `orch` is gone (there is no alias).
Everything else keeps its name: the home `~/.orchestrator` with `orch.db`, the `ORCH_*`
variables, the `orch` MCP server and its `mcp__orch__…` tools, the agents' `orch/` branches
and the Python package `orchestrator`.

An install from before the rename is the tool `orchestrator` (`uv tool list`), whose
`orch` script runs the new code once the checkout has it, warns on every call, and has no
`spartito`. Replace it with the steps of
[Upgrading to the distribution name `spartito`](#upgrading-to-the-distribution-name-spartito):
`uv tool uninstall orchestrator` removes `orch` with the old tool, the next step installs
`spartito`, and the service and the launcher are installed again for the new environment.
Until then the command is `orch`: run `orch ps` in step 1. Do not run only
`uv tool install --editable` over it: that adds a second tool `spartito` and leaves `orch`,
the service and the launcher on the old environment.

- **An open `orch chat` session** was allowed `Bash(orch *)`, and `orch` no longer exists.
  Leave it and continue it with `spartito chat --continue`, which allows `Bash(spartito *)`.
  Its conversation still shows `orch …` commands: if it tries one, tell it the new name.
- **Your own scripts, aliases and notes** that call `orch` need the new name.

## Upgrading to the distribution name `spartito`

The checkout used to install itself as the Python distribution `orchestrator`; it is now
`spartito`, the name it has on PyPI ([releasing.md](releasing.md)). The command, the import
package `orchestrator`, `-m orchestrator`, the home and every internal name stay. uv names a
tool after its distribution, so `uv tool install --editable` now installs into a new tool
directory, `<tool dir>/spartito` (`uv tool dir` shows the tool directory), next to the old
`<tool dir>/orchestrator`, which still provides `spartito` (or the command's old name:
[Upgrading from `orch`](#upgrading-from-orch)). The background service and the
launcher name the old environment's Python, so they need a reinstall too.

Once the checkout has the new code, the old install keeps working; only `/api/ping` reports
the version `0+unknown`. Then, from the checkout:

1. Wait until no agent works: `spartito ps` shows no agent `running` or `waiting`. Leave open
   `spartito chat` sessions, and quit `spartito app` and `spartito menubar`.
2. `uv tool uninstall orchestrator`
3. `uv tool install --editable '.[app,menubar]'`, with the extras you installed before.
   To move to the package from PyPI instead: `uv tool install --python 3.14 'spartito[app,menubar]'`.
4. `spartito service install`, with the `--host` and `--port` you used: it rewrites the unit
   with the new environment's Python and restarts the dashboard.
5. `spartito app install`: a new copy of Python for the launcher, and its record.
6. Check: `uv tool list` shows `spartito` with the executable `spartito`;
   `spartito service status` shows that the web UI answers. Start `spartito menubar` again
   if you use it, and continue a chat with `spartito chat --continue`.

Keep steps 2 and 3 in this order. While the old tool provides `spartito`,
`uv tool install` stops with `Executable already exists: spartito` and changes nothing. With
`--force` it installs, but a later `uv tool uninstall orchestrator` then deletes the
`spartito` command that the new tool now owns, while `uv tool list` still lists it. If that
happened, run step 3 again with `--force`.

Between step 2 and steps 4 and 5, the processes started from the old environment cannot
start anything new, because its Python is gone:

- **A running turn** finishes normally. A turn its runner starts next (a queued message)
  fails at once with `FileNotFoundError` naming `…/tools/orchestrator/bin/python`: send the
  message again.
- **The dashboard service** still serves the page, but starting a turn fails with an error
  and leaves the agent `waiting` with no runner, and later messages queue behind that turn.
  Run `spartito stop <agent>`, then send again. If the service stops, launchd or systemd
  cannot restart it before step 4.
- **The launcher** names the removed environment: it reports that Python changed since
  `spartito app install`, does not open, and posts no notifications until step 5.
- **A Claude Code agent in a running turn** has an MCP server from the old environment: its
  `spawn` and `send` calls fail and leave the new turn `waiting` (`spartito stop`, then again),
  and writes to its memory directory are refused until the turn ends. Its next turn uses the
  new environment. The same holds for the MCP server of an open `spartito chat`.

In a checkout used with `uv run`, `uv sync` does the same for `.venv`: nothing else to do.
