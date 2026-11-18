# Capabilities

A *capability* is something extra an agent's CLI gets for its turns, beyond its role prompt and
its [loadout](configuration.md#loadout). Spartito has one built in, `chrome`: the browser tools
of Claude in Chrome. Every MCP server you add in Spartito is one too, named `mcp:<name>`
([MCP servers](#mcp-servers)). Capabilities are off by default. They are turned on and off
globally, per project, per role and per agent, and every agent shows which ones its next turn
gets.

- [Capabilities, the loadout and tools](#capabilities-the-loadout-and-tools)
- [Listing them](#listing-them)
- [Turning them on and off](#turning-them-on-and-off)
- [What an agent gets](#what-an-agent-gets)
- [Giving capabilities from agents](#giving-capabilities-from-agents)
- [`chrome`: Claude in Chrome](#chrome-claude-in-chrome)
- [MCP servers](#mcp-servers)
- [Adding a capability](#adding-a-capability)

## Capabilities, the loadout and tools

A *tool* of Claude Code or Codex is one thing the model can call, such as `Bash` or
`mcp__orch__spawn_agent`. The loadout's `tools` key lists the CLI's built-in tools and
restricts them. A capability adds: one name stands for what a CLI needs for a whole feature,
such as a flag that brings a set of tools (`chrome` brings Claude in Chrome's browser tools).
Each provider that supports a capability says what it adds to that provider's turns; an agent
on another provider skips it.

## Listing them

```sh
spartito capabilities [--project PROJECT] [--json]
```

```
$ spartito capabilities --project my-app
  NAME    PROVIDERS  GLOBAL         PROJECT  STATUS   REASON
  chrome  claude     off (default)  inherit  unknown  needs the Claude in Chrome extension, a running browser and a claude.ai login, which Spartito cannot see
role reviewer: chrome off (final)
role tester (my-app): chrome on
```

`PROVIDERS` are the providers that support it. `GLOBAL` is the global setting, or the default
when there is none; `--project` adds the project's own setting (`inherit`: none). The lines
below the table list the roles' settings, globally and, with `--project`, the project's.
`--json` adds each capability's label, description, what it can reach and what it requires.
MCP servers you added follow in a section of their own, `MCP servers (spartito mcp):`, with
their capability names, transport, command or URL origin, levels and last status
([mcp.md](mcp.md#listing-and-showing)).
`spartito providers` lists, per provider, the capabilities it supports.

| status | meaning |
|---|---|
| `unknown` | Spartito cannot tell whether it works here; the agent's turn shows whether it does |
| `unavailable` | known not to work for this process, with the reason: no provider that supports it is usable here, or the capability's own check says so (for `chrome`, for example `ANTHROPIC_API_KEY is set: Claude in Chrome needs a claude.ai login`) |
| `available` | the capability's own check says it works; no built-in capability has a check that can say this |

The check is best effort: it looks at the provider check (`spartito providers`) and at the
environment of the process that asks, reads no file and no credential, and runs nothing
beyond the providers' `--version`. A variable is only looked at for whether it is set, never
for its value. The status is information only: a capability that is on is passed to the turn
whatever its status. The dashboard shows the background service's view, which can differ from
a terminal's.

## Turning them on and off

```sh
spartito capabilities on chrome                                # every agent
spartito capabilities off chrome --project my-app              # not in this project
spartito capabilities off chrome --role reviewer               # reviewers never get it
spartito capabilities on chrome --role tester --project my-app # testers of my-app get it
spartito capabilities on chrome --agent fix-login-3f2a         # this agent
spartito capabilities inherit chrome                           # back to the default
```

`on` and `off` set the value at one level; `inherit` removes it, so that the level above
applies. Without `--project`, `--role` or `--agent` the level is the global one. `--agent`
cannot be combined with `--project` or `--role`. Each command prints the listing of its
scope afterwards, or with `--agent` the agent's capabilities (as `capabilities show`), and
drops entries of that scope's settings that cannot be read.

In the dashboard, the settings have a **Capabilities** box, for all projects and per project,
with an inherit / on / off choice per capability and the roles' settings shown read-only. An
agent's setup panel has a line per capability with its state, where it comes from and an
inherit / on / off choice; the new-agent dialog has a choice per capability, and agents with a
capability on show a small badge in the list. The JSON API: `GET /api/capabilities` and
`POST /api/capabilities`, `POST /api/agents/<id>/capabilities`
([usage.md](usage.md#dashboard)).

For each capability the last level that says something wins:

1. the default: off for every capability
2. the global setting
3. the project's setting (the orchestrator agent has no project, so this level does not apply
   to it)
4. the role's setting: the project's entry for the role (`--role R --project P`), else the
   global entry (`--role R`)
5. the agent's own setting

**A role's `off` is final.** An agent's own setting cannot turn the capability back on, and
setting it is refused, naming the command that changes the role's setting:

```
$ spartito spawn -p my-app -r reviewer --capabilities chrome "Check the login page."
Error: chrome is off for role reviewer, and an agent's own setting cannot turn it on; change the role's: `spartito capabilities on chrome --role reviewer`
```

An agent whose own setting was there first keeps it stored, and its turns skip the capability
with the note `off for role reviewer: the role's setting wins over the agent's`. An `off` at the
global or the project level is not final: a role or an agent setting below it can turn the
capability on again.

A name that is not registered is refused when it is set (`unknown capability 'jira' (known:
chrome)`), by the commands and by the settings API alike. `inherit` also accepts a name that is
no longer registered, so that a setting left by a removed plugin can be cleared; until then, a
turn that would have it skips it with the note `not registered here`. For an MCP server the
refusal names the servers added and the command that adds one (`spartito mcp add`), and
removing a server removes its settings at every level.

A capability that is on but that the agent's provider does not support is skipped, with the
note `codex does not support it`; it is not an error, and the setting stays. Starting an agent
whose own setting asks for one says so in the reply's note.

The settings are stored with the other settings: the keys `capabilities` and
`role_capabilities`, globally and per project
([configuration.md](configuration.md#capabilities-capabilities-and-role_capabilities)). The
agent's own setting is stored on the agent. A change applies from each agent's next turn: the
set is resolved when a turn starts, and a running turn keeps the one it started with.

## What an agent gets

```
$ spartito capabilities show fix-login-3f2a
chrome  on       global
$ spartito capabilities show review-7b1c
chrome  skipped  global  codex does not support it
```

One line per capability: its name, its state for the agent's next turn (`on`, `off`, or
`skipped` with the reason), the level that decided it (`default`, `global`, `project`, `role` or
`agent`) and a note. An [MCP server](#mcp-servers)'s line also gives its status at the agent's
last turn that recorded one, such as `last status: connected`. The same appears in:

- `spartito status AGENT`, as one line:
  `capabilities none  (skipped: chrome; spartito capabilities show review-7b1c)`
- the MCP tool `get_agent`: `capabilities` with `on` (the names that are on) and `items`,
  one per capability with `name`, `label`, `state`, `source` (the level), `override` (the
  agent's own setting, or `null`), `final` (a role turns it off) and `note`
- the MCP tool `list_agents` and the agents of the JSON API's `/api/state`: `capabilities`,
  the names that are on

When a turn's set differs from the agent's previous turn's, the agent's log gets a line such as
`capabilities for this turn: chrome` or
`capabilities for this turn: none; skipped: chrome (codex does not support it)`; an agent's
first turn gets one only when something is on or skipped. Every turn's set is recorded with the
turn in the database.

Changing the set of an agent that has a conversation changes the tools its model sees from its
next turn. For `chrome`, one measurement showed no fresh prompt cache when it was turned off for
a resumed session. Capabilities apply to agents' turns only:
`spartito chat` and `spartito attach` are your own sessions and run with your own CLI settings.

## Giving capabilities from agents

The MCP tools `spawn_agent` and `send_message` take `capabilities`, comma-separated:
`"chrome"` turns one on in the agent's own setting, `"-chrome"` turns it off. A `spartito`
command run in an agent's turn (`spartito capabilities …`, `spartito spawn --capabilities …`)
acts as that agent too: every turn runs with `ORCH_AGENT_ID` set to its own agent, and the same
rules apply. Run against another home than the turn's own (a temporary one, for example), the
command acts as that home's owner.

- `spawn_agent(…, capabilities="chrome")` sets the new agent's own setting.
- `send_message(agent_id, message, capabilities="chrome")` changes the target agent's own
  setting before the message is queued. The change stays for its later turns. A refused change
  queues nothing.

**No escalation.** The orchestrator agent, the interactive `spartito chat` session and the
owner's own surfaces (a terminal outside any agent's turn, the dashboard, the JSON API) can give
any capability, at any level. Any other agent:

- can turn on, for itself or another agent, only a capability that its own next turn has
- cannot turn on again what an agent's own setting turned off: a lead that gave a worker
  `"-chrome"` cannot give it back; the owner or the orchestrator agent can
- cannot turn a capability on globally, for a project or for a role
- cannot use `inherit` to remove an off, whether the global one, a project's, a role's or an
  agent's own: removing an off can turn the capability on
- can always turn a capability off, and use `inherit` to remove an on

A role's `off` applies to everyone, the owner included. A refusal names the reason:

```
cannot give chrome: lead-1c2d does not have it itself, and an agent may give only the capabilities its own turns have
cannot give chrome: it was turned off for fix-login-3f2a, and only the owner or the orchestrator can turn it on again
lead-1c2d cannot turn chrome on globally: only the owner or the orchestrator turns a capability on globally, for a project or for a role
lead-1c2d cannot lift the off of chrome for role reviewer: only the owner or the orchestrator removes an off globally, for a project or for a role
cannot lift the off of chrome for fix-login-3f2a: it was turned off for it, and only the owner or the orchestrator can lift it
```

From the command line, `spartito spawn --capabilities "chrome"` sets a new agent's own setting
the same way. Back to inherit, for an agent's own setting, goes through
`spartito capabilities inherit NAME --agent AGENT`, the dashboard and the JSON API (for an
off, the owner's or the orchestrator agent's only).

## `chrome`: Claude in Chrome

Claude in Chrome connects Claude Code to a Chromium browser through a browser extension. With
it, Claude Code gets browser tools: open tabs, read pages, click, type and submit.

| | |
|---|---|
| providers | `claude` |
| default | off |
| when on | `--chrome` on the `claude` command line |
| when off | `CLAUDE_CODE_ENABLE_CFC=0` in the turn's environment |
| reaches | your real browser, signed in as you: its open tabs, the pages and accounts you are logged in to; it reads what pages show and acts on them (click, type, submit) as you |

Requires:

- Claude Code with --chrome
- the Claude in Chrome extension, version 1.0.36 or later, in Chrome or another supported Chromium browser, running while the turn runs
- Claude Code logged in with a claude.ai account: not an API key, a long-lived token or a third-party cloud provider (Bedrock, Vertex)

It is Claude only. A Codex agent skips it, with a note; `spartito providers` shows `chrome` in
the `CAPABILITIES` column of `claude` and `-` for `codex`.

Every Claude turn without `chrome` runs with `CLAUDE_CODE_ENABLE_CFC=0`, so an agent never gets
the browser by accident, for example through your own Claude Code setting that turns Claude in
Chrome on by default.

Its status is `unavailable` when Claude Code is not usable here, or when one of
`ANTHROPIC_API_KEY`, `CLAUDE_CODE_OAUTH_TOKEN`, `CLAUDE_CODE_USE_BEDROCK` or
`CLAUDE_CODE_USE_VERTEX` is set in the process that asks; otherwise it is `unknown`: the
extension, the browser and the login cannot be seen from outside.

A session must call the browser tool `tabs_context_mcp` with `createIfEmpty: true` before any
other browser tool; calling `tabs_create_mcp` first failed with an error that says so. Agents
normally follow that error; a prompt that asks for browser work can say it up front.

To turn it on for every agent, and off again:

```sh
spartito capabilities on chrome
spartito capabilities off chrome
```

### Cost

The browser tools are MCP tools, and their schemas take room in every request. Measured once
with Claude Code 2.1.322 (`--model sonnet`), on the first request of a worker's turn:

| worker's turn | prompt tokens per request |
|---|---|
| without `chrome` | about 8,500 |
| with `chrome`, the tools listed upfront (`tool_search = never`) | about 20,000 (+11,600) |
| with `chrome` and tool search (the default) | about 11,300 (+2,900) |

With tool search, Claude Code sends the tools' names only and loads a tool's schema when the
agent needs it: before it first uses a browser tool, the agent calls the built-in tool
`ToolSearch`, which costs one extra request. Spartito adds `ToolSearch` to the turn of an agent
with `chrome` whose role restricts the built-in tools; with the CLI's default tool set it is
there anyway. The loadout's [`tool_search`](configuration.md#tool-search) changes this.

### Safety

- It drives your real, signed-in browser: an agent with `chrome` acts with your sessions on any
  site you are logged in to. It can read mail, submit forms and change settings as you.
- Its windows and tabs appear on your desktop.
- Agents that run at the same time share the one browser; each session works in its own tab
  group and sees only its own tabs.
- Web pages can carry instructions aimed at the agent (prompt injection).
- It adds context to every turn of every agent that has it.
- No browser tool is pre-approved: the agent's permission mode decides each call (`auto` unless
  `spawn --permission` sets another). Do not combine `chrome` with `bypassPermissions`.
- Roles that should never browse can be turned off, for example
  `spartito capabilities off chrome --role planner`.

The rules on who may give a capability ([above](#giving-capabilities-from-agents)) are a guard
rail, not a sandbox. They stop an agent from widening its tools by accident or because a web
page told it to; an agent with a shell on your machine can still reach Spartito's home, the
dashboard's API or the command without its identity, as it can for every other setting.

### Checked so far

Checked once with Claude Code 2.1.322, with real headless turns, a running Chrome with the
extension and a claude.ai login; each point is a single measurement:

- In a headless turn (`-p --output-format stream-json`) with `--chrome`, the browser tools
  (`mcp__claude-in-chrome__…`, 20 of them) are listed and the built-in `claude-in-chrome` server
  reports `connected`. Without the flag, and with `CLAUDE_CODE_ENABLE_CFC=0`, they are absent.
- They stay with a strict MCP configuration (the loadout's `mcp = strict`), with a restricted
  tool list (the loadout's `tools`) and in the `auto` permission mode. A turn started by
  Spartito with `chrome` on got them.
- Resuming a session with `--chrome` keeps them; resuming it without (the capability turned off)
  works without an error, and they are gone.
- A turn opened its own new tab, read a page and closed the tab: in the `default` mode with the
  tools allowed, and in the `auto` mode with nothing pre-approved, where the classifier let these
  calls through.
- Two turns at the same time each used their own tab and both finished (one pair, not a stress
  test).
- With the browser tools listed upfront, a request carried about 11,600 more prompt tokens
  than without `chrome`; with tool search about 2,900 more ([Cost](#cost)). With tool search,
  turns found the browser tools through `ToolSearch` and called them without an error.

### Not verified yet

- how a turn behaves when the browser is closed, or the extension is missing or disabled
- whether the `auto` classifier also lets riskier browser tools through (JavaScript execution,
  screenshots, form input, uploads); only tab handling, navigation and reading a page were tried
- more than two agents at once, and long sessions
- an organization-managed MCP configuration: the Claude Code documentation says `claude --chrome`
  can exit at startup there; if it does, turn `chrome` off for the agents concerned

The default stays off: `chrome` reaches your signed-in browser, it costs context on every turn
of an agent that has it, and the cases above are untested.

## MCP servers

Each MCP server added in Spartito, a local command (stdio) or a remote URL (HTTP), is a
capability named `mcp:<name>`: `spartito mcp add docs-db --command docs-db-mcp` adds
`mcp:docs-db`, off until a level turns it on. Everything on this page applies to it: the
levels, a role's final `off`, the rules for agents, `spawn --capabilities mcp:docs-db`, the
dashboard's controls and what an agent shows. `spartito mcp on|off|inherit docs-db` is the same
as `spartito capabilities on|off|inherit mcp:docs-db`.

| | |
|---|---|
| providers | `claude` (Codex agents skip it with the note `codex does not support it`) |
| default | off |
| reaches | for a command, that it runs on this machine as you, with your files, your network and the environment it is given; for a URL, that the agent's tool calls go to its origin; and your own `--reaches` line |

Only you add and remove servers; agents cannot, the orchestrator agent included. Env values,
header values and the URL's query values are `${VAR}` references only, so Spartito stores no
secret in them; the command, arguments and the URL's path are stored as given and must hold
none. A turn whose
environment lacks a referenced variable leaves the server out, and the background service
passes such variables on to no turn
([mcp.md](mcp.md#the-variables)). On a Claude turn, the
servers that are on go into one private MCP configuration with the turn's tool search, and
`get_agent` shows each one's status at the agent's last turn. All of it, with the safety
notes and the limits of what is built: [mcp.md](mcp.md#what-works-today).

## Adding a capability

`ORCH_CAPABILITIES="package.module:attribute,…"` registers more capabilities after the
built-in one ([configuration.md](configuration.md#environment-variables)). The attribute is a
`Capability` from `src/orchestrator/capabilities.py`, or a sequence of them:

| field | what |
|---|---|
| `name` | lower case letters, digits and `-`, starting with a letter, at most 32 characters |
| `label`, `description` | what people read |
| `reaches` | one line on what it can reach |
| `providers` | provider name → `Support(on=Wiring(argv=…, env=…), off=Wiring(…))`: what it adds to that provider's turns when on and when off; a provider not named does not support it. `Wiring(…, mcp_tools=True)` says that what it adds brings MCP tools: a Claude turn then gets tool search under the loadout's [`tool_search`](configuration.md#tool-search) (default `False`) |
| `requires` | what it needs, one phrase each (optional) |
| `check` | returns `(status, reason)`: instant and offline, it reads no file and runs nothing (optional; without it the status is `unknown`) |
| `docs` | where it is documented (optional) |

```python
# my_capabilities.py, importable by Spartito (for example on PYTHONPATH)
from orchestrator.capabilities import Capability
from orchestrator.providers.base import Support, Wiring

STAGING = Capability(
    name="staging",
    label="Staging API",
    description="points the project's tools at the staging API",
    reaches="the staging API, with the access the agent's environment already has",
    providers={"claude": Support(on=Wiring(env={"MY_APP_API": "https://staging.example.com"}))},
)
```

```sh
export ORCH_CAPABILITIES="my_capabilities:STAGING"
```

A plugin's capabilities are off until turned on. An invalid or a duplicate name is refused, and
a spec that cannot be loaded stops the command, as a broken `ORCH_PROVIDERS` does.
`ORCH_CAPABILITIES` must reach the process that starts the runners (the background service
keeps it from the shell it is installed from:
[desktop.md](desktop.md#background-service)).

A file in the home that declares capabilities, and further availability checks, are planned,
not built. MCP servers need no plugin: they are added with `spartito mcp add`
([MCP servers](#mcp-servers)).
