# Configuration

- [Model and effort](#model-and-effort)
- [Settings](#settings)
- [Loadout](#loadout)
- [Roles](#roles)
- [Providers and permissions](#providers-and-permissions)
- [Environment variables](#environment-variables)
- [Files](#files)

Settings are stored in the database (`~/.orchestrator/orch.db`). `spartito config show` prints
the global settings with their defaults, `spartito config show -p <project>` a project's
overrides. Every setting can also be changed in the dashboard's **Models and effort** window.

## Model and effort

### Tiers

Model and effort are configured per tier and per provider (`claude`, `codex`), because model
names and effort levels differ between providers:

| tier | agents |
|---|---|
| `orchestrator` | the top-level orchestrator (Maestro), and `spartito chat` |
| `lead` | project leads (every role with `lead: true` inside a project) |
| `worker` | every other role: worker, planner, reviewer, tester, custom roles |

```sh
spartito config set orchestrator --model opus --effort high
spartito config set lead --model sonnet --effort medium
spartito config set worker --model haiku --effort low
spartito config set worker --provider codex --model gpt-6-luna
spartito config set worker --model sonnet -p my-app     # only in my-app
spartito config set worker --model ""                   # clear the global worker model
```

Effort levels: Claude `low`, `medium`, `high`, `xhigh`, `max`; Codex `low`, `medium`, `high`,
`xhigh`.

### Precedence

For each turn, the first value found applies:

1. the agent's own value (`spartito spawn --model/--effort`, the dashboard's new-agent form,
   `spartito model`, the agent header in the dashboard);
2. the project's setting for the agent's tier and provider;
3. the global setting;
4. the role's `model` frontmatter field (model only);
5. nothing: the CLI's own default.

Values are resolved when a turn starts, so a changed setting applies to existing agents from
their next turn. `spartito chat` uses the global Claude setting of the `orchestrator` tier;
`--model` and `--effort` override it.

### One agent

```sh
spartito model fix-login-3f2a                         # effective, own and inherited values
spartito model fix-login-3f2a --model opus --effort high
spartito model fix-login-3f2a --model ""              # clear: the tier setting applies again
spartito model orchestrator --effort high
```

A running turn keeps its model. In an existing session, a model change starts a new prompt
cache, so the next turn reads the whole conversation at full price; an effort change keeps the
cache with Codex and may start a new one with Claude. Codex switches models in a resumed
session too (`codex exec resume -m`).

A model name is any single word of at most 200 characters without control characters and not
starting with `-`.

## Settings

| key | command | scope | default |
|---|---|---|---|
| tier values (model and effort) | `spartito config set <tier>` | global, project | none: the CLI's default |
| `compact_at_tokens` | `spartito config compact-at <tokens>` | global, project | `256000` |
| `archive_after_hours` | `spartito config archive-after <hours>` | global, project | `24` |
| `keep_awake` | `spartito config keep-awake on\|off` | global | `on` |
| `desktop_notifications` | `spartito config desktop-notifications on\|off` | global | `on` |

A project value overrides the global one. An empty value (`""`) clears a setting, so that the
global value or the default applies; `0` or `off` turns compaction and archiving off.

### Automatic compaction: `compact_at_tokens`

```sh
spartito config compact-at 256k            # also 256000, 1M; 0 or off: never
spartito config compact-at 400k -p my-app
spartito config compact-at "" -p my-app    # inherit the global value again
```

When an agent's turn ends successfully with its context above the threshold, a compaction is
queued ahead of any message already waiting, so it runs before the agent's next turn and
never in the middle of one. The new value takes effect at the end of the next turn. Above the
threshold, the context is shown in a warning colour (without automatic compaction: from 80% of
the window). How compaction works, and when automatic compaction pauses:
[agents.md](agents.md#context-and-compaction).

### Automatic archiving: `archive_after_hours`

```sh
spartito config archive-after 48           # 0 or off: never
spartito config archive-after off -p my-app
```

Archives idle agents whose work is safe after that many idle hours. Leads and the
orchestrator are never archived automatically. Details: [agents.md](agents.md#archiving).

### Keep-awake: `keep_awake`

While any agent's turn is running (queued and idle agents do not count), the machine does not
sleep on idle: on macOS `caffeinate -i -w <runner pid>`, on Linux `systemd-inhibit
--what=idle`. The inhibitor ends with the runner process, also after a crash. It prevents idle
sleep only: the display may still sleep, and it does not prevent sleep from closing a laptop
lid without an external display, a forced sleep, or a Linux desktop environment that
suspends on its own timer.

### Desktop notifications: `desktop_notifications`

A notification is shown when a decision starts waiting for you, and when an agent whose
latest message came from you has finished: its turn ended and neither it nor any of its
descendants has a running or queued turn. So Maestro notifies you once its agents are done; an
agent started by a lead does not notify you.

Each notification is recorded as a notice in the database (kept for one day). Who shows it,
the first of these that is there:

- on macOS, Spartito's open window: it posts the notice when *Spartito* is allowed (a click
  opens the agent or decision), else shows it inside the window (a banner, the Dock's badge);
- with no window that may post, the launcher's app, once *Spartito* is allowed: started
  hidden, it posts the notice;
- an open dashboard page (a tab, the installed app, or the browser window of `spartito app`) that
  is allowed to show notifications: the **Enable notifications** button in its header, or
  **Models and effort** › **Notifications** › **Allow in this browser**. It shows them as
  Web Notifications, and a click opens the agent or the decision. It counts while it has
  polled within the last 15 seconds; on Linux it comes first, then `notify-send`. The native
  window of `spartito app` is not such a page: it has no **Enable notifications** button, and
  Spartito shows the notifications itself there (on Linux with `notify-send`);
- with a launcher that may post or a window, but none of these able to take it now: the
  notice waits, and the window shows it when it next opens;
- otherwise, on macOS, `osascript` shows a plain notification, whose text ends with where to
  look (`— open Spartito to answer`, `— open Spartito to see it`), as its click cannot open
  Spartito.

`spartito notices` lists how the recent ones were shown, by which process, and why.
Off, nothing is recorded and nothing is shown. Details, including hidden tabs, the launcher and
Linux: [desktop.md](desktop.md#notifications-with-click-through).

### Review rounds

The number of review rounds is not a setting. The packaged `lead` role prompt allows at most
two review rounds per branch (the review and one re-review of the fixes); after that the lead
reports the findings still open, with their severity, instead of sending them back. To change
this, override the `lead` role (see [Roles](#roles)).

## Loadout

Without restrictions every agent would inherit your whole Claude Code or Codex configuration
(claude.ai connectors, plugins, skills, MCP servers), which multiplies the size of every
request. Each role therefore declares what its CLI loads besides the role prompt. By default,
agents load only the `orch` MCP server (leads), the project's own `CLAUDE.md` and `.claude/`
settings, and a fixed set of built-in tools; skills and slash commands are off and Codex
ignores `~/.codex/config.toml`. If you rely on `~/.claude/settings.json` or on Codex's
`model_provider` or profiles, see
[Authentication and model settings](#overrides-loadouttoml) below.

### Keys

| key | values | CLI flag |
|---|---|---|
| `tools` | comma-separated built-in tools; `default` = the CLI's default set; `none` = no built-in tools. MCP tools are not affected. | `--tools` |
| `mcp` | `strict` = only the `orch` server (lead roles); `inherit` = also user, project, plugin and claude.ai connector servers | `--strict-mcp-config` |
| `setting_sources` | `project,local` = without the user level (user plugins, skills, commands, hooks), keeping the project's `CLAUDE.md` and `.claude/`; `user,project,local` = everything | `--setting-sources` |
| `slash_commands` | `false` = no skills or slash commands at all, the project's included | `--disable-slash-commands` |
| `codex_user_config` | `false` = Codex does not read `$CODEX_HOME/config.toml` (the login is kept) | `--ignore-user-config` |
| `memory` | `true` = also `Write` and `Edit`, but only into the session's own memory directory (`~/.claude/projects/<project>/memory/`) | `--settings` (a PreToolUse hook) |

Codex uses only `codex_user_config`; the other keys apply to Claude.

### Defaults

The packaged roles set `mcp = strict`, `setting_sources = project,local`,
`slash_commands = false` and `codex_user_config = false`, and these tools:

| role | `tools` |
|---|---|
| orchestrator, lead | `Bash,Read,Glob,Grep` (plus the `orch` MCP tools), `memory = true` |
| worker, tester | `Bash,Read,Edit,Write,Glob,Grep` |
| reviewer | `Bash,Read,Glob,Grep` |
| planner | `Bash,Read,Glob,Grep,WebSearch,WebFetch` |

The packaged roles have no `Skill` tool, so skills and slash commands are off for them. A role
that should use the project's skills or commands needs `slash_commands = true` and `Skill` in
its `tools`.

A custom role that sets none of the keys gets `tools = default` (including `Skill`),
`mcp = strict`, `setting_sources = project,local`, `slash_commands = true`,
`codex_user_config = false`. An empty value (`tools:`) is an error.

### Overrides: `loadout.toml`

Keys can be overridden without copying a role file, in `<project>/.orch/loadout.toml` and
`~/.orchestrator/loadout.toml`, with a `[default]` table (every role) and `[<role>]` tables.
For each key the first hit wins:

1. project `[<role>]`
2. project `[default]`
3. home `[<role>]`
4. home `[default]`
5. the role's frontmatter
6. the defaults above

A malformed file or value makes the turn fail with the file and the error named. A table for
an unknown role (for example a misspelt `[wroker]`) is ignored and reported as an `info`
event.

**Authentication and model settings.** Dropping the `user` setting source drops all of
`~/.claude/settings.json`, not only plugins: also `env` (for example Bedrock, Vertex or proxy
routing), `apiKeyHelper`, `model`, permissions and hooks. With Codex,
`codex_user_config = false` also drops `model`, `model_provider` and profiles from
`config.toml`. If you rely on these:

```toml
# ~/.orchestrator/loadout.toml
[default]
setting_sources = "user,project,local"   # Claude: ~/.claude/settings.json applies again
codex_user_config = true                 # Codex: ~/.codex/config.toml applies again
```

A model can also be set with `spartito config set` instead.

**User-level plugins and skills.** A project that needs them turns the three layers back on;
`tools` is set per role, so `Skill` must be added to every role that uses skills:

```toml
# <project>/.orch/loadout.toml
[default]
setting_sources = "user,project,local"   # user plugins, skills, commands
mcp = "inherit"                          # plugin, user and connector MCP servers
slash_commands = true

[worker]
tools = "Bash,Read,Edit,Write,Glob,Grep,Skill"

[tester]
tools = "Bash,Read,Edit,Write,Glob,Grep,Skill"

[lead]
tools = "Bash,Read,Glob,Grep,Skill"
```

### Notes

- The flags are passed on every turn, resumed ones included.
- `spartito chat` uses the `orchestrator` role's loadout, with only the home-level
  `loadout.toml` applied. `Bash` is always kept (for `spartito` commands), and slash commands stay
  on so that `/compact`, `/clear`, `/model` and `/context` work; `slash_commands` affects only
  headless agents.
- With Codex and `codex_user_config = false`, the model, MCP servers and plugins of the user
  configuration are not loaded (the model is Codex's default unless set in Spartito), and the
  lead roles' `orch` tools run without approval.
- **`memory`** keeps Claude Code's memory (one file per fact and a `MEMORY.md` index) usable
  for the orchestrator and the leads; a lead's memory is the project's, shared with your own
  interactive sessions there. Only `Write` and `Edit` are confined, not `Bash`. It has no
  effect when `tools` already grants a writing tool (`default` does), in `spartito chat`, or with
  Codex. It takes effect only when the session's hooks are proven to run: the prompt is sent
  after a marker file written by the hook appears. If hooks are disabled (`disableAllHooks`,
  the policy `allowManagedHooksOnly`), the turn restarts without `Write`/`Edit`; this is
  remembered per agent in `~/.orchestrator/hooks-off/<agent>.json` and retried in a new
  session or after a day, with an `info` event on each change. If the CLI exits before the
  marker appears (for example an expired login), the turn fails with the CLI's error. If the
  memory directory cannot be located reliably (a symlinked `.git`, environment variables that
  move it), every write is refused; with `autoMemoryDirectory` only the default directory is
  writable.
- A session started before a loadout change keeps the earlier tool, skill and MCP lists in its
  conversation history, so its turns stay large; the savings show in full for new agents.

## Roles

A role is a Markdown file: frontmatter, then the prompt. Lookup order, first hit wins:

1. `<project>/.orch/roles/<name>.md`
2. `~/.orchestrator/roles/<name>.md`
3. the packaged roles (`src/orchestrator/roles/`)

A file with the name of a packaged role overrides it completely: copy the packaged file and
change it, rather than writing only the keys that differ. `spartito roles` lists the packaged roles
and those in `~/.orchestrator/roles/` (whether each is a lead role, and whether it works in a
worktree or the checkout); it has no project option and does not list roles under
`<project>/.orch/roles/`. Those are still used by `spartito spawn -p <project> -r <name>` and are
listed for agents by the MCP tool `list_roles(project)`.

### Frontmatter

```markdown
---
description: Writes and updates API documentation
lead: false
worktree: true
provider: claude
model: sonnet
output_schema: none
tools: Bash,Read,Edit,Write,Glob,Grep
---
You are a documentation agent. ...
```

| key | meaning | default |
|---|---|---|
| `description` | one line shown in listings | empty |
| `lead` | `true` gives the agent the `orch` MCP tools so it can start agents | `false` |
| `worktree` | `false` makes the agent work in the main checkout by default | `true` |
| `provider` | default provider for the role | the project's |
| `model` | default model, below the tier settings in precedence | none |
| `output_schema` | file name of a JSON Schema for the final answer; `none` = free text | `none` |
| loadout keys | see [Loadout](#loadout) | see there |

### Output schemas

`output_schema` names a JSON Schema file (a file name, not a path), looked up in the same
directories as roles, so a project can override a packaged schema too. It is the role's
default when the agent is started or messaged through the MCP tools (`spawn_agent`,
`send_message`); the caller can pass its own `output_schema` or ask for prose with
`freeform: true`. The command line and the dashboard always get prose. The packaged worker,
planner, reviewer and tester roles have compact schemas; the lead and orchestrator answer in
prose.

The schema must be strict enough for both Claude's `--json-schema` and Codex's
`--output-schema`: every property listed in `required`, `additionalProperties: false`, and an
optional value expressed as nullable or as an empty list.

## Providers and permissions

A new agent's provider is, first hit wins: `--provider` (or `provider` in `spawn_agent`, or
the dashboard's form), the role's `provider`, the project's default (`spartito project add
--provider`, default `claude`). The orchestrator agent uses the `provider` of the
`orchestrator` role (an override in `~/.orchestrator/roles/orchestrator.md`), otherwise
`claude`; its provider is fixed when it is first created (by its first message, or by setting its model or
effort). `spartito chat` always
runs Claude Code.

Claude agents run with `--permission-mode auto`, Codex agents in the `workspace-write`
sandbox. `spartito spawn --permission <mode>` sets another mode for one agent.

A new agent CLI can be added by implementing the `Provider` protocol
(`src/orchestrator/providers/base.py`) and registering it in `providers/__init__.py`, or from
outside with `ORCH_PROVIDERS="name=package.module:Class"`.

The building blocks for multi-step pipelines (planning, breakdown, implementation, review,
test) are `--after`, `{{result:…}}`, `--base`, shared worktrees and output schemas; there is
no declarative pipeline layer.

## Environment variables

| variable | meaning |
|---|---|
| `ORCH_HOME` | state directory (default `~/.orchestrator`) |
| `ORCH_PROVIDERS` | extra providers, `name=package.module:Class`, comma-separated |
| `ORCH_NOTIFY_DELAY` | seconds a notification turn waits to collect results (default 5) |
| `ORCH_DESKTOP_NOTIFICATIONS` | `off` (or `0`, `false`, `no`): no desktop notification by any path; notices are still recorded, with the reason, in `spartito notices` ([desktop.md](desktop.md#notifications-with-click-through)). For CI and manual runs of a checkout |
| `ORCH_TURN_HOME` | set by the runner for an agent's turn: the home of the instance it belongs to. A process with it that works on another home shows no desktop notification |
| `CODEX_HOME` | read for Codex rollout files and `config.toml` (Codex's own variable) |
| `PYWEBVIEW_GUI` | the pywebview backend of `spartito app` on Linux (pywebview's own variable; default Qt) |

The background service (`spartito service install`) keeps only `PATH`, `LANG`, `LC_*`, `ORCH_*`
(not `ORCH_AGENT_ID`, `ORCH_TURN`, `ORCH_TURN_HOME` or `ORCH_DESKTOP_NOTIFICATIONS`),
`CLAUDE_CONFIG_DIR` and `CODEX_HOME` from the shell it is installed from; run the install
again after changing them. See [desktop.md](desktop.md#background-service).

## Files

The main files under `~/.orchestrator/` (or `$ORCH_HOME`):

| path | contents |
|---|---|
| `orch.db` | the SQLite database: projects, agents, turns, events, settings, board, decisions, notices |
| `worktrees/<project>/<agent-id>/` | agents' worktrees |
| `workspace/` | the orchestrator's working directory |
| `roles/`, `loadout.toml` | your roles and loadout overrides |
| `logs/run-<id>/` | per turn: the CLI's output, stderr, `result.md`, `result.json` |
| `logs/web.log` | output of the background service (macOS; emptied at install above 5 MB) |
| `hooks-off/` | agents whose memory hook did not run (see [Loadout](#notes)) |
| `run/web.json` | the running dashboard server: pid, host, port, start time |
| `run/app.lock` | held by the open `spartito app` window |
| `run/page-notifier` | touched by a dashboard page that shows notifications |
| `run/app-notifier`, `run/app-notifier.json` | the open native window's pid, start time and notification state |
| `run/app-notify.json` | the launcher's app that may post notifications |
| `run/notices/` | notices the busy database did not take yet (recorded by the next helper or poll) |
| `logs/notices.log` | one line per step of each notification (see `spartito notices`) |
| `run/app-profile/` | the browser profile of `spartito app` in Chromium app mode |
| `run/app-webview/` | the native window's web storage (Linux; macOS WebKit keeps its own) |
