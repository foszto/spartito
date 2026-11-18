# Capabilities

A *capability* is something extra an agent's CLI gets for its turns, beyond its role prompt and
its [loadout](configuration.md#loadout). Spartito has one so far, `chrome`: the browser tools
of Claude in Chrome. Capabilities are off by default. They are turned on and off globally, per
project, per role and per agent, and every agent shows which ones its next turn gets.

- [Capabilities, the loadout and tools](#capabilities-the-loadout-and-tools)
- [Listing them](#listing-them)
- [Turning them on and off](#turning-them-on-and-off)
- [What an agent gets](#what-an-agent-gets)
- [Giving capabilities from agents](#giving-capabilities-from-agents)
- [`chrome`: Claude in Chrome](#chrome-claude-in-chrome)
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
scope afterwards, or with `--agent` the agent's capabilities (as `capabilities show`). The
dashboard's settings and an agent's setup panel show and change them. The JSON API:
`GET /api/capabilities` and `POST /api/capabilities`, `POST /api/agents/<id>/capabilities`
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
chrome)`). `inherit` also accepts a name that is no longer registered, so that a setting left by
a removed plugin can be cleared; until then, a turn that would have it skips it with the note
`not registered here`.

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
`agent`) and a note. The same appears in:

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

Changing the set of an agent that has a conversation changes the tools its model sees, which
can cost a fresh prompt cache. Capabilities apply to agents' turns only: `spartito chat` and
`spartito attach` are your own sessions and run with your own CLI settings.

## Giving capabilities from agents

The MCP tools `spawn_agent` and `send_message` take `capabilities`, comma-separated:
`"chrome"` turns one on in the agent's own setting, `"-chrome"` turns it off.

- `spawn_agent(…, capabilities="chrome")` sets the new agent's own setting.
- `send_message(agent_id, message, capabilities="chrome")` changes the target agent's own
  setting before the message is queued. The change stays for its later turns. A refused change
  queues nothing.

**No escalation.** An agent can turn on for another agent only a capability that its own next
turn has. The orchestrator agent, the interactive `spartito chat` session and the owner's
surfaces (the command line, the dashboard, the JSON API) can give any. Turning a capability off
is always allowed. A role's `off` applies to everyone, the owner included. A refusal names the
reason:

```
cannot give chrome: lead-1c2d does not have it itself, and an agent may give only the capabilities its own turns have
```

From the command line, `spartito spawn --capabilities "chrome"` sets a new agent's own setting
the same way. Back to inherit, for an agent's own setting, goes only through
`spartito capabilities inherit NAME --agent AGENT` and the JSON API.

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

To turn it on for every agent, and off again:

```sh
spartito capabilities on chrome
spartito capabilities off chrome
```

### Safety

- It drives your real, signed-in browser: an agent with `chrome` acts with your sessions on any
  site you are logged in to. It can read mail, submit forms and change settings as you.
- Its windows and tabs appear on your desktop.
- Agents that run at the same time share the one browser.
- Web pages can carry instructions aimed at the agent (prompt injection).
- It adds context to every turn of every agent that has it.
- No browser tool is pre-approved: the agent's permission mode decides each call (`auto` unless
  `spawn --permission` sets another). Do not combine `chrome` with `bypassPermissions`.
- Roles that should never browse can be turned off, for example
  `spartito capabilities off chrome --role planner`.

### Not verified yet

The Claude Code documentation describes `--chrome` for interactive sessions. It has not been
tested with a real session:

- whether the browser tools appear in a headless turn (`-p --output-format stream-json`), which
  is how Spartito runs every turn
- how a turn behaves when the extension is missing or the browser is closed
- how it interacts with a strict MCP configuration (the loadout's `mcp = strict`) and a
  restricted tool list (the loadout's `tools`)
- how it interacts with resuming a session
- how it behaves with several agents at once

This is why the default is off. An organization-managed MCP configuration can also make
`claude --chrome` exit at startup; if it does, turn `chrome` off for the agents concerned.

## Adding a capability

`ORCH_CAPABILITIES="package.module:attribute,…"` registers more capabilities after the
built-in one ([configuration.md](configuration.md#environment-variables)). The attribute is a
`Capability` from `src/orchestrator/capabilities.py`, or a sequence of them:

| field | what |
|---|---|
| `name` | lower case letters, digits and `-`, starting with a letter, at most 32 characters |
| `label`, `description` | what people read |
| `reaches` | one line on what it can reach |
| `providers` | provider name → `Support(on=Wiring(argv=…, env=…), off=Wiring(…))`: what it adds to that provider's turns when on and when off; a provider not named does not support it |
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

Capabilities that bring their own MCP servers, a file in the home that declares them, and
further availability checks are planned, not built.
