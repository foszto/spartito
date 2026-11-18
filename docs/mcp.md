# MCP servers

> **Design: not built yet.** This page describes how Spartito would give agents MCP servers
> as capabilities. Nothing on it exists in the code today. Facts are marked
> **[documented]** (in the public Claude Code or Codex docs; sources at the end),
> **[observed]** (seen on one machine with Claude Code 2.1.322) or **[unverified]**.
> The owner decides the open questions before anything is built.

An *MCP server* gives a coding agent a set of tools, such as a ticket tracker's or a
database's. Today every Claude agent runs with `--strict-mcp-config` and only Spartito's own
`orch` server. Your own MCP servers, plugin servers and claude.ai connectors never reach
agents. The loadout's `mcp = inherit` would give all of them at once.

This design makes each MCP server a [capability](capabilities.md) of its own. You turn it on
and off at the same levels as `chrome`: globally, per project, per role and per agent. You can
also limit it to some of its tools.

- [The model](#the-model)
- [Discovery and status](#discovery-and-status)
- [Wiring per provider](#wiring-per-provider)
- [The MCP page, the command and the API](#the-mcp-page-the-command-and-the-api)
- [Adding and signing in](#adding-and-signing-in)
- [Safety](#safety)
- [Storage](#storage)
- [Phases](#phases)
- [Open questions](#open-questions)
- [Not verified yet](#not-verified-yet)
- [Sources](#sources)

## The model

### An MCP server entry

| field | what |
|---|---|
| `name` | the server's name, as Claude Code knows it (`jira`, `docs-db`); its tools are `mcp__<name>__<tool>` |
| `origin` | where the definition comes from: `spartito` (added in Spartito), `claude-user`, `claude-local` (one project), `claude-project` (a repository's `.mcp.json`), `plugin`, `connector` (claude.ai) |
| `transport` | `stdio` (a command), `http`, `sse`, `ws`, or `connector` |
| `definition` | stdio: `command`, `args`, `env`; remote: `url`, `headers`. Stored only for `origin = spartito`; the others are read from Claude Code's configuration when a turn starts |
| `reaches` | one line on what it can reach (see [Safety](#safety)) |
| `tools` | the allowlist: the tool names agents get, or none (all tools) |
| `pin` | for `claude-project` only: a hash of the definition's shape when the owner turned it on |

### Each server is a capability

**Recommendation:** every MCP server is a capability named `mcp:<name>` (`mcp:jira`), in the
same registry and with the same levels as `chrome`. A second registry with the same levels is
not recommended.

The existing code makes this cheap. These already work on capability names and need no
copy:

- the level resolution (`capabilities.resolve`)
- the no-escalation rules (`core/grants.py`)
- the settings keys `capabilities` and `role_capabilities`
- the agent column `agents.capabilities` and the per-turn record `run_meta.capabilities`
- the turn's log line `capabilities for this turn: …`
- `spartito capabilities …`, `spawn --capabilities`, and the MCP tools `spawn_agent`,
  `send_message`, `get_agent` and `list_agents`
- the JSON API and the dashboard's inherit / on / off controls

Three things change:

1. **Names.** `capabilities.NAME` also accepts `mcp:` followed by a server name (letters,
   digits, `_`, `-`, at most 64 characters). Older code reads such entries as unreadable,
   so for it they are off.
2. **A dynamic registry.** The static entries (`chrome`, plugins from `ORCH_CAPABILITIES`)
   are joined by entries built from the store and from discovery each time the registry is
   read. Every `REGISTRY` lookup goes through a function that takes the store.
3. **Wiring.** `Wiring` gets a field `mcp_servers` (name → definition) and a field
   `disallowed_tools`. A provider merges every turn's `mcp_servers` with the `orch` server
   into one MCP configuration (see [Wiring](#wiring-per-provider)). One `--mcp-config` flag
   per server would also work in principle, but merging is safer.

Every rule of [capabilities.md](capabilities.md) applies unchanged:

- the levels default → global → project → role → agent
- a role's off is final
- agents can give only what their own turn has
- only the owner or the orchestrator agent turns a server on globally, for a project or for
  a role

### Unsupported providers and servers

- A server that is on but that the agent's provider cannot carry is skipped with a note, as
  for `chrome` (`codex does not support it` until the Codex phase).
- claude.ai connectors and plugin servers are listed with their status, but turning one on
  is refused (`connector servers cannot be given to agents yet`) until phase 9. For
  connectors there is a way, observed once: one connector given alone as a copied entry (see
  [claude.ai connectors](#claudeai-connectors)). For plugin servers no way has been checked
  yet.

## Discovery and status

### What Spartito knows about, and from where

| origin | inventory | status |
|---|---|---|
| `spartito` | its own table | probe |
| `claude-user`, `claude-local` | `~/.claude.json` (`mcpServers` at the top level; per project under `projects.<path>.mcpServers`), honouring `CLAUDE_CONFIG_DIR` | `claude mcp list` |
| `claude-project` | `.mcp.json` in the project's main checkout | `claude mcp list` |
| `plugin` | names `plugin:<plugin>:<server>` in `claude mcp list` | `claude mcp list` |
| `connector` | names `claude.ai <Name>` in `claude mcp list`; the entry (`type`, `url`, `id`) only in the MCP status of a session without strict mode | `claude mcp list` |

The file locations and scopes are **[documented]**. Spartito reads these files only for the
names, the transport and the field *names*. It keeps no values. Lists and API answers show
definitions redacted, the way `claude mcp get` shows plugin servers: `env` as
`NAME=[REDACTED]`, header values hidden, URLs as origin only.

### Status: `claude mcp list`

What the CLI offers:

- `claude mcp list` and `claude mcp get NAME` have no `--json` option. **[observed]**
- `claude mcp list` prints one line per server with a status, such as `✔ Connected`,
  `! Needs authentication` or `✘ Failed to connect`. A failure in that list does not make
  the command fail. **[documented]**
- Statuses that come from configuration need no connection: `⏸ Pending approval`,
  `⊘ Disabled for this project` and `✘ Blocked by enterprise managed policy`.
  **[documented]**
- Approved servers are health-checked. Listing therefore starts stdio servers and connects
  to remote ones; unapproved `.mcp.json` servers are not started. **[documented]**
- Here it ran in about 10 seconds and exited 0. It listed only claude.ai connectors, about
  half of them connected and half needing authentication. **[observed]**

What Spartito does:

- It runs `claude mcp list` with the project's main checkout as the working directory,
  because local and project scope depend on the directory.
- It parses each line as `NAME: TARGET - STATUS` and reads the status by its leading mark.
  A line it cannot parse gives `unknown` for that server; a failed run gives `unknown` for
  all, with the reason.
- It keeps the result in a cache table and stores no failure detail beyond one line.
- It runs on demand: when the MCP page opens and the cache is older than 10 minutes, on
  **Check now**, and with `spartito mcp --refresh`. Never in a background loop: each run
  connects to every server you have.

States shown:

| state | from |
|---|---|
| `connected` | `✔` |
| `needs auth` | `!` (`Needs authentication`) |
| `failing` | `✘` (with the one-line detail, which Claude Code already redacts) |
| `pending approval` | `⏸`: a `.mcp.json` server not approved in Claude Code |
| `disabled` | `⊘`, or blocked by policy |
| `not configured` | a remote server with an empty URL |
| `unknown` | not checked yet, or the output could not be read |

### Tools and token cost: the probe

Spartito measures one server with a *probe*. The probe starts `claude -p` with
`--input-format stream-json --output-format stream-json`, in an empty temporary directory,
with the agent loadout's flags. It passes `--strict-mcp-config` and an MCP configuration
holding only that server, and sends no user message. Spartito already uses the same protocol
for the model list (`initialize`, **[observed]**, no model request). The probe sends three
control requests, then closes stdin:

1. `initialize`
2. MCP status: the SDK's `mcpServerStatus()`, which returns `name`, `status`
   (`connected`, `failed`, `needs-auth`, `pending`, `disabled`), `error` and `tools`
   (name, description and the read-only / destructive / open-world hints). **[documented]**
   for the SDK.
3. Context usage: the SDK's `getContextUsage()`, which returns `mcpTools` with `tokens` and
   `isLoaded` per tool, plus a deferred-tools category. Claude Code computes it with
   token-counting API requests, which are not in the message stream and not billed on the
   Anthropic API. **[documented]** for the SDK.

The CLI docs list neither request. Request 2 was observed with Claude Code 2.1.322, once,
with no user message and no model request **[observed]**:

- The request is `{"type": "control_request", "request_id": "…", "request": {"subtype":
  "mcp_status"}}` on stdin, after `initialize`.
- The answer is a `control_response` whose `response.mcpServers` holds one entry per server,
  with the keys `name`, `status`, `scope`, `source`, `transport`, `config`,
  `sessionProxied`, and for a connected server `serverInfo` and `tools`. A tool had the keys
  `name` and `annotations`, and no description.
- A server still connecting is `pending`; asking again a few seconds later gives its final
  status.
- Without a user message no `system`/`init` event arrives, so the probe relies on the
  control requests, not on the init event.

The wire name of request 3 is **[unverified]**: the SDK's source sends it as
`get_context_usage`, and it was not tried.

What the probe gives per server:

- the tool count and each tool's read-only or destructive hint (for the allowlist editor)
- a measured token cost, *listed* (`isLoaded`) and *deferred*

The cache keys on the definition's hash and the CLI version. A probe starts the server, so
it runs only on demand (**Measure**) and after a server is added. It never runs for a
`claude-project` server that is not approved.

**Fallback.** If the probe fails, the page shows the tool count from the last status and
`~N tokens (estimate)`. The estimate counts the characters of the tool names and
descriptions divided by 4, and is labelled as an estimate: schemas are not in the status, so
it is low. Spartito does not write an MCP client of its own. It could not reach an OAuth
server without handling the token, which Spartito must never do.

## Wiring per provider

### Claude

A turn with `mcp:docs-db` (stdio, from Spartito) and `mcp:jira` (remote, from your user
scope, limited to three tools):

    claude -p … --tools Bash,Read,Edit,Write,Glob,Grep,ToolSearch \
      --strict-mcp-config \
      --mcp-config <runtime dir>/mcp-<run>.json \
      --allowedTools mcp__orch \
      --disallowedTools mcp__jira__create_issue,mcp__jira__delete_issue,…

The file, mode `0600`, outside the logs, removed when the turn ends:

    {"mcpServers": {
      "orch":    {"command": "…", "args": ["-m", "orchestrator.mcp_server"], "env": {…},
                  "alwaysLoad": true},
      "docs-db": {"command": "docs-db-mcp", "args": ["--read-only"],
                  "env": {"DOCS_DB_URL": "${DOCS_DB_URL}"}},
      "jira":    {"type": "http", "url": "https://jira.example.com/mcp"}
    }}

**Strict mode stays.**

- `--strict-mcp-config` skips the servers of `.mcp.json`, `~/.claude.json` and plugins, and
  loads only `--mcp-config`. **[documented]**
- Without it, a `-p` run loads a repository's `.mcp.json` servers without asking.
  **[documented]**
- Without it, a repository's server takes precedence over your user-scope server of the same
  name. **[documented]**
- Allow and deny lists cannot fix this: their `serverName` entries are not a security
  control, by the docs' own account.

So every chosen server is copied into the turn's configuration.

**A file, not the command line.** A definition copied from your configuration can hold
literal secrets: `claude mcp add -e KEY=value` stores the value in `~/.claude.json`. A
command line is visible to every process on the machine, so the configuration goes into a
private file. Spartito does not store it, log it or show it. Configurations with the `orch`
server only stay inline, as today.

### claude.ai connectors

Observed once with Claude Code 2.1.322, through the control requests of the probe, with no
user message and no model request, in a headless session with the agents' flags
(`--setting-sources project,local`) **[observed]**:

- Without `--strict-mcp-config`, every claude.ai connector of the account is in the MCP
  status, with `scope` `claudeai`, `config.type` `claudeai-proxy` and the config keys
  `type`, `url` and `id`. Connected ones list their tools; the others are `needs-auth`.
- With `--strict-mcp-config`, and no `--mcp-config` or one with other servers only, no
  connector is present. Strict mode keeps connectors out of agents' turns, as wired today.
- With `--strict-mcp-config` and an `--mcp-config` holding one entry, a connected
  connector's `claudeai-proxy` entry copied as reported and under its reported name
  (`claude.ai <Name>`), that connector is the only server: `connected`, with `scope`
  `dynamic` and all its tools listed. None of its tools was called.

Neither behaviour is in the docs. They describe `--strict-mcp-config` only for configured
servers, and they name `ENABLE_CLAUDEAI_MCP_SERVERS=false` and the setting
`disableClaudeAiConnectors` as the switches that stop Claude Code from fetching connectors
(**[documented]**, not tested). Phase 9 therefore checks the behaviour again for each new
Claude Code version.

The entry is read from the MCP status of a session without strict mode, which also starts
your user-scope servers (as `claude mcp list` does). Its `url` and `id` are account data and
are handled like a definition from `~/.claude.json`: copied into the turn's private file,
never logged or shown.

Still **[unverified]**: a tool call through such an entry; the tool names a turn sees; and
whether a key other than the reported name works, which matters because Spartito's names
allow no spaces or dots.

**Same name and URL.** Claude Code stores OAuth sign-ins per endpoint. **[documented]**
Spartito keeps the server's name and its `type`, `url` and `headers` exactly as configured,
so that the copy finds the stored sign-in. Whether the name is part of the key is
**[unverified]**.

**Tool search.** With a restricted `--tools` list and no `ToolSearch` in it, Claude Code
loads every MCP tool's schema into every request. The docs say so: leave it out of
`--tools`, "MCP tools load upfront instead". **[documented]**

The prompt tokens of a turn's first request, in real headless turns with `--model sonnet`,
one measurement each **[observed]**:

| agent | prompt tokens |
|---|---|
| worker without a capability | 8,457 |
| worker with `chrome`, its 20 tools listed upfront (as today) | 20,015 (about +11,600) |
| worker with `chrome` and `ToolSearch` in `--tools` | 11,311 (about +2,900) |
| lead (16 `orch` tools), as wired today | 14,121 |
| lead with `ToolSearch` (the `orch` tools deferred too) | 9,855 |
| lead with `chrome` and `ToolSearch` | 12,054 |
| lead with `chrome`, `ToolSearch` and `"alwaysLoad": true` on the `orch` server | 16,939 |

What else these turns showed **[observed]**:

- Deferral works in headless stream-json turns when `ToolSearch` is in `--tools`. Deferred
  tools were found and called without errors, with one extra request before a tool's first
  use.
- The stream's `system`/`init` event lists the MCP tools whether they are deferred or not.
  Only the token count shows the difference.
- `ENABLE_TOOL_SEARCH` is not needed. Unset, tools are already deferred; `true` cannot
  replace the `--tools` entry; `false` removes `ToolSearch` even when `--tools` names it;
  `auto` loads tools upfront below the documented 10% threshold.
- `alwaysLoad: true` on one server keeps its tools upfront while the other servers' tools
  stay deferred. `alwaysLoad` is accepted on every server type. **[documented]**

The changes, built separately as [phase 0](#phases):

- A turn where a capability that brings MCP tools is on (`chrome`, or any MCP-server
  capability) adds `ToolSearch` to a restricted `--tools` list.
- The `orch` server gets `alwaysLoad: true` on such a turn, so a lead's own tools stay
  upfront and need no search step.
- The loadout key `tool_search` chooses: `auto` (the default) does the above, `always` adds
  `ToolSearch` on every turn with MCP tools and defers the `orch` tools too, and `never`
  adds it on no turn.

**Allowlists.**

- `--disallowedTools` with a bare tool name removes that tool from the model's context.
  **[documented]** It does so for the `chrome` tools. **[observed]**
- Allow rules take an `mcp__<server>__…` prefix and globs after it; deny rules accept full
  globs. **[documented]**
- Nothing in a `--mcp-config` entry restricts a server's tools. **[documented]**: the only
  per-tool policy is for in-process SDK servers.

An allowlist therefore becomes a deny list: every tool of the last probe that is not on the
allowlist. A tool the server adds after the last probe would get through. Two guards:

- the turn records the tools the CLI reported, and the page flags tools it has not seen
  before
- an optional `PreToolUse` hook refuses any `mcp__<server>__…` call that is not allowed. It
  uses the hook mechanism the memory guard already has, with its canary. When hooks are off,
  the server is skipped, so the allowlist fails closed.

**Permissions.** An allowlisted tool is *available*, not *approved*. As for `chrome`, nothing
is added to `--allowedTools`. In `auto` mode (Spartito's default), an MCP call that no rule
matches goes to the classifier. **[documented]** In a `-p` run without a mode it is denied.
**[documented]** Tools a server marks as needing user interaction, and connector tools your
organization set to `ask`, are always denied in `-p`. **[documented]**

**When a server fails.**

- With `--mcp-config` and `-p`, Claude Code waits for pending servers up to `MCP_TIMEOUT`
  (30 s by default). **[documented]**
- If a server still fails, the run goes on without its tools. **[documented]**
- With tool search, the model is told which server failed, or that it needs sign-in.
  **[documented]**

A failing server can therefore delay a turn by up to 30 s, but it does not fail the turn.
The runner reads the `mcp_servers` statuses of the stream's `system`/`init` line and records
them with the turn. An agent's line then shows, for example,
`jira: on · needs auth at the last turn`.

### Codex

What Codex offers:

- `[mcp_servers.<name>]` with `command`, `args`, `env`, `env_vars`, `cwd`, `url`,
  `bearer_token_env_var`, `env_http_headers`, `enabled_tools`, `disabled_tools`,
  `default_tools_approval_mode`, `required` and the timeouts. **[documented]**
- `-c mcp_servers.<name>.…` overrides work under `--ignore-user-config`. **[observed]**:
  Spartito's `orch` server is wired this way today.
- `codex mcp list --json` gives machine-readable output. **[documented]**
- `codex mcp login NAME` signs in to an HTTP server. **[documented]**

`enabled_tools` is a real allowlist, so Codex needs no deny list.

What is open:

- `codex exec` rejects MCP calls that need approval without a reviewer. Spartito's code says
  so in a comment and sets `approve` for the `orch` server for that reason. A user server
  would therefore run either with no approval gate (`approve`) or not at all.
- Whether a token from `codex mcp login` is found for a server defined only through `-c`,
  under `--ignore-user-config`. **[unverified]**

Codex is therefore a later phase. Until then a Codex agent skips MCP-server capabilities
with a note.

## The MCP page, the command and the API

### The dashboard page

A new page at `#/mcp`, opened from a new button in the side bar's header. It has a project
picker (*All projects* / a project), because local scope, `.mcp.json` and the project level
depend on it. Its parts:

- **Header.** When the status was last checked, **Check now**, **Add server**.
- **Groups.** Spartito, Claude Code (user), this project (local and `.mcp.json`), plugins,
  claude.ai connectors.
- **One row per server.**
  - name, origin and transport
  - status chip: `connected`, `needs auth` with a **Sign in** button, `failing` with its
    detail, `pending approval`, `unknown`
  - tools: `12`, or `8 of 12` with an allowlist
  - cost: `~9.8k tokens listed · ~0.3k deferred`, with *estimate* when it is not measured
  - **Measure**
  - the inherit / on / off controls for the global level and, with a project picked, the
    project level, reusing `capabilityRow`
  - a count of roles and agents with their own setting
- **An expanded row.**
  - what it reaches
  - the redacted definition
  - per-role controls (the same rows as the Capabilities box)
  - the agents that have it on
  - the allowlist editor: the tools with checkboxes, their read-only or destructive hints,
    and presets *all* and *read-only only*; tools new since the last check are flagged
- **Plugin servers and connectors.** The level controls are disabled, with the reason.

**Long-lived agents.** Before a change that affects the orchestrator or a lead with a
conversation, the page warns:

> Applies from Maestro's and lead-1c2d's next turn; a running turn keeps its servers. It
> changes their tool list, which may start a fresh prompt cache: the next turn re-reads
> the whole conversation (about 180k tokens for lead-1c2d) at full price.

The background for this warning:

- With deferred tools, Claude Code keeps the tool list from the conversation's first
  request for the whole conversation, so servers coming or going keep the cache.
  **[documented]**
- Without deferral, adding or removing a server's tools invalidates the cache.
  **[documented]**
- Adding `ToolSearch` to an agent that had none changes the tool list once.
- Whether "the whole conversation" holds across Spartito's one-process-per-turn `--resume`
  is **[unverified]**, so the warning says *may*.

The text comes from the same place as the model and effort cache hints (`core/setup.py`).

The agent's setup panel keeps one line per capability; MCP servers show under a heading
*MCP servers*. The new-agent dialog lists them with the other capabilities. The agent list's
badges show them.

### The command

**Recommendation:** a group `spartito mcp`, whose `on`, `off` and `inherit` are the
capability commands for `mcp:<name>`.

    spartito mcp [--project P] [--refresh] [--json]   # list with status, tools, cost, levels
    spartito mcp show NAME [--project P]              # definition (redacted), tools, levels
    spartito mcp on|off|inherit NAME [--project P] [--role R] [--agent A]
    spartito mcp tools NAME [--only a,b | --all]      # the allowlist
    spartito mcp measure NAME [--project P]           # the probe
    spartito mcp add NAME --command CMD [--arg A]… [--env NAME='${VAR}']…
    spartito mcp add NAME --url URL [--header 'Name: ${VAR}']… [--also-claude-code]
    spartito mcp remove NAME                          # Spartito's own servers only
    spartito mcp login NAME [--project P]             # runs claude mcp login (below)

`spartito mcp on jira --role tester` is the same as
`spartito capabilities on mcp:jira --role tester`. `spartito capabilities` lists MCP servers
in a section of their own, and `spawn --capabilities mcp:jira` works.

    $ spartito mcp --project my-app
      NAME     ORIGIN       TRANSPORT  STATUS      TOOLS   COST (listed / deferred)  GLOBAL   PROJECT
      docs-db  spartito     stdio      connected   6       ~3.1k / ~0.1k             off      on
      jira     claude-user  http       needs auth  8 of 12 ~6.0k / ~0.2k (estimate)  inherit  inherit
    role reviewer: mcp:docs-db off (final)
    not available to agents yet: 3 claude.ai connectors, 1 plugin server

### The JSON API

| endpoint | does |
|---|---|
| `GET /api/mcp?project=&refresh=` | the list as `spartito mcp --json` |
| `GET /api/mcp/servers/<name>?project=` | one server with its tools |
| `POST /api/mcp/servers` | add (owner only) |
| `POST /api/mcp/servers/<name>/remove` | remove (owner only) |
| `POST /api/mcp/servers/<name>/tools` | set the allowlist (owner only) |
| `POST /api/mcp/servers/<name>/measure` | run the probe |
| `POST /api/mcp/servers/<name>/login` | start the sign-in (owner only) |
| `POST /api/mcp/refresh` | run the status check |

Levels go through the existing `POST /api/capabilities` and
`POST /api/agents/<id>/capabilities` with the name `mcp:<name>`. `/api/state` keeps
`capabilities` and gains nothing heavy: the MCP page fetches its own data.

### What agents see

`get_agent`'s `capabilities.items` gets one entry per server, with:

- `kind: "mcp"`
- `origin`
- `tools`: allowed of total, or `null`
- `last_status`: as the last turn reported it

`list_agents` lists the names that are on, as today. Agents get no MCP tools to add, remove,
restrict or sign in to servers.

## Adding and signing in

### Where added servers live

**Recommendation:** in Spartito's database (table `mcp_servers`), seen only by Spartito's
agents. Spartito does not change `~/.claude.json` unless you ask for exactly that.

| | Spartito's table | `claude mcp add` (user or local scope) |
|---|---|---|
| who sees it | Spartito's agents | also your own Claude Code sessions |
| removal | `spartito mcp remove` | `claude mcp remove` |
| OAuth sign-in | not possible: `claude mcp login` knows only servers in Claude Code's own configuration **[documented]** | `claude mcp login NAME` |
| secrets | values refused; `${VAR}` references only | whatever was typed, stored in plain text |
| backup | with the database ([state.md](state.md)) | not Spartito's |

Rules for added servers:

- `env` and header values must be `${VAR}` references. A literal value is refused:
  `put the value in an environment variable of the service and write ${VAR}`. Spartito is
  not a secret store.
- The variable must reach the process that runs the turns. For the background service, that
  is the environment it was installed with ([desktop.md](desktop.md#background-service)).
- Whether `--mcp-config` expands `${VAR}` as `.mcp.json` does is **[unverified]**. If it does
  not, the runner expands it into the private file at turn start, and keeps nothing.
- Arguments are literal. The form warns against putting secrets in them.
- Names must not collide with the `orch` server, with Claude Code's reserved names
  (`claude-in-chrome`, `computer-use`, `workspace`, …) or with a server discovered in your
  configuration.

**OAuth servers.** A remote server that signs in with OAuth must be in Claude Code's own
configuration. The add form's **Also add to Claude Code (user scope)** checkbox, and
`--also-claude-code`, say exactly that:

> Your own Claude Code sessions will see this server too.

Only with it does Spartito run
`claude mcp add-json NAME '<definition>' --scope user`, and Claude Code writes its file.
The server is then a `claude-user` server, signed in with `claude mcp login`.

### Signing in

Claude Code runs OAuth itself:

- `/mcp` in an interactive session, or `claude mcp login NAME` in a shell. **[documented]**
- `claude mcp login` works for HTTP, SSE and claude.ai connector servers. **[documented]**
- A `-p` run cannot sign in: sign in first, then the run uses the stored token.
  **[documented]**
- Tokens are kept in the macOS Keychain, or in `~/.claude/.credentials.json`.
  **[documented]**

**Sign in**, step by step:

1. You click **Sign in** on a server with status `needs auth`. The page says that your
   browser will open.
2. Spartito runs `claude mcp login NAME`, with stdin closed and a 5-minute limit. For a
   local-scope server it runs in the project's checkout.
3. Claude Code opens your browser at the server's sign-in page. You sign in, and the browser
   is sent back to Claude Code's local callback.
4. Claude Code stores the token. Spartito keeps only the exit code, never the output (it
   holds the sign-in URL), and never reads, stores or passes on a token.
5. Spartito checks the status again. The row shows `connected`, or the failure with the
   fallback below.

The fallback: if the browser cannot reach the callback, `claude mcp login` asks for the
redirect URL to be pasted, which needs a terminal. The page then shows the command to copy:
`claude mcp login jira`. Whether the command works without a terminal when the redirect
succeeds is **[unverified]**.

A copy of the definition in a turn's configuration is expected to find the token, because
tokens are kept per endpoint. **[documented]** Whether it finds it under another name, or
when the definition differs, is **[unverified]**.

## Safety

**Reaches.** Every server's `reaches` line is shown wherever it can be turned on:

- `stdio`: runs `<command>` on this machine as you, with your files, your network and the
  environment it is given.
- remote: sends the agent's tool calls, and what they carry, to `<origin>`, with the
  credentials configured for it; the service acts as the account you signed in with.
- an added server: your own line is shown as well.

**Prompt injection.** Tool results are text the model reads. A ticket, a page or a database
row can carry instructions aimed at the agent. The `auto` classifier sees tool calls but not
tool results. A server-side check scans incoming results. **[documented]** Neither is a
guarantee.

**Repositories grant nothing.**

- Spartito never reads a server from an agent's worktree.
- A `.mcp.json` server is listed from the project's main checkout, and is off until the
  owner turns it on.
- The owner's on pins the definition's shape: its command, args, URL, and env and header
  names, not their values. When a pulled change alters it, turns skip the server with
  `the definition changed since it was turned on: review it in the MCP page`.
- Strict mode stays, so a repository's `.mcp.json` never loads by itself.

**No escalation.** The rules of [capabilities.md](capabilities.md#giving-capabilities-from-agents)
apply to every `mcp:` name. Adding, removing, allowlists and sign-in are the owner's alone:
not even the orchestrator agent may do them, since they change what code runs and with whose
credentials.

**The `auto` mode.** No MCP tool is pre-approved; the agent's permission mode decides each
call. Do not combine MCP servers with `bypassPermissions`.

**A guard rail, not a sandbox.** An agent with a shell can still edit `~/.claude.json` or
Spartito's home, as it can every other setting.

**Cost.** A server adds context to every turn of every agent that has it: little when its
tools are deferred, a lot when they are listed.

## Storage

All of it is additive.

- **Settings.** No new keys. The `capabilities` and `role_capabilities` objects take
  `mcp:<name>` entries. Older code ignores them, so for it they are off. Older code that
  writes a scope's settings drops them, because it keeps only the entries it can read. A
  downgrade therefore loses MCP levels, and nothing else.
- **`agents.capabilities`.** The same.
- **`run_meta.capabilities`.** The JSON record gains a key `mcp`, for example
  `{"jira": "needs-auth"}`. Older code ignores it.
- **New table `mcp_servers`.**
  - `name` (primary key), `origin`, `transport`
  - `definition` (JSON; Spartito's own servers only; never with values)
  - `reaches`
  - `tools` (JSON allowlist, or null)
  - `pin` (shape hash, or null)
  - `created_at`, `updated_at`

  Rows for discovered servers carry only the allowlist and pin.
- **New table `mcp_checks`.**
  - `name`, `project` (`''` for none), `status`, `detail` (one line)
  - `tools` (JSON: name, hints, tokens listed and deferred)
  - `definition_hash`, `cli_version`, `checked_at`

  It is a cache that can be deleted.
- Both tables are created with `CREATE TABLE IF NOT EXISTS`. Existing databases open
  without a manual step. `tests/test_store.py` covers an old database and the new code, and
  an old code path against a new database.
- **Outside Spartito.** `~/.claude.json` is changed only by Claude Code, and only through
  `--also-claude-code`.

## Phases

Sizes are for one worker each, tests and docs included: **S** about half a day, **M** about
a day, **L** about two days. Every phase ships alone.

| # | phase | depends on | size |
|---|---|---|---|
| 0 | `ToolSearch` and an upfront `orch` server (a separate change, being built) | nothing | S, 1 task |
| 1 | Spartito's own servers as capabilities, Claude wiring, command | 0 | 3 × M |
| 2 | Discovering your servers and their status | 1 | 2 × M |
| 3 | The probe: tools and token cost | 2 | M, possibly + S |
| 4 | The dashboard's MCP page | 1–3 | L + M |
| 5 | Allowlists | 3 | M + S |
| 6 | Adding from the page | 4 | M |
| 7 | Signing in | 2, 4 | M |
| 8 | Codex | 1, 5 | M + S |
| 9 | claude.ai connectors and plugin servers | 2, 3; plugin servers: owner's verifications | connectors: M (estimate); plugin servers: not estimated |

Phases 0 to 8 come to about 15 to 18 worker-days.

**0. Tool search for agents with MCP tools**

This phase is being built as a separate, small change, ahead of the rest of this design.

- Scope: a loadout key `tool_search`:
  - `auto`, the default: on a turn where a capability that brings MCP tools is on, a
    restricted tool list gets `ToolSearch`, and the `orch` server gets `alwaysLoad: true`.
    `chrome` brings MCP tools, and so does every MCP-server capability.
  - `always`: `ToolSearch` is added on every turn with MCP tools, and the `orch` tools are
    deferred too.
  - `never`: `ToolSearch` is not added.
- Files: `loadout.py`, `providers/claude.py`, `tests/test_loadout.py`,
  `tests/test_providers.py`, `docs/configuration.md`, `docs/capabilities.md`,
  `CHANGELOG.md`.
- Risky: the cost of a turn and the cache when an existing agent first gets `ToolSearch`
  (real turns).

**1. Spartito's own servers as capabilities, Claude wiring, command**

- Scope: the `mcp:` names; the dynamic registry; `Wiring.mcp_servers`; the merged private
  configuration file; the `mcp_servers` table; the init statuses recorded with each turn;
  `spartito mcp list|show|on|off|inherit|add|remove` (stdio and HTTP, `${VAR}` only);
  `get_agent`.
- Files:
  - new: `src/orchestrator/mcp.py` (entries, validation, redaction), `core/mcp.py`
  - changed: `capabilities.py`, `core/grants.py`, `store.py`, `service.py`,
    `providers/base.py`, `providers/claude.py`, `models.py` (init statuses), `runner.py`,
    `config.py` (runtime file), `cli.py`, `mcp_server.py`
  - tests: `test_mcp.py`, `test_capabilities.py`, `test_store.py`, `test_providers.py`,
    `test_runner.py`, using fake CLIs on `PATH`
  - docs: this page, `capabilities.md`, `configuration.md`, `usage.md`, `agents.md`,
    `CHANGELOG.md`
- Tasks: (a) domain, store and registry; (b) wiring and the runner; (c) command and docs.
- Risky: `${VAR}` expansion in `--mcp-config`, and whether stdio servers inherit the turn's
  environment (and thereby the turn mark the runner ends processes by) **[unverified]**.

**2. Discovering your servers and their status**

- Scope: read user, local and project scope (names and shapes only); parse
  `claude mcp list`; the `mcp_checks` cache; `--refresh`; wire discovered stdio and remote
  servers through the private file; the pin for `.mcp.json`.
- Files: `providers/claude.py` (or a new `providers/claude_mcp.py` for the readers and the
  parser), `core/mcp.py`, `store.py`, `cli.py`, tests with a fake `claude mcp list`, docs.
- Risky: the list's text format can change between Claude Code versions (keep the parser
  lenient; `unknown` is the fallback); `claude mcp list` connects to everything you have.

**3. The probe: tools and token cost**

- Scope: the control-protocol probe; listed and deferred tokens; the chars/4 fallback;
  `spartito mcp measure`.
- Files: `providers/claude.py`, `core/mcp.py`, `store.py`, `cli.py`, tests with a fake CLI
  that answers the control requests, docs.
- Risky: the control requests' wire names and answers are **[unverified]**; the probe makes
  token-counting API requests.

**4. The dashboard's MCP page**

- Scope: the page, the per-level controls (reusing the capability rows), the measure and
  check-now actions, the long-lived-agent warning, the agent setup's *MCP servers* heading,
  and the HTTP endpoints.
- Files: `web/app.py`, `web/static/app.js`, `web/index.html`, `web/static/app.css`, tests
  of the web API, the node tests of `app.js`, `docs/usage.md`.
- Risky: `app.js` is large and its polls must not reset a focused control (the capability
  work already solved this; reuse it).

**5. Allowlists**

- Scope: `spartito mcp tools`, the editor, deny lists built from the last probe, the
  new-tool flag, and the optional fail-closed hook guard.
- Files: `mcp.py`, `providers/claude.py`, `memory_guard.py` (or a sibling guard),
  `runner.py`, web, tests, docs.
- Risky: a tool the server adds between probes; hooks turned off.

**6. Adding from the page**

- Scope: the add form (stdio or URL, `${VAR}` fields, reaches, *Also add to Claude Code*);
  `claude mcp add-json --scope user` only with that checkbox.
- Files: web, `core/mcp.py`, `providers/claude.py`, tests with a fake `claude`, docs.
- Risky: writing your Claude Code configuration; it must be the checkbox and nothing else.

**7. Signing in**

- Scope: **Sign in** and `spartito mcp login`: run `claude mcp login`, wait, check again;
  the copy-the-command fallback.
- Files: `core/mcp.py`, `providers/claude.py`, web, `cli.py`, tests with a fake `claude`
  (no browser), docs.
- Risky: behaviour without a terminal **[unverified]**; it opens your browser, but only on
  your click.

**8. Codex**

- Scope: `-c mcp_servers.<name>.*` wiring with `enabled_tools`; discovery through
  `codex mcp list --json` when `codex_user_config` is on; `codex mcp login`.
- Files: `providers/codex.py`, `core/mcp.py`, tests, `docs/providers.md`.
- Risky: no approval gate in `exec`; whether a token is found under
  `--ignore-user-config` **[unverified]**.

**9. claude.ai connectors and plugin servers**

- Scope, connectors: read each connector's `claudeai-proxy` entry from the MCP status of a
  session without strict mode; turning one on copies the entry, under its reported name,
  into the turn's private file (see [claude.ai connectors](#claudeai-connectors)); a
  `spartito mcp` name without spaces maps to it; a check that strict mode still keeps
  connectors out and that a single entry still connects, per Claude Code version.
- Scope, plugin servers: only if a verification shows a way: `${CLAUDE_PLUGIN_ROOT}`
  resolved by Spartito.
- Size: connectors M, an estimate; plugin servers not estimated.
- Risky: both connector behaviours are observed, not documented, and may change with a
  Claude Code version.

## Open questions

1. **Copying definitions from `~/.claude.json` into each turn.** Some hold literal secrets.
   *Recommendation:* yes, into a private file per turn that is removed when the turn ends,
   never stored, logged or shown. Without it, only `${VAR}`-only definitions could be used.
2. **Where added servers live.** *Recommendation:* Spartito's database, not a file in the
   home; it is backed up with the state.
3. **Refuse literal `env` and header values.** *Recommendation:* yes, `${VAR}` only.
4. **OAuth servers must be in Claude Code's configuration** (the *Also add to Claude Code*
   checkbox). *Recommendation:* yes.
5. **Who manages servers.** *Recommendation:* add, remove, allowlists and sign-in are the
   owner's only. Turning on follows the capability rules, so the orchestrator agent may.
6. **Allowlists per server or per level.** *Recommendation:* one per server first; per role
   later if needed.
7. **`.mcp.json` servers from repositories.** *Recommendation:* list them and allow the
   owner's on, pinned by shape.
8. **Connectors.** Strict mode keeps them out, and one can be given alone as a copied
   `claudeai-proxy` entry (observed once, not documented). *Recommendation:* give them one
   at a time this way in phase 9, keep strict mode, and check the behaviour per Claude Code
   version; never through `mcp = inherit`.
9. **How often the status is checked.** *Recommendation:* when the page opens and the cache
   is older than 10 minutes, and on **Check now**; never in the background.
10. **`ToolSearch` always, or only with MCP tools.** *Recommendation:* only with MCP tools
    or `chrome`, and the `orch` tools always upfront (`tool_search = auto`). The trade-off
    for leads, from one measurement: deferring the `orch` tools too saves about 4,300
    tokens per request, and costs a search before the first use of each tool.
11. **Pre-approving allowlisted tools.** *Recommendation:* no; the permission mode decides,
    as for `chrome`.
12. **The command.** *Recommendation:* `spartito mcp`, mapping to `mcp:<name>`
    capabilities.
13. **Codex.** It has no approval gate in `exec`. *Recommendation:* support a server for
    Codex only when the owner marks it *runs without approval for Codex*.
14. **Repositories and `mcp = inherit`.** A repository's `.orch/loadout.toml` or
    `.orch/roles/*.md` can set `mcp = inherit` today, which gives agents every server,
    including the repository's own `.mcp.json`. *Recommendation:* a separate fix that
    ignores `mcp = inherit` from project files.

## Not verified yet

Each item says how to check it. None needs a model turn unless said; all need the owner's
go-ahead, because they run the real CLI with the owner's login. Some items are no longer on
this list:

- That `ToolSearch` in `--tools` defers MCP tools for agents was observed in real turns (see
  [Wiring per provider](#wiring-per-provider)).
- That `--strict-mcp-config` keeps claude.ai connectors out, and that one connector can be
  given alone as a `claudeai-proxy` entry in `--mcp-config`, were observed once with Claude
  Code 2.1.322, through the MCP status control request and without a model turn (see
  [claude.ai connectors](#claudeai-connectors)).
- The MCP status control request itself was observed then (see
  [the probe](#tools-and-token-cost-the-probe)); the context-usage request remains.

1. Whether a copied definition finds an OAuth token: same name and URL; another name.
   Probe against a signed-in server.
2. Whether `--mcp-config` expands `${VAR}`, and whether stdio servers inherit the CLI's
   environment. Probe with a fake local stdio server that records its environment, in a
   temporary directory.
3. The control request for context usage over stream-json, without a user message, and its
   answer. Probe.
4. A connector given alone as a `claudeai-proxy` entry: whether a tool call through it
   works, which tool names a turn sees, and whether a key other than its reported name
   works. One minimal real turn; a read-only tool.
5. That the probe's token counting is not billed, or is acceptable, for the owner's login.
6. Whether a resumed conversation (a new process with `--resume`) keeps the cache when a
   server is added or removed, with deferred tools. Real turns.
7. Whether a disallowed MCP tool leaves the deferred list too. Probe with context usage.
8. How the `auto` classifier treats writing or destructive MCP tools. Real turns.
9. Whether `claude mcp login` works without a terminal when the browser redirect succeeds.
   Visible (it opens a browser): the owner does it.
10. Whether `claude mcp list` writes to `~/.claude.json`, and how long it takes with stdio
    servers. Compare the file's modification time; time a run.
11. Codex: whether a token is found for a `-c`-defined server under
    `--ignore-user-config`, and how `enabled_tools` and the approval modes behave in
    `exec`. Real Codex turns.

## Sources

Claude Code documentation (read November 2026):

- MCP: <https://code.claude.com/docs/en/mcp>. Scopes and precedence, `claude mcp list`
  statuses, environment-variable expansion, plugin servers, connectors and
  `disableClaudeAiConnectors`, authentication and `claude mcp login`, failures in `-p` runs,
  tool search and `alwaysLoad`.
- CLI reference: <https://code.claude.com/docs/en/cli-reference>. `--mcp-config` and
  `MCP_TIMEOUT`, `--strict-mcp-config`, `--tools`, `--allowedTools`, `--disallowedTools`.
- Permissions: <https://code.claude.com/docs/en/permissions> (MCP rules, tool-name
  wildcards).
- Permission modes: <https://code.claude.com/docs/en/permission-modes> (`-p` without a
  mode, how auto mode decides).
- Prompt caching: <https://code.claude.com/docs/en/prompt-caching> (connecting or removing
  an MCP server, denying a tool).
- Managed MCP: <https://code.claude.com/docs/en/managed-mcp> (allow and deny lists,
  `serverName`).
- Settings reference: <https://code.claude.com/docs/en/settings-reference>.
- Environment variables: <https://code.claude.com/docs/en/env-vars>.
- Credentials: <https://code.claude.com/docs/en/iam> (Keychain, `.credentials.json`).
- Agent SDK: <https://code.claude.com/docs/en/agent-sdk/mcp> and
  <https://code.claude.com/docs/en/agent-sdk/typescript> (`McpServerStatus`,
  `getContextUsage`).

Codex documentation:

- <https://developers.openai.com/codex/mcp>
- <https://developers.openai.com/codex/cli/reference> (`codex mcp`, `--ignore-user-config`)
- <https://developers.openai.com/codex/config-reference> (`mcp_oauth_credentials_store`)
