# Architecture

How Spartito works at run time, its platform support and limitations, and how the code is
laid out.

- [How it works at run time](#how-it-works-at-run-time)
  - [Processes](#processes)
  - [State](#state)
  - [Logs](#logs)
  - [Permissions and providers](#permissions-and-providers)
- [Platform support and limitations](#platform-support-and-limitations)
- [Code layout](#code-layout)
  - [Modules](#modules)
  - [Layers](#layers)

## How it works at run time

### Processes

There is no daemon. `spartito`, the dashboard and the MCP server write to a SQLite database and
start one detached runner process (`python -m orchestrator.runner <run-id>`) per turn. The
runner waits for dependencies, creates the worktree if needed, runs `claude -p
--output-format stream-json` or `codex exec --json`, and turns the output into uniform events.
Every turn is its own process, and the command line, the dashboard and the MCP server read
the same database, so they can be used side by side.

### State

State lives under `~/.orchestrator/` (or `$ORCH_HOME`):

- `orch.db`: projects, agents, turns, events, usage, settings, board, decisions, notices;
- `worktrees/<project>/<agent-id>/`: the agents' worktrees;
- `workspace/`: the orchestrator's working directory;
- `run/`: files about live processes: the running dashboard server's `web.json`, locks and
  markers, the browser profile of `spartito app`;
- `backups/`: backups of the state ([state.md](state.md)).

The full list of files: [configuration.md](configuration.md#files).

### Logs

Per turn in `logs/run-<id>/`: the CLI's raw output stream, stderr, `result.md` (the full
final answer, or the error of a failed turn) and `result.json` for a structured answer. On
macOS the background service writes to `logs/web.log`; the window of `spartito app` writes to
`logs/app.log`.

### Permissions and providers

Claude agents run with `--permission-mode auto`, Codex agents in the `workspace-write`
sandbox; `spartito spawn --permission` overrides this per agent.

Providers implement a small protocol (`providers/base.py`); further providers can be
registered with `ORCH_PROVIDERS="name=package.module:Class"`. Details:
[configuration.md](configuration.md#providers-and-permissions).

## Platform support and limitations

- **Platforms.** Developed and used on macOS. On Linux, the sleep inhibitor
  (`systemd-inhibit`) and the cleanup of a stopped turn's processes have not been tested on a
  real machine, nor have the launcher and `notify-send` notifications. Windows is not
  supported: process handling relies on POSIX process groups and sessions. The desktop
  features per platform: [desktop.md](desktop.md#platform-support).
- **Codex.** Codex reports no cost. The context size is known only from the session's rollout
  file under `$CODEX_HOME/sessions/`. `/compact` does not work in `codex exec resume`, so a
  compaction is a turn with a lowered `model_auto_compact_token_limit`; it takes no
  instructions and needs a known context size. Codex reports token counts per session, so
  per-turn figures are derived by subtraction; the tokens of a turn that failed or was stopped
  without a result are counted in the agent's next turn (the agent's total is correct).
  Details: [agents.md](agents.md#context-and-compaction).
- **Notifications.** A lead's answer reaches its parent only once the lead and all of its
  agents are idle.
- **Role prompts.** A Codex agent receives its role prompt in the first turn of its session
  and keeps that version; a running `spartito chat` session keeps the prompt it started with.
  Headless Claude agents receive the current role prompt with every turn.
- **Upgrades.** A running `spartito web` keeps the code it was started with; restart it after an
  upgrade (for the background service: `spartito service install` again). New turns start new
  runner processes and use the new code.
- **Automatic compaction** pauses for an agent when a compaction did not bring its context
  below the threshold ([agents.md](agents.md#automatic-compaction)).
- **Loadout changes** do not shrink existing sessions: the earlier tool and skill lists stay in
  their conversation history ([configuration.md](configuration.md#notes)).

Limitations of single features: [desktop.md](desktop.md#known-limitations),
[board.md](board.md#known-limitations).

## Code layout

### Modules

```
src/orchestrator/
  cli.py           the `spartito` command
  __main__.py      `python -m orchestrator`: the same command line (what the background service runs)
  mcp_server.py    the MCP tools of the orchestrator and the leads
  web/             dashboard: one page and a JSON API
    app.py           the page and the JSON API, with the guards against other origins
    board_api.py     the JSON API of the board view across projects
    code_api.py      the JSON API of the code view of an agent's worktree (read-only)
    serve.py         running the dashboard server, shared by `spartito web` and `spartito app`
  gui/             `spartito app`: the window (window.py), finding a running server (locate.py),
                   the launcher (launcher.py), notifications posted by Spartito itself
                   (notifier.py); `spartito menubar` (menubar.py)
  app_open.py      deep-link requests to the open window of `spartito app`
  autostart.py     `spartito service`: the launchd agent or systemd user unit
  service.py       the facade `Orchestrator`, used by the CLI, the MCP server and the dashboard
  core/            the operations behind the facade, one module per area:
    dispatch.py      creating agents, queueing turns with dependencies, withdrawing a message
    launcher.py      starting and stopping runner processes
    lifecycle.py     the steps after a turn ends (compaction check, notifications)
    compaction.py    compacting a conversation, by hand and automatically
    archive.py       archiving, restoring and deleting agents; the automatic archive check
    setup.py         model and effort: settings, overrides, what the next turn uses
    views.py         agent views, listings, run history and context, waiting until settled
    delivery.py      waiting for agents and delivering each result once per caller
    access.py        who calls through the MCP server and in which project it may act
    reads.py         plain store reads the interfaces show
    notices.py       notices for whoever shows desktop notifications (page, Spartito)
    boards.py        the task board across projects: the joined view, placing, dispatching
                     named items, an item's history
    board_git.py     the cached git state of the board items' branches
    code_view.py     an agent's worktree, read-only: changed files, patches, files, contents
    state.py         backups of the state: making, listing and restoring them
  runner.py        one turn in its own detached process
  store.py         SQLite (WAL): the only shared state
  providers/       base.py (protocol), claude.py, codex.py
  notify.py        notification turns to parents
  replies.py       results cut to a size budget for agents
  models.py        Project, Agent, Run (one turn), Event
  timeline.py      one agent's runs as one consistent read (`AgentRuns`)
  textfmt.py       short text renderings: token counts, one-line previews
  errors.py        the exceptions the interfaces catch from the core
  roles.py         role loading; roles/*.md are the packaged roles
  loadout.py       what an agent loads (tools, MCP servers, setting sources)
  memory_guard.py  hook that confines the `memory` key's file writes to the memory directory
  settings.py      tiers and settings, global and per project
  board.py         task board: one item's rules
  decisions.py     decisions
  keep_awake.py    sleep inhibitor
  desktop.py       desktop notifications: the one route of a notice (Spartito's window, its
                   launcher's app, a page, notify-send, `osascript`), its helper and trace
  git.py           worktree handling
  config.py        paths under `~/.orchestrator/` (or `$ORCH_HOME`)
```

### Layers

The interfaces (`cli`, `mcp_server`, `web`, `gui`) use only the facade: they neither import
the store nor touch a `.store` attribute. A module imports only from its own layer or a lower
one, in the order interfaces > runner > core > domain > infra, without cycles;
`tests/test_architecture.py` enforces both from the source, with a pinned list of today's
exceptions that may only shrink.
