# Roadmap

Where Spartito is heading. This is a direction, not a schedule: there are no dates, and an
item can change or be dropped. Each item says what it would mean for you, what it builds on
today, and the questions that are still open. Ideas and comments are welcome in the
repository's issues ([CONTRIBUTING.md](CONTRIBUTING.md)).

| theme | near term | next | later |
|---|---|---|---|
| reach | [authentication](#dashboard-authentication-and-safe-exposure), [remote control](#remote-control) | [phone](#phone-friendly-dashboard-and-push-notifications) | |
| isolation | | [devcontainers](#devcontainers), [permissions](#permission-profiles-and-approvals) | |
| integrations | | [harnesses](#more-harness-integrations), [GitHub](#github-automations), [trackers](#issue-tracker-links-for-the-board) | |
| workflow | [code view](#worktree-code-view) | [merging](#merge-assistance), [playbooks](#reusable-playbooks), [schedules](#scheduled-and-event-triggered-turns) | |
| operations | [CI](#ci-for-the-public-repository) | [releases](#safe-upgrades-and-releases), [observability](#observability), [costs](#budgets-and-cost-reports), [backup](#state-backup-export-and-move), [refactoring](#the-rest-of-the-internal-refactoring) | |
| platforms | [Linux](#linux-tested-end-to-end) | | [Windows](#windows-support) |
| team | | | [team projects](#team-level-project-management) |

The near term makes the dashboard reachable safely and the agents' work visible. The next
phase isolates agents, connects them to more tools, and makes long-running use dependable.
The later phase is for what needs shared state or a new platform layer.

## Near term

### Reach

#### Dashboard authentication and safe exposure

**What it means.** The dashboard could be opened from somewhere other than this machine
without letting everyone who can reach it control your agents: a login or token, plus
guidance for TLS. Remote control depends on it.

**Today.** The dashboard has no login. It accepts only loopback `Host` headers (a guard
against DNS rebinding), and writes only from its own origin with a JSON body.
`spartito web --host` with a non-loopback address accepts any `Host` and prints a warning
instead of protecting anything ([dashboard](docs/usage.md#dashboard)).

**Open questions.** A login of Spartito's own, a token, or a tunnel or VPN in front of an
unchanged dashboard? Built-in TLS, or guidance for a reverse proxy? One user, or several
(see [team projects](#team-level-project-management))?

#### Remote control

**What it means.** You could follow your agents, answer their decisions and send messages
from another device. The working assumption is the dashboard itself, reached safely from
elsewhere.

**Today.** The [dashboard](docs/usage.md#dashboard) does all of this in a local browser, and
[decisions](docs/agents.md) and
[notifications](docs/desktop.md#notifications-with-click-through) tell you when an agent
needs you.

**Open questions.** Does "remote" mean another device reaching this dashboard, a chat channel
(messages and decisions in a messenger), or controlling agents that run on another machine?
Which actions are allowed remotely: everything, or only reading and answering decisions?

### Workflow

#### Worktree code view

**What it means.** You could look at the code an agent wrote without opening a terminal: the
files of its worktree and the diff against the base branch, next to its conversation. The
working assumption is read-only browsing and diffs.

**Today.** Every agent works in its own
[git worktree](docs/concepts.md#worktrees-and-branches). The agent view and `spartito status`
show its commits since the base branch and its uncommitted files, and the
[task board](docs/board.md) shows a branch as ahead, behind, dirty or merged. File contents
and diffs are not shown anywhere.

**Open questions.** Read-only, or with comments sent back to the agent, or editing? Limits for
large diffs and binary files. Serving file contents widens what the dashboard exposes, which
ties this item to authentication.

### Operations

#### CI for the public repository

**What it means.** Tests and linters would run on every pull request, so that outside
contributions are checked before review.

**Today.** GitHub Actions runs the checks of [CONTRIBUTING.md](CONTRIBUTING.md#checks) on
every push to `master` and every pull request: the suite on Linux and on macOS (with the
desktop extras there), the linters and the link test.

**Open questions.** Whether the Linux run should include the `app` extra (PyQt6), which no
test needs today.

### Platforms

#### Linux tested end to end

**What it means.** Linux would be as dependable as macOS, including its desktop features.

**Today.** Linux is supported, but the sleep inhibitor, the cleanup of a stopped turn's
processes, the launcher and `notify-send` notifications have not been tested on a real
machine ([limitations](docs/architecture.md#platform-support-and-limitations)).

**Open questions.** Which distributions and desktops to cover.

## Next

### Reach

#### Phone-friendly dashboard and push notifications

**What it means.** On a phone, you could answer a decision or see that an agent has
finished, from a notification, without a computer at hand. This continues remote control.

**Today.** The dashboard can be installed as an app
([installable page](docs/desktop.md#installable-page)), and an open page shows notifications
with a click that opens the agent or the decision. Its stylesheet has a layout for narrow
windows, but no phone has been a target so far. Notifications come only from an open page or
from a helper on this machine.

**Open questions.** Push to a phone with no page open needs a push service. Browsers allow
installing and notifications only on a secure origin, so this needs authentication and TLS
first.

### Isolation

#### Devcontainers

**What it means.** A project's agents could run inside that project's container, with its
toolchain and a boundary between them and the rest of the machine. Spartito's service could
run in a Docker container too.

**Today.** Every turn is a detached runner process on the host that runs `claude -p` or
`codex exec` in the agent's worktree. Claude's permission mode or Codex's sandbox is the only
boundary ([architecture](docs/architecture.md#how-it-works-at-run-time)). The
[background service](docs/desktop.md#background-service) is a launchd agent or a systemd
user unit.

**Open questions.** One container per project or per agent, and how worktrees and the state
directory are mounted. How the provider CLIs log in inside a container. A dashboard in a
container must listen beyond loopback, so it needs authentication first. Which host
features stay, such as notifications and keeping the machine awake.

#### Permission profiles and approvals

**What it means.** Per role or project, you could set what agents may do (push, network
access, deleting files), and an agent that needs more would ask you from the dashboard, like a
decision. Agents and logs would never carry secret values. Devcontainers would be the
isolation layer underneath.

**Today.** Claude agents run with `--permission-mode auto` and Codex agents in the
`workspace-write` sandbox. `spartito spawn --permission` changes this for one agent
([permissions](docs/configuration.md#providers-and-permissions)), and the
[loadout](docs/configuration.md#loadout) sets the built-in tools per role and project. The
service and the launcher pass only a fixed set of environment variables to agents.

**Open questions.** Where rules are enforced (the CLI's own permissions, hooks, a container).
How a headless turn waits for an approval. How secrets are kept out of prompts, events and
logs.

### Integrations

#### More harness integrations

**What it means.** Agents could run on more coding-agent CLIs than Claude Code and Codex,
chosen per project or per agent as today, including locally hosted models where a harness
supports them.

**Today.** The plug-in interface exists. Providers implement a small protocol
(`providers/base.py`) and are registered in the package or with `ORCH_PROVIDERS`
([providers](docs/configuration.md#providers-and-permissions)). Codex shows what an
integration with fewer features looks like: no cost, a context size only from its session
files, and compaction as a separate turn
([limitations](docs/architecture.md#platform-support-and-limitations)).

**Open questions.** Which harnesses come first. What a provider must support (structured
answers, the `orch` MCP tools for leads, usage and cost, compaction), and how missing
features are shown.

#### GitHub automations

**What it means.** Per project and opt-in, Spartito could watch pull requests, reviews and CI
with `gh`, and turn events into board items or messages to the project lead.

**Today.** The lead role answers PR review comments it is given and asks before every push.
Nothing watches GitHub.

**Open questions.** Polling needs a running process, and webhooks need a reachable server
(see [schedules](#scheduled-and-event-triggered-turns) and
[authentication](#dashboard-authentication-and-safe-exposure)). Which events act on their own,
and which become a decision for you?

#### Issue-tracker links for the board

**What it means.** Board items could be linked to GitHub Issues, or kept in sync with them,
with other trackers later.

**Today.** A [board item](docs/board.md) links to an agent, a branch, the message it was made
from and the decisions that block it. There are no links to outside trackers.

**Open questions.** A one-way link or a two-way sync, and which side owns an item's state.

### Workflow

#### Merge assistance

**What it means.** Help with bringing agent branches together: integration branches, a
preview of conflicts across branches, and a merge queue that waits for your approval.

**Today.** Agents never merge; you do. `--base` starts a branch on an earlier one. The board
moves an item to done when its branch is merged, rebased or reworded commits included, but
it does not detect a squash merge of more than one commit.

**Open questions.** Who resolves a conflict (an agent in its worktree?), and how this fits
merges that happen on GitHub.

#### Reusable playbooks

**What it means.** Named multi-agent recipes, such as implement, review and fix with a round
limit, a test matrix, or a release check, that you or a lead start with parameters. Projects
could also have brief templates.

**Today.** The building blocks exist: roles as Markdown files, `--after`, `{{result:…}}`,
`--base`, shared worktrees and output schemas. The lead's prompt allows at most two review
rounds. There is no declarative pipeline layer
([providers and permissions](docs/configuration.md#providers-and-permissions)).

**Open questions.** Where a playbook lives (the project, the home), and how it differs from a
custom role.

#### Scheduled and event-triggered turns

**What it means.** A brief could run at a set time or on an event, such as a failed CI run or
a new pull request.

**Today.** There is no daemon: a turn starts when someone sends a message, and its runner
waits only for the turns it depends on. A schedule therefore needs something running at
that time, such as the background service or the system's own timers (launchd, systemd).

**Open questions.** Where schedules live, what happens to a run missed while the machine was
asleep, and how cost is kept in check.

### Operations

#### Safe upgrades and releases

**What it means.** Versioned releases with a changelog, packaged installs (a published
package, a signed macOS launcher), a backup of the state before the schema changes, and no
mismatch between the page and the server after an update.

**Today.** Spartito is published on PyPI as `spartito` from a version tag by GitHub Actions
([docs/releasing.md](docs/releasing.md)), without a changelog. Schema changes
are additive and applied when the database opens. After an update, a running dashboard keeps
its old code while the page is new, so it has to be restarted
([upgrading](docs/usage.md#upgrading-from-orch)). The launcher's copy of Python is signed ad
hoc only.

**Open questions.** Versioning. Whether the page should
notice a server of another version (`/api/ping` reports it).

#### Observability

**What it means.** Searchable run history and logs, timelines per agent and per board item, a
record of how each notification was shown, and export of results and decisions.

**Today.** The dashboard shows each agent's runs and events, `spartito logs` and
`spartito status` show them in the terminal, and every turn keeps its logs in
`logs/run-<id>/`. A board item's detail shows its history and runs. On macOS the window of
`spartito app` logs its notification state to `logs/app.log`. There is no search and no
export.

**Open questions.** What is searched, how long it is kept, and how exports stay free of
private data.

#### Budgets and cost reports

**What it means.** Limits per project, agent or day that warn or stop, and cost over time and
per board item.

**Today.** Every turn stores its tokens and cost. `spartito ps`, the agent lists and the
dashboard show totals, the dashboard's over every recorded run
([tokens and cost](docs/agents.md#tokens-and-cost)). Codex reports no cost. There are no
limits and no reports over time.

**Open questions.** What a limit does to a running turn, and how to budget a provider that
reports no cost.

#### State backup, export and move

**What it means.** Back up the state, export it, and move it to another machine. This is a
step towards shared team state.

**Today.** All state is in one directory (`~/.orchestrator/`, or `ORCH_HOME`): the database,
the worktrees, the logs ([state](docs/architecture.md#state)). Projects are stored with their
local paths. There is no export or import command.

**Open questions.** What moves (the database only, or the worktrees too), how paths are
rewritten, and how to copy a database that is in use.

#### The rest of the internal refactoring

**What it means.** Nothing visible. The code would be easier to change safely.

**Today.** The layer rule has a pinned list of exceptions that may only shrink
([layers](docs/architecture.md#layers)). The remaining work removes them.

## Later

### Platforms

#### Windows support

**What it means.** Spartito could run on Windows.

**Today.** Windows is not supported: process handling relies on POSIX process groups and
sessions, and the service, the launcher and the notifications are built for macOS and Linux
([platform support](docs/architecture.md#platform-support-and-limitations)).

**Open questions.** Native Windows, or Linux under WSL.

### Team

#### Team-level project management

**What it means.** Several people could share projects, the task board and decisions, see who
started which agent, and answer decisions meant for them.

**Today.** Spartito is single-user. All state is one local SQLite database, decisions go to
the one owner, and the dashboard has no users. The [task board](docs/board.md) and
[decisions](docs/agents.md) are the parts a team would share first. Authentication and
state export come before it.

**Open questions.** A server, or synchronising local databases? Who may start agents, and who
answers which decision? How cost is assigned to people.
