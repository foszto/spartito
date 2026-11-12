# Spartito

Spartito runs Claude Code and Codex agents headless and in parallel, each in its own git
worktree and branch. You steer them through one orchestrator agent (shown as *Maestro*) from a
web dashboard or the command line, instead of managing many terminal sessions by hand. It is
for developers who already use Claude Code or Codex and want several agents working on their
repositories at once.

```
you ── orchestrator (Maestro)
        └─ project ── project lead (in the project's main checkout)
                        ├─ worker    (own git worktree, branch orch/<agent-id>)
                        ├─ reviewer  (reads the worker's worktree after it finishes)
                        └─ ...
```

The command is `spartito`; the Python package is `orchestrator`. The internal names stay as they
were: the `orch` MCP server and its tools, the `ORCH_*` variables, `orch.db` and the agents'
`orch/` branches.

## Features

- One conversation with an orchestrator agent that briefs a project lead per project; leads
  start and coordinate their own agents.
- Agents run headless (`claude -p`, `codex exec`) in parallel, each in its own git worktree and
  branch, or in a worktree shared with another agent (review, test, fix).
- Packaged roles: orchestrator, lead, planner, worker, reviewer, tester; custom roles are
  Markdown files.
- Results travel up automatically: a parent is woken with its agents' answers when they are
  done; tasks can depend on each other and embed another agent's answer.
- Web dashboard: conversations, tool calls, queued messages, model and effort per agent,
  context size, tokens and cost.
- Model and effort per tier (orchestrator, lead, worker), per provider, globally or per
  project; per-agent overrides.
- Persistent task board per project, and a list of decisions that wait for you.
- Automatic compaction of long conversations, automatic archiving of finished agents, keeping
  the machine awake while agents work, desktop notifications.
- The dashboard as a desktop application: a background service, an installable page,
  notifications whose click opens the agent or decision, and its own window (`spartito app`).
- No daemon: every turn is its own process; all state is in one SQLite database.

## Requirements

- macOS or Linux (see [Platform support](https://github.com/foszto/spartito#platform-support)).
- Python 3.14 and [uv](https://docs.astral.sh/uv/).
- `git`.
- The `claude` (Claude Code) CLI, installed and logged in, and optionally the `codex` CLI.
  The defaults use Claude Code: the orchestrator agent, `spartito chat`, and every project
  registered without `--provider`, so the quick start below needs `claude`. A Codex-only or
  mixed setup is configured per project and per agent:
  [docs/configuration.md](https://github.com/foszto/spartito/blob/master/docs/configuration.md#providers-and-permissions).

## Installation

From PyPI, the recommended install:

```sh
uv tool install --python 3.14 'spartito[app,menubar]'    # puts `spartito` on the PATH
spartito --help
```

The extras bring the native window of `spartito app` (`app`) and the macOS menu-bar item
(`menubar`; nothing on Linux). On Linux, `app` pulls in PyQt6, which is GPL-3.0-only: see
[docs/desktop.md](https://github.com/foszto/spartito/blob/master/docs/desktop.md#optional-dependencies). `--python 3.14` picks the Python
the package needs even when uv's default is older; uv downloads it if it is missing.

Alternatives: `uv tool install --python 3.14 spartito` without the extras (`spartito app` then
opens a browser window, and on macOS notifications do not come from Spartito itself), or
`pipx install --python python3.14 'spartito[app,menubar]'`. Upgrade with
`uv tool upgrade spartito` (it keeps the extras), remove with `uv tool uninstall spartito`.
To run a checkout instead, see [Development](https://github.com/foszto/spartito#development).
Installed before the command was renamed? See
[Upgrading from `orch`](https://github.com/foszto/spartito/blob/master/docs/usage.md#upgrading-from-orch). Installed from a checkout
before the distribution was renamed to `spartito`? See
[Upgrading to the distribution name `spartito`](https://github.com/foszto/spartito/blob/master/docs/usage.md#upgrading-to-the-distribution-name-spartito).

Spartito keeps its state in `~/.orchestrator/`. Set `ORCH_HOME` to use another directory.

## Quick start

Register a git repository as a project. Its currently checked-out branch becomes the
project's base branch, from which agent branches start:

```sh
spartito project add ~/src/my-app
# added my-app  /home/dev/src/my-app  (base branch: main)
```

**Through the orchestrator.** The recommended way: the dashboard as a background service, and
Spartito as an application:

```sh
spartito service install   # dashboard at http://127.0.0.1:8787, in the background and at login
spartito app install       # a launcher with Spartito's own name and icon
spartito app               # or open Spartito like any application
```

The service is a launchd agent on macOS and a systemd user unit on Linux, restarted when it
fails. The launcher is `~/Applications/Spartito.app` on macOS and a desktop entry on Linux;
notifications come from Spartito, and a click opens the agent or decision (macOS: once
Spartito is allowed to notify; Linux: with libnotify 0.7.10 or later). Both are undone with
`spartito service uninstall` and `spartito app uninstall`. The dashboard has no login: keep it
on the loopback address (the default), as anyone who can reach it controls agents that run
code ([docs/usage.md](https://github.com/foszto/spartito/blob/master/docs/usage.md#dashboard)). Details per platform:
[docs/desktop.md](https://github.com/foszto/spartito/blob/master/docs/desktop.md).

Without the service or the window (or where no window is available): `spartito web` serves
the dashboard at `http://127.0.0.1:8787` until Ctrl-C; open it in a browser.

Select **Maestro** and describe the goal, for example: *"In my-app, fix the login redirect
loop. Have a worker implement it with Claude and a reviewer check it with Codex."* The
orchestrator passes the brief to the project lead, which starts the agents. They appear in the
left-hand list as they start; click one to follow its conversation. When the lead's agents are
done, the lead reports to the orchestrator, which reports to you. Alternatively, `spartito chat`
opens the orchestrator as an interactive Claude Code session in the terminal: a separate
conversation with the same tools and the same state.

**Directly from the command line.**

```sh
spartito spawn -p my-app -n fix-login "Fix the redirect loop after login. Add a regression test."
# fix-login-3f2a  run 1  /home/dev/.orchestrator/worktrees/my-app/fix-login-3f2a

spartito logs fix-login-3f2a -f     # follow what it does
spartito wait fix-login-3f2a        # block until it is done
spartito status fix-login-3f2a      # result, commits and changes in its worktree
```

The worker commits on branch `orch/fix-login-3f2a`. Review and merge it yourself; agents do
not merge:

```sh
cd ~/src/my-app
git log --oneline main..orch/fix-login-3f2a
git merge orch/fix-login-3f2a
spartito archive fix-login-3f2a     # hide it from the lists; everything is kept
```

## How it fits together

Every agent is a headless Claude Code or Codex session with a role. A message to an agent
starts a *turn*: `spartito`, the dashboard or the MCP server records it in a SQLite database
under `~/.orchestrator/` and starts one detached runner process, which runs the CLI in the
agent's worktree and records its output as events. The orchestrator and the project leads
get the `orch` MCP tools to start agents, wait for them, keep the task board and ask you for
decisions; when an agent and its sub-agents are done, their parent receives their answers in
an automatic notification turn. The command line, the dashboard, the desktop window and the
MCP tools are views on the same database. More: [docs/concepts.md](https://github.com/foszto/spartito/blob/master/docs/concepts.md),
[docs/architecture.md](https://github.com/foszto/spartito/blob/master/docs/architecture.md).

## Documentation

- [docs/concepts.md](https://github.com/foszto/spartito/blob/master/docs/concepts.md): orchestrator, leads, roles, worktrees, turns, results.
- [docs/usage.md](https://github.com/foszto/spartito/blob/master/docs/usage.md): every command and option, and the dashboard.
- [docs/agents.md](https://github.com/foszto/spartito/blob/master/docs/agents.md): MCP tools, queue, dependencies, notifications, decisions, archiving, compaction, cost.
- [docs/configuration.md](https://github.com/foszto/spartito/blob/master/docs/configuration.md): model and effort, settings, loadout, roles, providers, files.
- [docs/board.md](https://github.com/foszto/spartito/blob/master/docs/board.md): the task board across projects.
- [docs/desktop.md](https://github.com/foszto/spartito/blob/master/docs/desktop.md): background service, installable page, notifications, `spartito app`, menu bar.
- [docs/desktop-checklist.md](https://github.com/foszto/spartito/blob/master/docs/desktop-checklist.md): manual checks of the desktop app.
- [docs/architecture.md](https://github.com/foszto/spartito/blob/master/docs/architecture.md): how it works at run time, limitations, code layout.
- [docs/releasing.md](https://github.com/foszto/spartito/blob/master/docs/releasing.md): the version, publishing to PyPI, the release workflow.
- [CONTRIBUTING.md](https://github.com/foszto/spartito/blob/master/CONTRIBUTING.md): setup, checks, conventions, pull requests, bug reports.
- [ROADMAP.md](https://github.com/foszto/spartito/blob/master/ROADMAP.md): where Spartito is heading, in phases, with the open questions.

## Platform support

Developed and used on macOS; Linux is supported, with parts not yet tested on a real machine;
Windows is not supported (process handling relies on POSIX process groups and sessions).
Details and other limitations:
[docs/architecture.md](https://github.com/foszto/spartito/blob/master/docs/architecture.md#platform-support-and-limitations); the desktop
features per platform: [docs/desktop.md](https://github.com/foszto/spartito/blob/master/docs/desktop.md#platform-support).

## Development

From a checkout of this repository, an install that runs the checkout's code:

```sh
uv tool install --editable '.[app,menubar]'    # puts `spartito` on the PATH
```

Or no install at all, prefixing every command with `uv run` inside the checkout
(`uv run spartito …`). The checks:

```sh
uv sync
uv run pytest                  # uses a fake provider and real git worktrees; no claude/codex calls
uv run pytest -n auto          # the same in parallel (pytest-xdist): about 6x faster, the usual way
uv run ruff check . && uv run ruff format --check .
node --check src/orchestrator/web/static/app.js
```

How to contribute, the checks a change must pass and how to report a bug:
[CONTRIBUTING.md](https://github.com/foszto/spartito/blob/master/CONTRIBUTING.md).

## License

MIT, see [LICENSE](https://github.com/foszto/spartito/blob/master/LICENSE).
