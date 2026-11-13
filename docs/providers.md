# Providers

A *provider* is the coding-agent CLI (the harness) that runs an agent's turns. Spartito
supports two, Claude Code and Codex. It checks which of them this machine can start, and new
agents and projects get one of those.

- [Supported harnesses](#supported-harnesses)
  - [Claude Code (`claude`)](#claude-code-claude)
  - [Codex (`codex`)](#codex-codex)
- [Availability](#availability)
  - [Where the CLI is looked for](#where-the-cli-is-looked-for)
  - [When no provider is available](#when-no-provider-is-available)
  - [What is not checked](#what-is-not-checked)
- [How a provider is chosen](#how-a-provider-is-chosen)
- [A project's default provider](#a-projects-default-provider)
- [Moving an agent to another provider](#moving-an-agent-to-another-provider)
- [Other providers](#other-providers)

## Supported harnesses

| provider | name | CLI |
|---|---|---|
| Claude Code | `claude` | `claude` |
| Codex | `codex` | `codex` |

The registry order (`claude`, then `codex`, then [other providers](#other-providers)) is the
order in which "the first available one" is chosen. Model and effort are set per provider
([configuration.md](configuration.md#model-and-effort)).

### Claude Code (`claude`)

Anthropic's coding agent CLI, run as `claude -p --output-format stream-json`.

| | |
|---|---|
| efforts | `low`, `medium`, `high`, `xhigh`, `max` |
| cost | reported |
| role prompt | every turn |
| compaction | Claude Code's own `/compact`, with optional instructions |
| loadout | every key but `codex_user_config` ([configuration.md](configuration.md#loadout)) |
| attribution | the Claude Code line ([concepts.md](concepts.md#agents-and-roles)) |
| permissions | `--permission-mode auto` by default |

Limits: none listed. `spartito chat` runs only Claude Code.

### Codex (`codex`)

OpenAI's coding agent CLI, run as `codex exec --json`.

| | |
|---|---|
| efforts | `low`, `medium`, `high`, `xhigh` |
| cost | not reported |
| role prompt | first turn of a conversation |
| compaction | a turn with a lowered auto-compaction limit, without instructions |
| loadout | `codex_user_config` only |
| attribution | the Codex line, in the first turn |
| permissions | the `workspace-write` sandbox by default |

Limits:

- reports no cost: budgets do not count its turns
- gets its role prompt and attribution only in the first turn
- compacts without instructions, once its context size is known
- of the loadout only `codex_user_config` applies: `tools` and `memory` do not restrict it

More on these: [agents.md](agents.md#context-and-compaction),
[architecture.md](architecture.md#platform-support-and-limitations).
`spartito providers --json` lists the same limits for each provider.

## Availability

`spartito providers` checks every provider now and prints one line each. `*` marks the
default: the first available one, which a new project gets. For example:

```
  NAME    STATUS         VERSION     REASON
* claude  available      2.4.1       found on PATH; login not checked
  codex   not_installed  -           `codex` was not found on PATH or in the usual install locations

A background service or launcher keeps the PATH of its install: after installing a CLI, run `spartito service install` or `spartito app install` again.
```

The line below the table appears only when a provider is `not_installed`, and not with
`--json`.

`spartito providers --json` adds each provider's label, description, efforts, features,
limits and models. The dashboard shows the same in every provider choice: one that is not
available is disabled, with the reason as its tooltip, and when the provider that would apply
is not available a hint says so, with a **Check again** button that checks once more. The JSON API: `GET /api/providers` (`?refresh=1` checks again).

| status | meaning | can be chosen |
|---|---|---|
| `available` | the CLI was found and answered `--version` | yes |
| `not_installed` | the CLI was not found, its `#!` interpreter (such as `node`) is missing, or it could not be started | no |
| `not_logged_in` | the provider's own login check failed; the built-in providers have none, so they never show it | no |
| `unknown` | the CLI was found but `--version` failed or did not answer within 5 seconds, the provider names no CLI, or the check has not run yet | yes |

`unknown` can be chosen: the check could not tell, and the turn shows whether the CLI works.

The check runs only `<cli> --version`, with a 5-second timeout. It makes no network request,
runs no agent turn and reads no credentials. With `--json` (and in the dashboard) an available
CLI is also asked for its models: Claude Code reads them without a model request, Codex with
`codex debug models`. Each process keeps the result for 5 minutes, and `spartito providers`
always checks again. Each process checks with its own `PATH`: what `spartito providers` finds
in a terminal can differ from what the background service sees, and the dashboard shows the
service's view.

### Where the CLI is looked for

First on `PATH`, then in these directories, in this order, where they exist:

```
~/.local/bin
/opt/homebrew/bin
/usr/local/bin
~/.npm-global/bin
~/.volta/bin
~/.bun/bin
```

The reason says which: `found on PATH`, or `found in ~/.npm-global/bin, not on PATH`. Every
CLI Spartito starts (turns, `spartito attach`, `spartito chat`, the model lists) gets these
directories appended to its `PATH`, so that a `#!/usr/bin/env node` script finds `node` next to
it.

A background service or launcher keeps the `PATH` of the shell it was installed from
([desktop.md](desktop.md#background-service)). A CLI installed in one of the directories above
is found anyway. After installing a CLI anywhere else, run `spartito service install` or
`spartito app install` again, then check with `spartito providers`.

A turn whose CLI is missing fails with that reason, and `spartito attach` and `spartito chat`
refuse with it; these errors, like the refusal of a provider that is not available, end with
"`spartito providers` says more".

### When no provider is available

An error that says no provider is available, or that a provider is not, gives each one's
status and reason. What to do:

1. Install a CLI (Claude Code or Codex, as its own documentation describes) and log in with
   it in a terminal.
2. Run `spartito providers`. It should show the CLI as `available`. If it shows
   `not_installed`, put the CLI's directory on `PATH` or install it into one of the
   [usual directories](#where-the-cli-is-looked-for); a `#!` interpreter it names (such as
   `node`) must be found too.
3. If the dashboard runs as a background service or from the launcher, run
   `spartito service install` or `spartito app install` again, so that it picks up the new
   `PATH`. Then use the dashboard's **Check again** button, or wait up to 5 minutes for the
   next check.

### What is not checked

The login is not checked: no check is known that is both offline and free of prompts. An
`available` CLI that is not logged in, has no access to the model or cannot reach its service
still fails at its first turn, with the CLI's own error in the agent's log. Log in with the
CLI itself, in a terminal.

## How a provider is chosen

For a new agent, the first that applies:

1. **Explicit**: `spartito spawn --provider`, `spartito ask --provider` (a new lead), the
   `provider` of the MCP tools, or the dashboard's choice. It must be available, or the request
   is refused with the reason and the available alternatives:
   ``codex is not available: not installed (…). Available: claude. `spartito providers` says more``.
2. **The role's** `provider` (role frontmatter, [configuration.md](configuration.md#roles)).
3. **The project's default** ([below](#a-projects-default-provider)).

When the provider from 2 or 3 is not available, the agent runs on the first available one in
registry order. So does the Maestro when nothing names its provider and `claude` is not
available (the note then calls `claude` "the first supported provider"). Such a fallback is
reported in the reply: a `note: …` line on the command line (`spartito spawn`, `spartito ask`,
`spartito model`, `spartito board dispatch`) and a `note` in the MCP and JSON replies. The
dashboard shows it as a message after a message to an agent (which may create a lead or the
Maestro), after adding a project and after starting an agent. When the dashboard creates the
Maestro by setting its model or effort, or a lead by a dispatch from its board, it shows no
message.

The agent's log has the note when the process that created the agent also sends its first
message, as for `spartito spawn`, `spartito ask`, the MCP tools, board dispatch and any
message from the dashboard. A Maestro created by a setting in one process
(`spartito model orchestrator --effort high`) whose first message comes from another (the
dashboard) has no such log entry: only that command's `note: …` line reported the fallback.

When no provider is available, the agent is not created, and the error lists each provider's
reason ([below](#when-no-provider-is-available)).

**The orchestrator (Maestro)** is created by its first message, or by setting its model,
effort or provider. Its provider is the one chosen before that
(`spartito model orchestrator --provider codex`, or the dashboard's setup panel), else the
`provider` of the `orchestrator` role (an override in `~/.orchestrator/roles/orchestrator.md`),
else the first available one. With none available it is created anyway, and its first turn
fails with the reason. After that it keeps its provider until it is
[moved](#moving-an-agent-to-another-provider).

**A project lead** is created by its first message too, on `spartito ask --provider` or
`ask_project_lead`'s `provider`, else as any agent (2 and 3); the dashboard's lead
conversation and board dispatch name none. An existing lead keeps its provider until it is
moved; a request that names another one gets a note that says so.

A role's `model` applies only to agents of the role's own provider (or to every agent when the
role names no provider): a Claude model name means nothing to Codex.

## A project's default provider

`spartito project add --provider` sets it; without it the project gets the first available
provider, and the output names it (`provider: claude`). An explicit `--provider` must be
available. When none is, the project is still registered, on `claude`, with a note.

```sh
spartito project provider my-app codex
# my-app: new agents run on codex
# note: 2 existing agent(s) keep their provider (lead-1c2d, fix-login-3f2a); new agents run on codex
```

The new default must be available. Existing agents keep theirs; move one as described below.
The same change: the dashboard's settings for the project, the MCP tool `set_project_provider`
(top-level orchestrator only), and `POST /api/projects/<name>/provider`.

If a project's default stops being available (the CLI is removed), its new agents run on the
first available provider, with a note, until it is back.

## Moving an agent to another provider

```sh
spartito model fix-login-3f2a --provider codex
spartito model orchestrator --provider codex --yes
```

In the dashboard: the `provider` select next to the agent's model and effort drop-downs, the
Maestro's included.

A conversation does not move from one CLI to another, but Spartito remembers an agent's last
conversation on each provider. The first move to a provider starts a new conversation there,
which knows nothing of the agent's conversations elsewhere: give it what it needs in the next
message. The conversation on the provider it left is kept, and moving back continues it; a
later move to a provider the agent has run on before continues that provider's conversation
the same way. Nothing is carried over from one provider's conversation to another's.

- **Stays:** the agent's id, role, worktree, branch, board items, sub-agents, its log and its
  last conversation on each provider.
- **Cleared on every move:** the agent's own model and effort (they named the old provider's
  models; the tier settings of the new provider apply until it gets its own again).
- **Logged:** the agent's log notes the move, and whether the next message continues the
  earlier conversation on the new provider or starts a new one.

If the CLI can no longer resume a remembered conversation (its session files were deleted or
belong to another machine), the agent's next turn fails with the CLI's error. There is no way
yet to forget such a conversation: keep the agent on the other provider, or start a new agent.

It is refused while the agent has a running or queued turn (stop it or wait), and when the new
provider is not available. Moving to the same provider changes nothing. An agent with a
conversation is moved only after a confirmation, which says whether the next message
continues an earlier conversation on the new provider or starts a new one: `spartito model`
asks (refusing exits with code 1, `--yes` skips the question), the dashboard asks, and the
JSON API answers `{"confirm": "…"}` until the request is repeated with `"confirm": true`. The Maestro before
its first message has no conversation: choosing its provider creates it on that one.

There is no MCP tool to move an agent.

## Other providers

`ORCH_PROVIDERS="name=package.module:Class,…"` registers more providers after the built-in
ones ([configuration.md](configuration.md#environment-variables)). A provider implements the
protocol in `src/orchestrator/providers/base.py`. Its self-description is optional:

| attribute | what | without it |
|---|---|---|
| `label` | the name people read | the provider's name |
| `description` | one line on what it is | empty |
| `executable` | the CLI's command name, for the availability check | not checked: `unknown`, which can be chosen |
| `version_args` | what the check runs | `--version` |
| `login_args` | a command that exits non-zero when not logged in; offline, never prompting | the login is not checked |
| `reports_cost` | whether its turns report a cost | not said |
| `role_prompt` | `every_turn` or `first_turn` | not said |
| `limits` | what it does not support, one phrase each | none listed |

Compaction (`compact_prompt`) and the context size (`context_after`) are optional too: a
provider without `compact_prompt` cannot compact. A provider other than `claude` and `codex`
gets no attribution section.
