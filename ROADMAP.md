# Roadmap

Where Spartito is heading. This is a direction, not a schedule: there are no dates, and an
item can change or be dropped. Each item says what it would mean for you, what it builds on
today, and the questions that are still open. Ideas and comments are welcome in the
repository's issues ([CONTRIBUTING.md](CONTRIBUTING.md)).

| phase | items |
|---|---|
| near term | [Remote control](#remote-control), [Worktree code view](#worktree-code-view) |
| next | [Devcontainers](#devcontainers), [More harness integrations](#more-harness-integrations) |
| later | [Team-level project management](#team-level-project-management) |

## Remote control

**What it means.** You could follow your agents, answer their decisions and send messages
from another device, for example your phone, without leaving the dashboard open to everyone
who can reach the machine. The working assumption is the dashboard itself, reached safely
from another device.

**Builds on.** The [dashboard](docs/usage.md#dashboard) already does all of this in a
browser, and [decisions](docs/agents.md) and
[notifications](docs/desktop.md#notifications-with-click-through) already tell you when an
agent needs you. But it has no login. It accepts only loopback `Host` headers and writes from
its own origin, and `spartito web --host` with a non-loopback address prints a warning
instead of protecting anything.

**Open questions.**

- Does "remote" mean another device reaching this dashboard, a chat channel (messages and
  decisions in a messenger), or controlling agents that run on another machine?
- Authentication and transport: a login of Spartito's own, or a tunnel or VPN in front of an
  unchanged dashboard?
- Which actions are allowed remotely: everything, or only reading and answering decisions?

## Worktree code view

**What it means.** You could look at the code an agent wrote without opening a terminal: the
files of its worktree and the diff against the project's base branch, next to its
conversation. The working assumption is read-only browsing and diffs.

**Builds on.** Every agent works in its own [git worktree](docs/concepts.md#worktrees-and-branches).
The dashboard's agent view and `spartito status` already show its commits since the base
branch and its uncommitted files, and the [task board](docs/board.md) shows a branch as ahead,
behind, dirty or merged. File contents and diffs are not shown anywhere yet.

**Open questions.**

- Read-only, or with comments that go back to the agent as a message, or editing?
- How large a diff or file it shows, and how binary files are treated.
- Serving file contents widens what the dashboard exposes, which ties this item to the login
  question of remote control.

## Devcontainers

**What it means.** The agents of a project could run inside that project's container, with
its toolchain and with a boundary between them and the rest of the machine. Spartito's
service could also run in a Docker container.

**Builds on.** Today every turn is a detached runner process on the host that runs
`claude -p` or `codex exec` in the agent's worktree, with Claude's permission mode or Codex's
sandbox as the only boundary ([architecture](docs/architecture.md#how-it-works-at-run-time)).
The [background service](docs/desktop.md#background-service) is a launchd agent or a systemd
user unit, and Linux is supported.

**Open questions.**

- One container per project or per agent, and how worktrees and the state directory are
  mounted into it.
- How the provider CLIs log in inside a container, and where their credentials live.
- A dashboard inside a container has to listen beyond loopback, which needs the login of
  remote control first.
- Which host features stay, such as desktop notifications and keeping the machine awake.

## More harness integrations

**What it means.** Agents could run on further coding-agent CLIs besides Claude Code and
Codex, chosen per project or per agent as today.

**Builds on.** Providers implement a small protocol (`providers/base.py`) and are registered
in the package or with `ORCH_PROVIDERS`
([configuration](docs/configuration.md#providers-and-permissions)). Codex shows what an
integration with fewer features looks like: no cost, a context size only from its session
files, and compaction as a separate turn
([limitations](docs/architecture.md#platform-support-and-limitations)).

**Open questions.**

- Which harnesses come first.
- What a provider must support (structured answers, the `orch` MCP tools for leads, usage and
  cost, compaction), and how missing features are shown.

## Team-level project management

**What it means.** A later phase: several people could share projects, the task board and
decisions, see who started which agent, and answer decisions meant for them.

**Builds on.** Spartito is single-user today. All its state is one local SQLite database
under `~/.orchestrator/`, decisions go to the one owner, and the dashboard has no users. The
[task board](docs/board.md) and [decisions](docs/agents.md) are the parts a team would share
first.

**Open questions.**

- Shared state: a server, or synchronising local databases?
- Identities and permissions: who may start agents, and who answers which decision.
- How agents' cost is assigned to people or projects.

## Also under consideration

Outside the five items above:

- **GitHub automations.** Watching pull requests, reviews and CI with `gh`, so that a lead can
  act on review comments or a failed check. Today the lead and orchestrator roles cover only
  replies to PR review comments, and nothing watches them.
- **The rest of the internal refactoring.** The layer rule has a pinned list of exceptions
  that may only shrink ([layers](docs/architecture.md#layers)). The remaining work removes
  them.
