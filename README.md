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

The command is `orch`; the Python package is `orchestrator`.

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
  notifications whose click opens the agent or decision, and its own window (`orch app`).
- No daemon: every turn is its own process; all state is in one SQLite database.

## Requirements

- macOS or Linux (see [Platform support](#platform-support)).
- Python 3.14 and [uv](https://docs.astral.sh/uv/).
- `git`.
- The `claude` (Claude Code) CLI, installed and logged in, and optionally the `codex` CLI.
  The defaults use Claude Code: the orchestrator agent, `orch chat`, and every project
  registered without `--provider`, so the quick start below needs `claude`. A Codex-only or
  mixed setup is configured per project and per agent:
  [docs/configuration.md](docs/configuration.md#providers-and-permissions).

## Installation

From a checkout of this repository:

```sh
uv tool install --editable .    # puts `orch` on the PATH
orch --help
```

Without installing, prefix every command with `uv run` inside the checkout (`uv run orch …`).
The native window of `orch app` needs the optional extra `app`, the macOS menu-bar item the
extra `menubar` (on Linux, `app` brings GPL-3.0-only PyQt6; see
[docs/desktop.md](docs/desktop.md#optional-dependencies)):

```sh
uv tool install --editable '.[app,menubar]'
```

Spartito keeps its state in `~/.orchestrator/`. Set `ORCH_HOME` to use another directory.

## Quick start

Register a git repository as a project. Its currently checked-out branch becomes the
project's base branch, from which agent branches start:

```sh
orch project add ~/src/my-app
# added my-app  /home/dev/src/my-app  (base branch: main)
```

**Through the orchestrator.** Start the dashboard:

```sh
orch web          # dashboard at http://127.0.0.1:8787
```

The dashboard has no login: keep it on the loopback address (the default), as anyone who can
reach it controls agents that run code ([docs/usage.md](docs/usage.md#dashboard)).

Select **Maestro** and describe the goal, for example: *"In my-app, fix the login redirect
loop. Have a worker implement it with Claude and a reviewer check it with Codex."* The
orchestrator passes the brief to the project lead, which starts the agents. They appear in the
left-hand list as they start; click one to follow its conversation. When the lead's agents are
done, the lead reports to the orchestrator, which reports to you. Alternatively, `orch chat`
opens the orchestrator as an interactive Claude Code session in the terminal: a separate
conversation with the same tools and the same state.

**Directly from the command line.**

```sh
orch spawn -p my-app -n fix-login "Fix the redirect loop after login. Add a regression test."
# fix-login-3f2a  run 1  /home/dev/.orchestrator/worktrees/my-app/fix-login-3f2a

orch logs fix-login-3f2a -f     # follow what it does
orch wait fix-login-3f2a        # block until it is done
orch status fix-login-3f2a      # result, commits and changes in its worktree
```

The worker commits on branch `orch/fix-login-3f2a`. Review and merge it yourself; agents do
not merge:

```sh
cd ~/src/my-app
git log --oneline main..orch/fix-login-3f2a
git merge orch/fix-login-3f2a
orch archive fix-login-3f2a     # hide it from the lists; everything is kept
```

## How it fits together

Every agent is a headless Claude Code or Codex session with a role. A message to an agent
starts a *turn*: `orch`, the dashboard or the MCP server records it in a SQLite database
under `~/.orchestrator/` and starts one detached runner process, which runs the CLI in the
agent's worktree and records its output as events. The orchestrator and the project leads
get the `orch` MCP tools to start agents, wait for them, keep the task board and ask you for
decisions; when an agent and its sub-agents are done, their parent receives their answers in
an automatic notification turn. The command line, the dashboard, the desktop window and the
MCP tools are views on the same database. More: [docs/concepts.md](docs/concepts.md),
[docs/architecture.md](docs/architecture.md).

## Documentation

- [docs/concepts.md](docs/concepts.md): orchestrator, leads, roles, worktrees, turns, results.
- [docs/usage.md](docs/usage.md): every command and option, and the dashboard.
- [docs/agents.md](docs/agents.md): MCP tools, queue, dependencies, notifications, decisions, archiving, compaction, cost.
- [docs/configuration.md](docs/configuration.md): model and effort, settings, loadout, roles, providers, files.
- [docs/board.md](docs/board.md): the task board across projects.
- [docs/desktop.md](docs/desktop.md): background service, installable page, notifications, `orch app`, menu bar.
- [docs/desktop-checklist.md](docs/desktop-checklist.md): manual checks of the desktop app.
- [docs/architecture.md](docs/architecture.md): how it works at run time, limitations, code layout.

## Platform support

Developed and used on macOS; Linux is supported, with parts not yet tested on a real machine;
Windows is not supported (process handling relies on POSIX process groups and sessions).
Details and other limitations:
[docs/architecture.md](docs/architecture.md#platform-support-and-limitations); the desktop
features per platform: [docs/desktop.md](docs/desktop.md#platform-support).

## Development

```sh
uv sync
uv run pytest                  # uses a fake provider and real git worktrees; no claude/codex calls
uv run pytest -n auto          # the same in parallel (pytest-xdist): about 6x faster, the usual way
uv run ruff check . && uv run ruff format --check .
node --check src/orchestrator/web/static/app.js
```

## License

MIT, see [LICENSE](LICENSE).
