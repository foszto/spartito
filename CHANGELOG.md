# Changelog

The changes of each release of Spartito that matter to the people who use it. The format
follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/); how the version number is
chosen is in the
[versioning rule](https://github.com/foszto/spartito/blob/master/docs/releasing.md#the-version).

## [Unreleased]

### Added

- OpenCode (`opencode`, 2.2.3 or later; tested with 2.3.2) is the third supported harness,
  after Claude Code and Codex: projects and agents can run on it, and `spartito providers`
  checks its version, lists its models (outside any project) and reads which providers are
  connected (`opencode auth list`); with none listed it stays usable as `unknown`, since a key
  in a config file is not listed there. Each turn runs with a private opencode server, the
  role prompt every turn, its loadout as opencode permissions and policies, and leads get the
  `orch` MCP server; a project's `opencode.json` cannot replace the turn's permissions,
  shell or MCP servers. By default (`setting_sources` without `user`) the user's global
  opencode config is left out, and with it a provider keyed only there; logins and
  environment variables still work, and the agent's commands keep the user's own
  `XDG_CONFIG_HOME` and run in bash. Its cost and context size are reported; it cannot
  compact, so it shows no automatic compaction threshold, has no Claude in Chrome, does not
  get MCP servers added in Spartito yet, and only asks for an answer schema in the prompt
  ([providers.md](docs/providers.md#opencode-opencode)).
- `spartito providers` checks which of the supported harnesses, Claude Code, Codex and
  OpenCode, this machine can run: each CLI is looked for on `PATH` and in the usual install
  directories and asked its version, without a network request or an agent turn; only
  OpenCode's login is checked, by the providers it lists. `--json` adds each provider's
  features, limits and models.
- Every provider choice in the dashboard lists all supported providers: the add-project and
  new-agent dialogs, a project's **Default provider** in the **Models and effort** window
  (applied at once), and a `provider` select next to each agent's model and effort, the
  Maestro's included before its first message. A provider that is not available is disabled
  with the reason, a hint with **Check again** appears when the provider that would apply is
  not available, and the new-agent dialog starts on the role's provider when the role names
  one.
- `spartito project provider` changes the provider of a project's new agents; existing agents
  keep theirs. The dashboard's project settings and the MCP tool `set_project_provider` do the
  same.
- `spartito model AGENT --provider` moves an agent, the Maestro included, to another provider.
  Spartito keeps the agent's last conversation on each provider: the first move to a provider
  starts a new conversation there, and moving back continues the earlier one. Nothing is
  carried over between providers. The agent's own model and effort are cleared; its worktree,
  branch, board items and sub-agents stay. It asks first when the agent has a conversation
  (`--yes` skips the question), and says whether the next message continues an earlier
  conversation or starts a new one; the dashboard's `provider` select asks the same way.
- `spartito ask --provider` (and `provider` in the MCP tool `ask_project_lead`) chooses the
  provider of a new project lead.
- Agent capabilities: extras an agent's CLI gets for its turns, turned on and off globally,
  per project, per role and per agent; a role's `off` is final. The first one is `chrome`,
  Claude in Chrome's browser tools for Claude agents. It is off by default:
  `spartito capabilities on chrome` turns it on for every agent, `off` turns it off again, and
  `inherit` goes back to the default. It drives your real, signed-in browser, so read
  `docs/capabilities.md` first. `spartito capabilities` lists them with their status,
  `spartito capabilities show` and `spartito status` show what an agent's next turn gets,
  `spartito spawn --capabilities` and `capabilities` in the MCP tools `spawn_agent` and
  `send_message` set an agent's own (an agent can give only what its own turns have, also
  through the command run in its turn), and `spartito providers` has a `CAPABILITIES` column.
  The dashboard has a **Capabilities** box in its settings, an inherit / on / off choice per
  capability in an agent's setup panel and the new-agent dialog, and a badge on agents that
  have one. More capabilities can be registered with `ORCH_CAPABILITIES`.
- The loadout key `tool_search` (`auto`, `always`, `never`) decides when Claude agents load MCP
  tools on demand; `always` defers a lead's `orch` tools too, about 4,300 fewer prompt tokens
  per request (`docs/configuration.md`).
- MCP servers as capabilities: `spartito mcp add NAME --command CMD` (a local server) or
  `--url URL` (a remote one) adds an MCP server, which becomes the capability `mcp:NAME`, off
  until turned on with `spartito mcp on NAME` (or `spartito capabilities on mcp:NAME`) at the
  same levels as `chrome`. A Claude agent's turn with a server on gets it, with the `orch`
  server, in one private MCP configuration file (mode `0600`, removed when the turn ends),
  still under `--strict-mcp-config`; Codex agents skip it with a note. Env values, header
  values and the URL's query values must be `${VAR}` references, so Spartito stores no secret
  in them (the command, arguments and URL path are stored as given and must hold none; a URL
  with a fragment is refused), and
  Claude Code reads the values from the turn's environment. A server name starts and ends
  with a letter or digit and holds no `__`. A turn where a referenced variable is not set leaves the
  server out with a note such as `DOCS_DB_URL is not set`; the background service passes no
  such variable on, so its turns skip those servers (`docs/mcp.md#the-variables`). Only the
  owner adds and removes servers, in their own shell: never an agent, nor a `spartito chat`
  session. `spartito mcp` lists them with their
  levels and the status the last turn recorded, `spartito mcp show` gives one,
  `spartito mcp remove` removes one and its settings, `spartito capabilities` lists them in a
  section of their own, `spartito capabilities show` gives each one's last status, and
  `spawn --capabilities mcp:NAME` works. Read `docs/mcp.md` first: a server runs as you or
  receives the agent's tool calls.
- Providers registered with `ORCH_PROVIDERS` can declare more optional attributes:
  `min_version` (an older CLI is listed as `not_installed`), `reports_context`,
  `cache_effect`, `config_env`, `carries_mcp_servers` and `credit` (`docs/providers.md`).
- Board items can be deleted for good, with their history and decision links: the MCP tool
  `delete_board_item` (a lead: its own project only), `spartito board rm` and **Delete** in an
  item's detail on the dashboard.
- The top-level orchestrator can remove a project with the MCP tool `remove_project`, after
  you agree to delete the archived agents its first reply names.

### Changed

- `spartito project rm` and the dashboard's **Remove** no longer require a project without
  agents. They remove the project with its settings, budget, board items and decisions, and
  delete its archived agents (worktrees too, branches kept) after a confirmation that names
  them (`--yes` skips it). Agents that are not archived, and archived ones with unmerged or
  uncommitted work, still stop the removal, and the refusal names the commands that clear
  them. A removal that stops part way says what was deleted; running it again completes it.
  What the deleted agents had spent stays in the cost history under the project's name.
- `withdraw_decision` is no longer limited to an agent's own decisions: the top-level
  orchestrator and `spartito chat` may withdraw any open decision, and a lead those of the
  agents in its project. The decision notes who withdrew it, and its agent gets a message
  that no answer will come, unless it is archived.
- **Incompatible:** `spartito mcp` without a subcommand now lists the MCP servers instead of
  running Spartito's own `orch` MCP server on stdio; `spartito mcp serve` runs it. Spartito's
  agents and `spartito chat` never started it through the command and are not affected. If
  another tool starts it as `spartito mcp`, change its arguments to `mcp serve`
  (`docs/usage.md#mcp-servers`).
- New projects and agents get a provider that is available here. `spartito project add`
  without `--provider` takes the first available one instead of always `claude`, and prints
  it. An explicit provider that is not available is refused, naming the available ones. When
  a role's or a project's provider is not available, the agent runs on the first available one,
  and says so in the agent's log and in the reply: a `note:` line on the command line, a `note`
  in the MCP and JSON replies, and a message in the dashboard after starting an agent or
  sending a message.
- Codex can be chosen for the Maestro (in the dashboard, or with
  `spartito model orchestrator --provider codex`) and for project leads (through the project's
  default provider, or `spartito ask --provider`). The Maestro runs on the first available
  provider unless another is chosen.
- A role's `model` applies only to agents of the role's own provider.
- Every Claude turn runs with `CLAUDE_CODE_ENABLE_CFC=0` unless `chrome` is on for it, so that
  your own Claude in Chrome setting does not reach agents.
- Agents with a capability that brings MCP tools, such as `chrome`, use Claude Code's tool
  search when their role restricts the built-in tools: the browser tools' schemas are loaded on
  demand instead of being sent with every request, which takes the extra prompt tokens of
  `chrome` from about 11,600 to about 2,900 per request.
- `mcp = "inherit"` in a project's own files (its `.orch/loadout.toml`, `[<role>]` or
  `[default]`, and the role files in its `.orch/roles/`) is ignored, with an `info` event on
  the turn naming the file: a repository could otherwise give its agents every MCP server
  Claude Code would load, with your credentials. It still works from
  `~/.orchestrator/loadout.toml` and the role files in `~/.orchestrator/roles/`.
- The agent view's header is shorter: it keeps the name, status, role, the provider, model
  and effort drop-downs, the context bar and the branch. What switching the model or effort
  does to the prompt cache is behind an info button next to them, shown on hover, focus or a
  click. **Details** is now **Settings & usage**: the capability choices, the tokens and
  cost with the recent turns, the agent's facts and its changes, each in a group that folds
  up and stays as left while the page refreshes.

### Fixed

- A refused MCP tool call (an unknown agent, someone else's decision, a closed one) tells the
  agent why, instead of a bare `Error executing tool <name>`; an unexpected error keeps its
  type and message. `spartito chat` is told to ask you directly instead of calling
  `request_decision`, and that tool's refusal there says who records decisions.
- A turn whose CLI is missing fails with a clear message that says where it was looked for,
  and the CLIs are also found in the usual install directories when a background service or
  launcher has a shorter `PATH`.
- A second `spartito app` started while the first one's window is still starting is answered
  and its target shown, instead of waiting 5 seconds for "the window did not answer".

## [0.2.0] - 2026-11-13

### Added

- A read-only code view in the dashboard: **Code** in the header of an agent with a worktree
  shows the files changed against its base branch, committed and uncommitted, with each
  file's patch, and the worktree's files with their content. Only files git tracks or would
  track are shown; ignored files and `.git` never are, and nothing is edited.
- `spartito state backup` backs up the state, also while agents run: the database (checked
  after the copy), the home's `roles/` and `loadout.toml`, and with `--logs` the logs.
  `spartito state list` lists the backups.
- `spartito state restore` restores a backup into the home. It refuses while a turn is
  running or queued, and without `--yes` only says what it would do; the state it replaces is
  kept as a new backup first.
- `spartito cost` reports the spend of the recorded turns with a total, grouped with `--by`
  by project (the default), day (local time), agent, model or role. `--since` and `--until`
  limit it to a range of local days, both included; `-p` keeps one project, and `--json`
  prints JSON. Codex reports no cost: its tokens are counted and its cost shows as `n/a`.
- A cost report refuses a range of more than 3,660 days (about ten years) and a day at the
  very edge of the calendar (such as `--until 9999-12-31`) with an error. All time runs from
  the first recorded day to today, so it meets the limit too once the records reach back
  further than that.
- Budgets with `spartito budget list`, `spartito budget set` and `spartito budget remove`: one
  global budget and one per project, per day, month or in total. A desktop notification warns
  once per period at a threshold (80 % by default) and once when the amount is reached.
- An opt-in hard stop for a budget (`spartito budget set --hard-stop`, off by default): once
  the amount is reached, new message turns of the agents the budget counts are refused before
  they start. A running turn is never stopped, and notification and compaction turns still run.
- A **Cost** view in the dashboard: each budget with what is spent in its current period,
  then the spend over a range of local days: the last 7, 30 or 90 days with today, all time,
  or any from–to, the last 30 days by default. A chart stacks each day's spend by group (the
  top eight, the rest as one `other`) and shows cost or tokens; a day with turns that
  reported no cost (Codex) gets a hollow mark and is never drawn as $0. A legend names the
  groups and the mark. A table gives each group's totals over the range. It groups by
  project, model, role or agent, keeps one project or all, and is read-only: budgets are set
  with `spartito budget set`.
- `GET /api/cost` gives the report (`by`, `since`, `until`, `project`), with every day of the
  range and the spend per day and group, and the budgets with their state.
- `spartito --version` prints the version; the dashboard's settings show it too.
- `spartito version` prints the version and the kind of install. `spartito version --check`
  asks PyPI whether a newer release exists, only when typed: nothing checks for updates on its
  own.
- `spartito upgrade` prepares an upgrade: it refuses while a turn is running or queued, backs
  the state up, and prints the command that upgrades this kind of install and what to restart
  afterwards. It installs nothing itself and runs no git. `--dry-run` prints the plan without
  a backup.
- The database gets the table `budgets` and the index `run_usage_created`, added when it is
  opened. The change is additive: older versions keep opening the database.

### Fixed

- Stopping an agent at the moment a turn had just been queued for it (such as the notice that
  one of its agents finished) could miss that turn: it started after the stop and ran. It now
  ends as stopped.

## [0.1.0] - 2026-11-13

### Added

- The first public release, on PyPI as `spartito`.

[Unreleased]: https://github.com/foszto/spartito/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/foszto/spartito/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/foszto/spartito/releases/tag/v0.1.0
