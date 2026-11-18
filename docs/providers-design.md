# Providers: less duplication, and opencode as a third provider

> **Status: a design, not built.** Nothing described here as new exists yet: the modules,
> attributes and the `opencode` provider named below are planned. What exists today is
> described in [providers.md](providers.md), [configuration.md](configuration.md#loadout),
> [capabilities.md](capabilities.md) and [mcp.md](mcp.md#what-works-today).
>
> Facts about opencode are marked:
> - **[documented]**: in opencode's public docs (URL given);
> - **[unverified]**: not in the docs. "Seen in source" means it was read in the public
>   repository's code on its default branch at the time of writing (URL given). It can change
>   in any release, and a real run has to confirm it;
> - **[observed]**: seen in the help output of the installed CLI, without running a session.
>
> opencode was installed on the research machine: version **2.3.2**. Only `--version` and
> `--help` output was read, with opencode's config, data, cache and state directories pointed
> at an empty temporary directory.

## Contents

1. Current state of the provider layer
2. opencode: what it offers a headless orchestrator
3. Target design
4. The opencode adapter
5. Phases, risks, verification, decided questions

---

## 1. Current state

Line numbers refer to the provider layer as it is once MCP servers have become
capabilities (the change that added [mcp.md](mcp.md#each-server-is-a-capability)). They shift with later edits.

### 1.1 The layer

| file | what it holds |
|---|---|
| `providers/__init__.py` | `BUILTIN = (ClaudeProvider, CodexProvider)` and `PROVIDERS` (13–14). `ORCH_PROVIDERS="name=module:Class"` plugins (17–25), `get()` |
| `providers/base.py` | `Wiring`/`Support` (35–58), `wired()`/`wired_servers()` (61–77), `${VAR}` references (80–100), `RunContext` (103–125), `Invocation` (128–140), the `Provider` protocol with optional attributes read through `getattr` (143–192), `model_choices` (195–201), `brief` (204–207) |
| `providers/claude.py` | 566 lines: parent-session variables (36–46), `chrome` support (59–62), `ToolSearch` (68, 260–275), MCP entries and statuses (74–98), the model list over an SDK `initialize` request (101–214), the memory guard settings and canary (227–257), loadout flags (285–307), `invocation` (350–411), the stream parser (413–554), `/compact` (560–566) |
| `providers/codex.py` | 298 lines: `-c key=value` TOML overrides (49–53), `codex debug models` (34–69), `invocation` (108–144), the parser (146–209), compaction through a lower auto-compact limit (25–28, 215–218), the context size from the rollout file (221–298) |
| `providers/locate.py` | finding the CLI, the extra install directories, `probe()` for `--version` and an optional login check (24–210) |

**How the rest of the code uses it**

- **Runner** (`runner.py`):
  - builds a `RunContext` and calls `provider.invocation()` (234–258).
  - calls it a second time when a server is left out for an unset variable (255–258), and a third time without `memory` when hooks are off (`_plain`, 603–607).
  - writes `Invocation.private_files` (`PrivateFiles`, 531–585) and waits for `Invocation.canary` (687–703).
  - feeds every stdout line to `provider.parse()` (333) and dispatches on the normalised event kinds `session`, `context`, `compact`, `result`/`notification_result`, `text`, `error` (334–359).
  - reads `compact_needs_size` and `context_after` with `getattr` (210, 386).
  - puts Claude's and Codex's config-home variables into the `orch` MCP server's environment (`MCP_PASSED_ENV`, 171–177).
- **Loadout** (`loadout.py`): the keys are `tools`, `mcp`, `setting_sources`, `slash_commands`, `codex_user_config`, `memory`, `tool_search` (53–61). They are named after Claude flags, plus one key named after Codex. Each provider reads the keys it understands: Claude in `loadout_flags`/`tool_search`, Codex only `codex_user_config`.
- **Capabilities** (`capabilities.py`):
  - `Capability.providers` maps a provider name to a `Support` (72).
  - `chrome` names `"claude"` (116) and imports `CHROME` from `providers.claude` (47).
  - every `mcp:<name>` capability is built for `"claude"` only (199).
- **Availability** (`core/availability.py`):
  - `probe()` runs `executable` + `version_args` (57–66) and optionally `login_args` (`locate.probe`, 171–210).
  - `describe()` builds the JSON features from `getattr` lookups. It sets `context_size` true only for built-in classes (127, 138).
- **Model and effort**:
  - `Provider.efforts` is a static tuple, checked in `settings.clean` (233), `core/dispatch.py` (131), `core/setup.py` (222) and `cli.py` (293).
  - `settings.check_model` checks that a model name is one safe word.
  - Model lists come from `provider.models()`, cached inside each provider.
- **Sessions**:
  - the session id comes from the stream's `session` or `result` event (`store.set_session`, `runner.py` 334–336).
  - `agents.provider_sessions` keeps one session per provider (`store.py` 879–910). That code is generic.
- **Compaction**: `core/compaction.py` calls `compact_prompt` (64–73) and checks `compact_needs_size` (183–187). The runner judges success by a `compact` event or by `context_after` (398–403).
- **Structured answers**:
  - Claude: `--json-schema`; the result carries `structured_output`.
  - Codex: `--output-schema <file>`.
  - Runner fallback: when no structured value came and the run has a schema, `json.loads(text)` (406–411).
- **Cost and usage**:
  - `Event.usage`/`Usage.cumulative`: Claude reports per invocation (or session totals on older CLIs), Codex reports thread totals.
  - `combine_results`/`record_usage` (897–1025) turn totals into per-turn figures generically.
- **Event stream** (`models.py`): `Event(kind, text, session_id, cost_usd, is_error, structured, model, usage, windows, mcp)`. The kinds are listed above. `NOTIFICATION_RESULT` is Claude's (`base.py` 23).
- **MCP wiring**:
  - Claude: the `orch` MCP server inline in `--mcp-config`, or with `mcp:<name>` servers in a private file `--mcp-config <path>`. Also `alwaysLoad` and `ToolSearch` (`claude.py` 362–397).
  - Codex: the `orch` MCP server through `-c mcp_servers.orch.*` (123–130). Codex ignores `Wiring.mcp_servers` without saying so (137); the capability layer already marks those capabilities as skipped.
  - `${VAR}` references are checked before the turn in Claude Code's syntax (`base.py` 80–100), against the entry as `mcp.Server.entry()` gives it (Claude's `mcpServers` shape).
- **Memory guard and hooks**: Claude only (`claude.py` 227–257, `memory_guard.py`, the runner's canary path). The runner side is generic: it follows `Invocation.canary`.
- **Permission modes**: `Agent.permission` is a free string. Claude: `--permission-mode` (default `auto`, 48, 357). Codex: `sandbox_mode` (default `workspace-write`, 21, 116). The help text of `spawn --permission` names both (`cli.py` 1172).
- **Attribution**: `roles._CREDITS` keyed `"claude"`/`"codex"` (`roles.py` 56–69). `attribution()` returns "" for other providers.
- **Surfaces**:
  - the `providers` command, `GET /api/providers` and every provider picker read `availability.listing()`.
  - the dashboard colours avatars per provider name (`app.css` 68–72).
  - `spartito chat` is Claude Code only (`cli.py` 283–329).
- **Tests**:
  - `tests/fakeprovider.py`: `FakeProvider` (209), `FirstTurnFake` (278), and `FakeClaude` (343), which runs a Python script in place of `claude`.
  - `tests/conftest.py`: fake `claude` and `codex` executables first on `PATH` (`PROVIDER_CLIS`, 382) and a recorded Codex model list (354, 413).
  - fixtures: `tests/fixtures/claude_*.jsonl`, `codex_*.jsonl`.
  - provider tests: `test_providers.py`, `test_availability.py`, `test_provider_choice.py`, `test_provider_move.py`, `test_dashboard_providers.py`, `test_docs_providers.py`, `test_attribution.py`, `test_tool_search.py`, `test_mcp_wiring.py`, `test_runner_memory.py`, `test_context.py`.

### 1.2 What is duplicated between `claude` and `codex`

| # | what | where | kind |
|---|---|---|---|
| D1 | decode one JSON line; a non-JSON line becomes an `info` event | `claude.py` 413–417 = `codex.py` 146–150 | identical code |
| D2 | model list cached per process, behind a lock, asked again after 600 s when it failed | `claude.py` 103–105, 322–348; `codex.py` 29–31, 90–106 | same pattern, different retry rule |
| D3 | run a CLI briefly with the extra install directories on `PATH` and a timeout | `claude.py` 111–150 (kills the process group), `codex.py` 34–46 (does not), `locate.py` 134–163 | three versions |
| D4 | the entry for Spartito's own `orch` MCP server: command, args, env | `claude.py` 372–381, `codex.py` 123–130, `cli.py` 251–259 | same data, three shapes |
| D5 | fresh versus resumed session; `attach_command` | `claude.py` 353–356, 556–557; `codex.py` 111–112, 211–212 | differs only in data |
| D6 | model and effort flags | `claude.py` 364–367; `codex.py` 119–122 | data (`--model`/`--effort` against `-m`/`-c model_reasoning_effort=`) |
| D7 | capability wirings: argv and env | `claude.py` 368–369; `codex.py` 137–138 | identical; Codex drops `mcp_servers` silently |
| D8 | output schema | `claude.py` 398–399 (inline); `codex.py` 133–136 (file in scratch) | two strategies |
| D9 | role prompt placement | `claude.py` 370–371 (flag, every turn); `codex.py` 140–143 (prefix of the first prompt) | two strategies |
| D10 | self-description attributes | `claude.py` 311–320; `codex.py` 73–88 | data |
| D11 | the CLI's usage mapped to `Usage` | `claude.py` 501–532; `codex.py` 168–184 | same target, different by nature |
| D12 | the default permission | `claude.py` 48; `codex.py` 21 | data |

### 1.3 What is provider-specific by nature (keep it explicit)

- **Claude**:
  - the stream-json prompt and the memory canary (`claude.py` 278–282, 401–403);
  - the memory guard and `--settings` hooks;
  - tool search and `alwaysLoad`;
  - background-task notification results;
  - the model list over an SDK `initialize` request;
  - `compact_boundary`, and per-model `contextWindow`;
  - parent-session variables.
- **Codex**:
  - every option as a `-c` TOML override, because `exec resume` takes few flags;
  - compaction by lowering `model_auto_compact_token_limit`;
  - the context size read from the rollout file;
  - thread totals for usage;
  - `--ignore-user-config`.

### 1.4 Places outside `providers/` that name a provider (the cost of adding one)

| place | what | change needed for a new provider |
|---|---|---|
| `roles.py` 56–69 | `_CREDITS` per provider | without an entry: no attribution section |
| `core/setup.py` 45–48 | `CACHE_EFFECT` per provider | falls back to "may restart" |
| `capabilities.py` 47, 116, 199 | `chrome` for `"claude"`; every `mcp:` capability for `"claude"` | MCP servers cannot reach a new provider without editing this |
| `runner.py` 171–177, `autostart.py` 38 | `CODEX_HOME`, `CLAUDE_CONFIG_DIR` passed on | the new provider's config-home variables are lost under the service and in sub-agents |
| `core/availability.py` 127, 138 | `context_size` true only for `BUILTIN` classes | none, but the flag is not the provider's own |
| `cli.py` 22, 251–259, 283–329 | `chat` builds Claude flags with `providers.claude` helpers | none (chat stays Claude only) |
| `cli.py` 702 | `config set --provider` defaults to `claude` | none |
| `cli.py` 1172, 1464, 1529–1533, 2098–2204; `web/index.html` 150; `web/static/app.js` 1420–1527 | help and UI texts that name Claude and Codex | wording |
| `mcp_server.py` 106, 152, 655 | docstrings list `claude` or `codex` | agents are not told about the new name |
| `models.py` 38 | `Project.default_provider = "claude"` | none (a stored default) |
| `web/static/app.css` 68–72 | avatar colours per name | a new provider gets no colour |
| `loadout.py` 15, 58, 73, 92 | the key `codex_user_config` | a provider-named key per provider |
| `mcp.py` 28 | `RESERVED` names are Claude Code's | none |
| `memory_guard.py` | Claude's memory directory | none (Claude only) |
| `tests/conftest.py` 382, 413 | `PROVIDER_CLIS = ("claude", "codex")` | **must** add a fake for the new CLI, or a test could start the real one |
| `tests/test_architecture.py` 65–69 | an import exception per provider module | a new module needs one, or must avoid `models`/`loadout` imports |
| docs | providers.md, configuration.md, capabilities.md, mcp.md, architecture.md | tables per provider |

---

## 2. opencode

Sources:
- the docs at <https://opencode.ai/docs/>;
- the repository <https://github.com/anomalyco/opencode> (default branch `dev`);
- `--help` output of 2.3.2.

**Two lines.** opencode 2.x ("V2") is not compatible with 1.x:
- V2 changes config keys, the permission model and the server API. **[documented]** <https://opencode.ai/docs/migrate-v1/>
- V2 installs as the Homebrew formula `opencode-v2` and through `/v2/install`; mise before 2026.10.9 installs 1.x. **[documented]** <https://opencode.ai/docs/>
- The repository's release page still marks a 1.19.x release "Latest" next to 2.3.x releases. **[observed on the releases page]**

**This design targets 2.x only.**

### 2.1 Non-interactive mode

- `opencode run [flags] [<message...>]` takes these flags: `--continue/-c`, `--session/-s <id>`, `--fork`, `--worktree/-w`, `--model/-m provider/model#variant`, `--agent`, `--format default|json`, `--file/-f`, `--title`, `--archived`, `--thinking` and `--auto`. The global flags `--standalone`, `--server <url>`, `--log-level`, `--print-logs` apply too. **[observed]**
- The docs show only `opencode run "<prompt>"`. **[documented]** <https://opencode.ai/docs/cli/>
- **The prompt**: positional words and, when stdin is not a TTY, stdin. Stdin is read fully when there is no positional message, and joined with a newline when both are given. An empty message fails. **[unverified; seen in source]** <https://github.com/anomalyco/opencode/blob/dev/packages/cli/src/run/run.ts>
- **JSON output**: one object per line, `{type, timestamp, sessionID, …}`. **[unverified; seen in source]** <https://github.com/anomalyco/opencode/blob/dev/packages/cli/src/run/noninteractive.ts>
  - `step_start`: `part` with `messageID`, `snapshot`.
  - `text` and `reasoning` (`reasoning` only with `--thinking`): `part.text` is **the new suffix since the last emission**, not the whole text.
  - `tool_use`: `part.tool`, `part.state` with `status` `completed` (with `input`, `output`, `title`) or `error` (with `input`, `error`).
  - `step_finish`: `part.reason`, `part.cost`, `part.tokens`.
  - `retry`: `attempt`, `nextRetryAt`, `error`.
  - `background_wait`: `jobs`.
  - `error`: `{type, message}`.
  - Every line carries the session id.
- **Tokens**: `{input, output, reasoning, cache: {read, write}}`. **[unverified; seen in source]** <https://github.com/anomalyco/opencode/blob/dev/packages/schema/src/token-usage.ts> Whether `input` includes cache reads is unknown.
- **How a turn ends**: the process waits until the session's execution has succeeded, failed or been interrupted. It then waits for the prompt's background jobs and sub-sessions, and exits. **[unverified; seen in source]**
- **Exit codes**: 1 with a final `error` line on failure, 0 otherwise. On SIGINT or SIGTERM it interrupts the session and its sub-agents. **[unverified; seen in source]**

### 2.2 Sessions

- **Ids**:
  - session ids start with `ses`; new ones are `ses_` followed by a generated suffix. **[unverified; seen in source]** <https://github.com/anomalyco/opencode/blob/dev/packages/schema/src/session-id.ts>
  - `--session <id>` resumes an existing session. A missing one is **created with that id**, unless `--fork` is given. **[unverified; seen in source]** <https://github.com/anomalyco/opencode/blob/dev/packages/cli/src/session-target.ts>
  - So Spartito can choose the id up front, as it does with Claude's `--session-id`. A resumed id whose session was deleted silently starts an empty conversation.
- **`--continue` and `--fork`**: `--continue` picks the newest top-level session of the exact directory. `--fork` copies a session first. **[unverified; seen in source]**
- **Storage**:
  - a SQLite database, normally `~/.local/share/opencode/opencode.db`. **[documented]** <https://opencode.ai/docs/troubleshooting/>
  - `OPENCODE_DB` moves it. **[documented]** <https://opencode.ai/docs/cli/>
- **Session commands**: `opencode session list|delete|export|import`. **[observed]**
- **Archived sessions**: `--archived` keeps a new session out of session lists. **[observed]**

### 2.3 Models and effort

- **Model ids**:
  - `provider/model` with an optional `#variant`. The provider ends at the first `/`; ids are case-sensitive catalogue ids. **[documented]** <https://opencode.ai/docs/models/>
  - The default is `model` in the config if enabled and available, otherwise "the newest available supported model". **[documented]** (same page)
- **Variants**:
  - variants are named per model ("often for reasoning effort"). `low`, `high` and `max` are examples, and not every model has them.
  - an unknown variant is a model-resolution error.
  - there is no `--variant` flag: the variant goes into `--model …#variant`. **[documented]** (same page)
  - a variant without a model fails ("Cannot select a variant before selecting a model"). **[unverified; seen in source]**
- **Listing models**: `opencode models` exists, with no options in 2.3.2. **[observed]** Its output format, whether it shows variants, and whether it works offline (`OPENCODE_DISABLE_MODELS_FETCH`, **[unverified; seen in source]**) are not documented.

### 2.4 System prompt and instructions

- **Agents**:
  - agents are defined under `agents.<id>` in config or in markdown files. **[documented]** <https://opencode.ai/docs/agents/>
  - an agent's `system` **replaces** the provider's base prompt; project instructions are still added. **[documented]** (same page)
  - built-in agents: `build` (default), `plan`, `general`, `explore`. **[documented]**
- **Instruction files**:
  - `AGENTS.md` from the working directory up to home or the project root;
  - `CLAUDE.md` where a directory has no `AGENTS.md`;
  - a global `~/.config/opencode/AGENTS.md`, or `~/.claude/CLAUDE.md` in its place. **[documented]** <https://opencode.ai/docs/instructions/>
- **`instructions`**: files, globs (absolute paths allowed) or URLs, loaded **after** the `AGENTS.md` files. Entries from all config documents are combined. **[documented]** (same page)
- **Switches**: `OPENCODE_DISABLE_CLAUDE_CODE_PROMPT`, `OPENCODE_DISABLE_CLAUDE_CODE` and `OPENCODE_DISABLE_PROJECT_CONFIG`. The last one skips project `AGENTS.md` discovery. **[documented]** (same page)
- **No append flag**: there is no flag or variable that appends to the system prompt per run. **[documented by absence]** An `instructions` entry pointing at a per-turn file is the equivalent.

### 2.5 Tools, permissions, approvals

- **Built-in tools**: `read`, `glob`, `grep`, `edit`, `write`, `patch`, `shell`, `webfetch`, `websearch`, `question`, `skill`, `subagent`, `execute` (Code Mode), the browser namespace, and `opencode` namespace tools (`session_move`, `worktree_create`, …). **[documented]** <https://opencode.ai/docs/tools/>
- **Permission rules**:
  - `permissions` is an ordered list of `{action, resource, effect}` with `allow`, `ask` or `deny`. The last matching rule wins.
  - the order is lower-priority config, then global rules, then agent rules.
  - `edit` covers `edit`, `write` and `patch`; MCP tools are `<server>_<tool>`.
  - the base policy allows `*`, asks for `*.env` reads, and allows `external_directory`. **[documented]** <https://opencode.ai/docs/permissions/>
- **Policies**: `policies` hold hard `allow`/`deny` statements for `tool.use`, `mcp.use`, `plugin.use`, `skill.use`, `provider.use`, …. Broader config wins, so a repository cannot lift a global deny. `mcp.use`, `plugin.use` and `skill.use` need 2.2.3 or later. **[documented]** <https://opencode.ai/docs/policies/>
- **An `ask` without a person**:
  - without `--auto`, `opencode run` **rejects** every permission request of its session tree and goes on. With `--auto` it approves once. **[unverified; seen in source]**
  - the docs only say that non-interactive clients decide. **[documented]**
- **Questions and input forms**:
  - a `question` is cancelled and the model continues. **[unverified; seen in source]**
  - any other input form, such as an MCP elicitation, is cancelled and **fails the run**. **[unverified; seen in source]**
- **No allowlist**: there is no tool allowlist like Claude's `--tools`. A role's restricted list becomes `deny` rules and policies.

### 2.6 MCP

- **Local servers**: `mcp.servers.<name>` with `type: "local"`, `command` (an array), `environment`, `cwd`, `disabled`, `timeout` (`startup`, `catalog`, `execution`; default execution 12 h) and `codemode` (default true). **[documented]** <https://opencode.ai/docs/mcp-servers/>
- **Remote servers**: `type: "remote"` with `url`, `headers` and `oauth`. **[documented]** (same page)
- **References**: `{env:NAME}` in strings. An unset variable gives an empty string. `$NAME` is not expanded. **[documented]** <https://opencode.ai/docs/config/>
- **Tool names**: `<server>_<tool>`, for example `orch_spawn_agent` where Claude has `mcp__orch__spawn_agent`. Per-tool control goes through `permissions`; per-server control through `disabled` and `mcp.use` policies. **[documented]**
- **Deferred schemas**: none documented. **Code Mode** (default) groups a server's tools behind `execute`. Whether it keeps their schemas out of every request, as Claude's tool search does, is **[unverified]**.
- **Commands**: `opencode mcp list|add|auth|logout`. **[observed]**

### 2.7 Working directory and worktrees

- **Directory**: `opencode run` takes its directory from **`$PWD`**, falling back to the process's cwd. **[unverified; seen in source]** A launcher that sets only the cwd must also set `PWD`.
- **Worktrees**: `--worktree` makes opencode create a git worktree itself. Spartito must not use it. **[observed]**
- **Outside the worktree**: V2 allows access outside the project by default (`external_directory`). **[documented]** <https://opencode.ai/docs/migrate-v1/>
- **No sandbox**: opencode has no OS sandbox like Codex's.

### 2.8 Usage and cost

- **Per step**: every `step_finish` carries `cost` and `tokens`. **[unverified; seen in source]** Summed over a turn they give per-turn figures, not session totals.
- **Units**: the unit of `cost` and its value for subscription sign-ins (a ChatGPT plan, GitHub Copilot) are **[unverified]**.
- **Totals**: `opencode stats --cost --json` reports totals across sessions. **[observed]**

### 2.9 Login state

- **Connecting a provider**: credentials are entered with `/connect` in the TUI; some providers use environment variables or cloud credential chains. **[documented]** <https://opencode.ai/docs/providers/>
- **Credential storage**: credentials live in the database. **[unverified; seen in source]** <https://github.com/anomalyco/opencode/blob/dev/packages/core/src/credential.ts>
- **Listing logins**: `opencode auth list --format json` lists integrations. **[observed]** `auth export` prints secrets and must never be used. **[observed]**
- **Open for a login check**: whether `auth list` is offline and secret-free is **[unverified]**. It also misses providers configured by environment variable only.

### 2.10 Config and isolation

- **Config files**:
  - managed or system config, then global `~/.config/opencode/opencode.json(c)`, then project `opencode.json(c)` and `.opencode/` from the directory upwards. Managed values cannot be overridden. **[documented]** <https://opencode.ai/docs/config/>
  - JSON or JSONC, with `{env:…}` and `{file:…}` substitution. **[documented]**
- **Variables** (all **[unverified; seen in source]**, <https://github.com/anomalyco/opencode/blob/dev/packages/cli/src/server-process.ts>):
  - `OPENCODE_CONFIG` (a config file), `OPENCODE_CONFIG_CONTENT` (inline JSON), `OPENCODE_CONFIG_DIR`;
  - `OPENCODE_DISABLE_PROJECT_CONFIG`, `OPENCODE_DISABLE_CLAUDE_CODE*`;
  - `OPENCODE_DISABLE_MODELS_FETCH`, `OPENCODE_FLAGS`.
  - Where `OPENCODE_CONFIG` falls in the precedence of V2 is unknown.
- **XDG directories**: `XDG_CONFIG_HOME` moves `cli.json` **[documented]** <https://opencode.ai/docs/cli/config/>. With all four XDG directories set to a temporary directory, `opencode --version` created its `opencode` config, data and state directories there (with `log` and `repos`). **[observed]**
- **The background service**:
  - by default every CLI command uses **one shared background server per user**. `--standalone` runs a private server for that command. **[documented]** <https://opencode.ai/docs/cli/>
  - the server reads the `OPENCODE_DISABLE_*` variables at its start; for the service they must be persisted with `opencode service set env`. **[documented]** <https://opencode.ai/docs/instructions/>
  - **A turn's environment therefore reaches opencode's configuration only with `--standalone`.**
- **Counterparts of Claude's `--strict-mcp-config` and `--setting-sources`, and Codex's `--ignore-user-config`**: there is no single switch.
  - a policy `mcp.use` `deny *` plus `allow` per wanted server excludes other MCP servers.
  - `plugin.use` `deny *` excludes plugins.
  - `OPENCODE_DISABLE_PROJECT_CONFIG` drops project config.
  - the global config can only be avoided by moving `XDG_CONFIG_HOME`. That variable is inherited by the commands the agent runs, where `git`, `gh` and others read it too. Whether opencode can give its shells the original value is **[unverified]**.

### 2.11 Hooks, structured output, compaction, server mode

- **Plugins**:
  - server plugins are TypeScript or JavaScript, listed in `plugins`.
  - their hooks include tool `execute.before`/`execute.after`, permission `evaluate` (can set `deny`), shell `create.before` and session hooks.
  - there is no session-start hook. **[documented]** <https://opencode.ai/docs/build/plugins/server/>
  - the memory guard has no counterpart to port, and needs none (see 4.2).
- **Structured output**: `opencode run` has no JSON-schema option. **[observed]**
- **Compaction**:
  - automatic by default: `compaction.auto`, `keep.tokens` (15000), `buffer` (15%, at least 16k). **[documented]** <https://opencode.ai/docs/compaction/>
  - manual compaction is the API call `POST /api/session/<id>/compact`. It returns before the summary is written and takes no instructions. **[documented]** (same page)
  - the JSON output of `run` does not render compaction events. **[unverified; seen in source]**
- **Server mode**:
  - `opencode serve` runs the V2 API: sessions, prompts, events, permission replies, an OpenAPI document at `/docs/openapi.json`. **[documented]** <https://opencode.ai/docs/api>
  - the event stream is "volatile by contract". **[documented]** (same page)
  - the client library is TypeScript (`@opencode/client`). **[documented]** <https://opencode.ai/docs/build/client/>
  - `opencode acp` speaks the Agent Client Protocol over stdio. **[observed]**
- **Decided: one `opencode run --standalone --format json` process per turn**, like the other providers.
  - It keeps the runner's model: one process group per turn, killable, a stdout stream, no daemon, isolation through the environment.
  - A long-lived `serve` would need daemon management, an HTTP client in Python, a password, per-location config and handling of missed events.
  - ACP is interesting as a later, generic provider for several harnesses, but it needs a long-lived process per session.
  - ACP is on the roadmap: [ROADMAP.md](../ROADMAP.md).

### 2.12 Licence and stability

- **Licence**: MIT. **[documented]** <https://github.com/anomalyco/opencode>
- **Cadence**: several releases a week; 2.3.1 and 2.3.2 came out on the same day. **[observed on the releases page]**
- **Stability**:
  - V2's `run` flags and JSON events are not documented. The server API makes "intentional breaking changes" across V1 and V2. **[documented]** <https://opencode.ai/docs/migrate-v1/>
  - Expect breakage between minor versions. Pin a tested minimum version in the adapter and parse defensively.

---

## 3. Target design

### 3.1 Alternatives weighed

1. **A declarative provider spec** (a dataclass of flag names and strategies, with one generic `invocation()`).
   - The three providers already differ in mechanism:
     - Claude: flags, a stream-json prompt with a hooks canary, a private MCP file.
     - Codex: `-c` TOML overrides that must follow `resume <id>`.
     - opencode: a config file in an environment variable and `$PWD`.
   - The spec would grow fields that are really code hooks, and argv order would be dictated by the builder. **Rejected.**
2. **A template-method base class** (`invocation()` assembled from overridable steps).
   - It saves little: each step is two lines, and the order of steps differs per CLI.
   - It hides the real differences behind hook names. **Rejected for `invocation()`**, accepted for self-description.
3. **Explicit adapters on a shared kit** (recommended).
   - Each provider writes its own `invocation()` and its own event mapping: the parts that differ by nature, in plain code.
   - Everything that is the same is a small, tested function or class in one module.
   - Facts about a provider that other modules need (credit, cache effect, config-home variables) become attributes of the provider instead of tables keyed by name.
   - The runner gets one new, optional, generic hook: a per-turn stream object.

### 3.2 The structure (planned)

```
providers/
  base.py        Provider protocol (unchanged required members) + new optional members
  kit.py         (planned) shared building blocks, no provider knowledge
  claude.py      ClaudeProvider: explicit invocation and parser, on the kit
  codex.py       CodexProvider: same
  opencode.py    (planned) OpencodeProvider
  locate.py      unchanged; its _run becomes kit.run_cli
```

**`kit.py` (planned)**:

| block | replaces | what |
|---|---|---|
| `run_cli(argv, timeout, stdin=None, env=None) -> (code, out, err)` | D3 | one way to run a CLI briefly: extra install directories on `PATH`, own process group, killed with its group on timeout |
| `ModelCatalog(lookup, retry_when)` | D2 | cached per process, behind a lock; `retry_when(result)` decides when a result is asked for again (Claude: a fallback or generic list; Codex: an empty one); exposes `models()` and `note` |
| `JsonLines` | D1 | the default per-turn stream: decodes a line, turns non-JSON into `info`, hands a dict to `on_json(data)`; `finish(exit_code) -> list[Event]` defaults to `[]` |
| `own_server_entry(mcp: McpServer) -> dict` | D4 | the `orch` server as a canonical entry (`command`, `args`, `env`), the shape of `mcp.Server.entry()`; Claude adds `alwaysLoad`; Codex and opencode translate |
| `turn_servers(ctx) -> dict[str, dict]` | 362–397, 123–130 | the `orch` server (if any) and the wirings' servers, minus the reserved name, as canonical entries |
| `schema_file(ctx) -> Path` | D8 | writes `ctx.output_schema` to the scratch directory |
| `with_role_prefix(ctx) -> str` | D9 | the first-turn strategy (role prompt, separator, prompt) |
| `schema_in_prompt(prompt, schema) -> str`, `extract_json(text)` | new | for CLIs without a schema option: the schema appended to the prompt, the answer's JSON taken out of a code fence |

**Optional `Provider` members (planned; all read with `getattr` and a default, so plugins keep working)**:

| member | default | replaces |
|---|---|---|
| `stream() -> TurnStream` (`feed(line) -> list[Event]`, `finish(exit_code) -> list[Event]`) | wraps `parse(line)`, `finish` returns `[]` | a parser that needs per-turn state or must report at the end of the stream (opencode) |
| `credit: Credit(link, trailer_rule)` | none: no attribution section | `roles._CREDITS` |
| `cache_effect: dict` | "may restart" for both | `core/setup.CACHE_EFFECT` |
| `config_env: tuple[str, ...]` | `()` | `runner.MCP_PASSED_ENV` and `autostart.ENV_NAMES` entries |
| `reports_context: bool` | `None` | `availability`'s `builtin` check |
| `carries_mcp_servers: bool` | `False` | the literal `"claude"` in `capabilities.mcp_capability` |
| `min_version: tuple[int, ...]` | none | new: an older CLI is reported as not usable, with the reason |
| `Invocation.notes: list[str]` | `[]` | new: info lines the runner logs, e.g. "effort needs a model on opencode" |

`chrome` stays declared in `capabilities.py` with `providers.claude.CHROME`: it is Claude's by nature.

**The runner change (planned)**: the event dispatch (334–359) becomes `handle(event)`. Lines go through `stream.feed(line)`, and at the end of the stream `stream.finish(code)` is handled the same way. Nothing else in the runner learns about providers.

**Attribution (planned)**:
- `roles.attribution(provider_name)` looks up `providers.get(name).credit`.
- The texts stay word for word.
- Codex's rule ("when you add a trailer, keep its email…") becomes a named constant that opencode reuses.

**Loadout**: no key changes in the refactor. No provider-neutral "user configuration" key for now (Q3, decided).

**Plugins (`ORCH_PROVIDERS`)**:
- The four required members (`name`, `efforts`, `models()`, `invocation()`, `parse()`, `attach_command()`) and every optional attribute keep their meaning.
- A plugin that knows nothing of the kit runs as before.
- `CliProvider`-style helpers are opt-in.
- A new test runs a turn with a minimal plugin that has only the required members.

### 3.3 What a new provider implements

One module, about 200–300 lines, and one dialect for the fake-CLI harness.

```python
"""<Harness> in headless mode: `<cli> … --json`."""   # (planned skeleton)

from ..models import Agent, Event, Usage
from . import kit
from .base import Invocation, RunContext, wired

class NewProvider:
    name = "new"
    label = "New Harness"
    description = "…, run as new run --json"
    executable = "new"
    version_args = ("--version",)
    min_version = (1, 0)
    efforts = ("low", "high")
    reports_cost = True
    role_prompt = "every_turn"
    reports_context = True
    carries_mcp_servers = True
    credit = kit.Credit("[New Harness](https://example.com)", kit.TRAILER_IF_ADDED)
    cache_effect = {"model": "restarts", "effort": "may restart"}
    config_env = ("NEW_HOME",)
    limits = ("…",)

    catalog = kit.ModelCatalog(lambda: parse_models(kit.run_cli(["new", "models"], 15)[1]),
                               retry_when=lambda models: not models)

    def models(self):
        return self.catalog.models()

    def invocation(self, ctx: RunContext) -> Invocation:
        argv = ["new", "run", "--json"]
        argv += ["--resume", ctx.agent.session_id] if ctx.agent.session_id else []
        argv += ["--model", ctx.model] if ctx.model else []
        servers = kit.turn_servers(ctx)            # canonical entries, translated here
        extra_argv, extra_env = wired(ctx.wirings)
        return Invocation(argv=argv + extra_argv, stdin=ctx.prompt, env=extra_env)

    def stream(self):
        return Stream()

    def parse(self, line: str) -> list[Event]:     # kept for the protocol
        return Stream().feed(line)

    def attach_command(self, agent: Agent) -> list[str]:
        return ["new", "--resume", agent.session_id or ""]

class Stream(kit.JsonLines):
    def on_json(self, data: dict) -> list[Event]:
        ...                                        # map the CLI's events to Spartito's
```

Plus:
- a fake-CLI dialect (`tests/fakecli/dialects/new.py`, planned);
- registration in `BUILTIN` (appended, so "the first available" does not change for anyone);
- its row in [providers.md](providers.md#other-providers).

### 3.4 One fake-CLI harness (planned)

`tests/fakecli/` generalises the script of `FakeClaude`:
- **One engine** reads the scenario words of the prompt (`SLEEP`, `FAIL`, `TOKENS`, `CONTEXT`, `TWICE`, `LINGER`, …).
- **It records** argv, stdin, the chosen environment variables and private files to the run's scratch directory.
- **Each dialect module** turns abstract events (session, text, tool, usage, compaction, result, error) into the CLI's own lines.

`conftest.PROVIDER_CLIS` comes from the registry, so every registered provider has a fake first on `PATH`. `FakeProvider` (the stand-in used by most runner tests) stays as it is.

### 3.5 Migration with no behaviour change

Each step ships alone. The refactor relies on the existing tests that already pin argv, env and parsed events (`tests/test_providers.py`, `tests/test_mcp_wiring.py`, `tests/test_tool_search.py`, `tests/test_runner_memory.py`, `tests/test_context.py`, `tests/test_costs.py`, `tests/test_usage_totals.py` and the others below) and on the normal review.

**What the existing tests pin**:
- `test_providers.py`: argv on fresh and resumed turns, loadout flags, `--ignore-user-config`, memory, parsing, usage.
- `test_tool_search.py`, `test_mcp_wiring.py`: `ToolSearch`, `alwaysLoad`, the private file.
- `test_runner_memory.py`: the canary.
- `test_context.py`: context, compaction, the rollout file.
- `test_costs.py`, `test_usage_totals.py`: cumulative usage.
- `test_attribution.py`, `test_availability.py`, `test_dashboard_providers.py`, `test_docs_providers.py`, `test_architecture.py`.

**Steps**:
1. Kit and porting `claude.py`/`codex.py` onto it (phase 1).
2. Provider metadata instead of tables outside `providers/` (phase 2).
3. The stream hook in the runner (phase 3).
4. The fake-CLI harness (phase 4).

---

## 4. The opencode adapter (planned)

### 4.1 The command line of a turn

```
opencode run --standalone --format json --session <ses_id> [--archived --title <agent-id>]
             [--auto] [--model <provider>/<model>[#<effort>]]
  stdin: the prompt (plus the answer schema, when the run has one)
  env:   OPENCODE_CONFIG=<private file>, PWD=<agent cwd>, the wirings' env
```

- **`--standalone`**: the turn's environment and config apply only to its own server, and the user's shared service is neither used nor started.
- **New session**: `<ses_id>` is chosen by Spartito (`ses_` and a random hex suffix; live check V2). `--archived` and `--title` are given only then.
- **The turn's config file** is written as an `Invocation.private_files` entry, at the path the runner already gives as `ctx.mcp_config`, and named by `OPENCODE_CONFIG`. It holds:
  - `instructions`: the absolute path of a per-turn file in the scratch directory holding the role prompt and attribution. **Role prompt: every turn**, after the project's `AGENTS.md`.
  - `permissions` and `policies`, from the loadout (4.2).
  - `mcp.servers`: the `orch` MCP server for leads, and the `mcp:` servers (4.3).
  - `update: "disable"`, so a turn never updates the CLI.

### 4.2 Loadout keys

| key | opencode |
|---|---|
| `tools` | Claude tool names map to actions: `Bash`→`shell`; `Read`→`read`; `Edit`/`Write`/`MultiEdit`/`NotebookEdit`→`edit`; `Glob`→`glob`; `Grep`→`grep`; `WebFetch`→`webfetch`; `WebSearch`→`websearch`; `Task`/`Agent`→`subagent`; `Skill`→`skill`. A built-in action that no listed tool covers gets a `deny` rule and a `tool.use` deny policy. `default` denies nothing extra; `none` denies every built-in action. `ToolSearch` and unknown names are ignored, with a note |
| always | `question` denied (nobody answers). The `opencode` namespace tools that create, move or remove worktrees and sessions are denied (Spartito owns both; action names: live check V6). `external_directory` denied except the turn's `writable_dirs` (the shared `.git`) |
| `mcp` | `strict`: policies `mcp.use` `deny *`, then `allow` for the `orch` MCP server and each turn server. `inherit`: no policy |
| `setting_sources` | without `project`: `OPENCODE_DISABLE_PROJECT_CONFIG=1`. This also drops project `AGENTS.md`, unlike Claude, where `project` keeps `CLAUDE.md`. Without `user`: policies `plugin.use` `deny *`. Moving the global config away (`XDG_CONFIG_HOME`) only if live check V15 shows that the agent's commands can keep the user's own value. Otherwise the user's global `opencode.json` still applies (a listed limit) |
| `slash_commands` | `false`: `skill` denied and `skill.use` `deny *` |
| `memory` | no effect (opencode has no memory directory). A listed limit |
| `tool_search` | no effect. Code Mode, opencode's default for MCP servers, is its own way (V9) |
| `codex_user_config` | no effect |

### 4.3 Capabilities and the `orch` MCP server

- **`chrome`**: not supported (Claude only). An agent on opencode skips it with the existing note.
- **`mcp:<name>`**: supported (`carries_mcp_servers = True`). The canonical entry is translated:
  - `command` + `args` → `command: [command, *args]`; `env` → `environment`; `url`/`headers` → `type: "remote"`;
  - `${NAME}` → `{env:NAME}`; `${NAME:-default}` → `{env:NAME}` when the turn's environment has it set, else the default written literally.
  - The pre-turn check of unset variables stays as it is.
  - Server statuses: the JSON output has none, so `mcp` statuses stay "not reported".
  - MCP servers on opencode follow the same marked-server rule as Codex (Q8, decided).
- **The `orch` MCP server** (leads):
  - a local server with the runner's `McpServer`, `timeout.execution` well above the longest wait, allowed in policies and permissions.
  - its tools are named `orch_<tool>`. The role prompts name no tool by its full Claude name, so they need no change.

### 4.4 The rest

| topic | opencode |
|---|---|
| permission modes | `default` (the default): no `--auto`, so every `ask` is rejected and the turn goes on. `auto`: `--auto`. Other values are refused when set |
| model and effort | `--model <model>#<effort>`. Effort without a model: ignored, with `Invocation.notes`. A model that already contains `#` keeps its own variant. `efforts = ("low", "medium", "high", "max")` as offered names; a model may reject one (the turn fails with opencode's error) |
| model list | `opencode models` through `kit.run_cli`, with `OPENCODE_DISABLE_MODELS_FETCH=1`, once V10 shows it is offline; until then an empty list with a note |
| sessions | Spartito's own `ses_` id; resume with the same `--session`. A lost session restarts empty instead of failing: a listed limit |
| compaction | no `compact_prompt` in the first version, so the provider cannot compact. opencode compacts on its own; this is not reported (V13) |
| context size | from the last `step_finish` of the turn: `input + cache.read + cache.write` (V8). No window, so `reports_context = True` with no `windows` |
| structured answers | `kit.schema_in_prompt`. The stream's `finish()` sets `structured` from `kit.extract_json` of the last text |
| cost and usage | summed over the turn's `step_finish` parts: per turn, `cumulative=False`. `reports_cost = True`, but a subscription sign-in may report `0` (V8) |
| stream | text parts accumulate per part id and are emitted when a step ends; `tool_use` → `tool` (error state → `tool_result`); `retry` and `background_wait` → `info`; `error` → `error` and a failed result; `finish(code)` → `result` with the last message's text and the turn's usage |
| lead turns | the turn ends when the process exits. opencode waits for background jobs and sub-agents first (V12) |
| attribution | the Codex rule ("when you add a Co-Authored-By trailer…"), credited as `[OpenCode](https://opencode.ai)` |
| availability | `opencode --version`, with `min_version = (2, 2, 3)` (policies). An older version is not usable: "opencode 1.x is not supported". `login_args = None` until V11 |
| attach | `opencode --session <id>`, the interactive UI on the user's shared service |
| `config_env` | `XDG_CONFIG_HOME`, `XDG_DATA_HOME`, `OPENCODE_DB` |

### 4.5 Gaps, and what the user notices

- **No Claude in Chrome.** The capability is skipped with a note.
- **No `memory`.**
- **The answer schema is asked for, not enforced.** An answer that is not JSON leaves `structured` empty, and a lead gets the text.
- **The Compact button and `spartito compact` refuse an opencode agent.** opencode's own compaction runs unseen, and the context gauge may drop without a log line.
- **No context window in the gauge.**
- **Costs may show `$0.00` for subscription sign-ins.**
- **No OS sandbox.** The agent's shell can write outside its worktree. Only opencode's own tools are held back.
- **Possibly the user's global `opencode.json`** (model, providers, instructions) applies to agents (4.2).
- **Sessions are stored in the user's opencode database**, archived, and visible in opencode's own UI.
- **A lost session restarts empty** instead of failing.
- **`spartito chat` stays Claude only.**

---

## 5. Phases, risks, verification, decided questions

Sizes are per worker, tests and docs included: **S** about half a day, **M** about a day, **L** about two days. At most three workers at a time, split by the files they own. There are no schema changes.

| # | phase | owns | depends on | size |
|---|---|---|---|---|
| 1 | Kit; `claude.py`/`codex.py` ported; provider attributes added (data only) | `providers/*`, `providers/locate.py` | the in-flight provider work merged (done) | M |
| 2 | Consumers read the attributes: `roles.py`, `core/setup.py`, `autostart.py`, `core/availability.py` (`min_version` too), `capabilities.py`, `mcp_server.py` docstrings, `cli.py` (help texts; `chat` uses `kit.own_server_entry`), `web/static/*` (a neutral avatar colour) | those files | 1 | M |
| 3 | The stream hook and `Invocation.notes` in the runner; `MCP_PASSED_ENV` from `config_env` | `runner.py`, `providers/base.py` | 1 | S |
| 4 | Fake-CLI harness; Claude and Codex dialects; `PROVIDER_CLIS` from the registry | `tests/fakecli/`, `tests/conftest.py`, `tests/fakeprovider.py` | 1 | M |
| 5a | Live checks V1–V19 with the owner | notes for this document | the owner's go-ahead; can run during 1–4 | S |
| 5b | `providers/opencode.py`: invocation, turn config, loadout mapping, MCP translation; registered last in `BUILTIN`; a fake `opencode` on `PATH` in the same change | `providers/opencode.py`, `providers/__init__.py`, `tests/test_opencode.py` | 2, 3, 4, 5a | M |
| 5c | opencode stream: usage, context, structured answers, the dialect | same owner as 5b | 5b | M |
| 5d | Model list, availability, docs (`providers.md`, `configuration.md`, `capabilities.md`, `mcp.md`, `architecture.md`), `CHANGELOG.md` | docs, small code | 5b | M |

- **Total**: about 7.5 worker-days.
- **Critical path**: 1 → 2 (or 4) → 5b → 5c (5d alongside), about 4 days with three workers.
- **In parallel**: 2, 3 and 4 run alongside each other after 1.

**Risks**:
- **opencode's surface changes**: undocumented `run` flags and JSON events, weekly releases. Mitigations: `min_version`, defensive parsing, a recorded fixture per tested version, and a note of the tested version in `providers.md`.
- **Config precedence**: `OPENCODE_CONFIG`'s place in the precedence is unknown. A repository's `opencode.json` could widen permissions; policies are the safeguard (V5).
- **Isolation**: moving `XDG_CONFIG_HOME` leaks into the agent's commands (V15).
- **Shared database**: concurrent standalone processes share one SQLite database (V14), as does the user's own opencode.
- **Silent new session**: a missing `PWD` sends the turn to the wrong directory; a lost session id silently starts a new one.
- **Refactor drift**: mitigated by the existing tests, the normal review and phase-by-phase pull requests.

**Live verification (each needs the owner's go-ahead).**
- Every check uses temporary `XDG_DATA_HOME`/`XDG_CONFIG_HOME`/`XDG_STATE_HOME`/`XDG_CACHE_HOME` and a throwaway git repository.
- For model turns there are two options:
  - **a local model through Ollama**: no account, no cost; checks the mechanics only;
  - **an API key given as an environment variable** for one provider the owner already uses: checks usage and cost.
- No login of the owner's real opencode data is touched.

| # | check | how | model turn | account |
|---|---|---|---|---|
| V1 | event shapes, exit codes, the end of a turn | `run --standalone --format json` with a short prompt, then one that fails | yes | yes |
| V2 | `--session ses_<hex>` creates, then resumes, with context | two turns | yes | yes |
| V3 | prompt from stdin without a positional message | one turn | yes | yes |
| V4 | `instructions` with an absolute path is appended | `opencode debug config` in the test repository | no | no |
| V5 | `OPENCODE_CONFIG` against project config: permissions and policies | `debug config`/`debug agents`, then one turn | partly | partly |
| V6 | action names of the `opencode` namespace tools; `tools` deny rules hold | `debug agents`, one turn asking for a denied tool | yes | yes |
| V7 | `PWD` decides the directory | one turn with cwd ≠ `PWD` | yes | yes |
| V8 | token fields (cache inside `input`?), cost unit, cost under a subscription | turns; compare with `opencode stats --cost --json` | yes | yes |
| V9 | Code Mode and the context cost of a stdio MCP server | a local fake MCP server; one turn | yes | yes |
| V10 | `opencode models` offline, its format, variants | the command with fetching off | no | yes (to list models) |
| V11 | `auth list --format json` offline and secret-free | the command on the temporary data directory | no | no |
| V12 | start-up time per turn; a lead turn with the `orch` MCP server, long waits, background jobs | timed turns | yes | yes |
| V13 | compaction under `run`: any visible event; a prompt-level compact | a long session | yes | yes |
| V14 | parallel standalone turns on one database | three turns at once | yes | yes |
| V15 | whether `XDG_CONFIG_HOME` reaches the agent's shell | a turn that prints its environment | yes | yes |
| V16 | SIGTERM ends the process group within the runner's grace time | stop a running turn | yes | yes |
| V17 | `text` parts as suffixes | one turn | yes | yes |
| V18 | `--archived` keeps sessions out of `session list` | after V2 | no | no |
| V19 | what `--version` writes (directories, logs) | temporary directories | no | no |

**Installing opencode for these checks**: not needed. It is already installed (2.3.2). Upgrading would need the owner's go-ahead, because releases are frequent and the checks should run on one pinned version.

**Decided questions**:

Any of them can still change during implementation, but only with the owner.

Decided: no golden tests for now; the existing tests and the normal review carry the refactor.

1. **Q1 Live checks**: run V1–V19 with the installed 2.3.2, temporary state, and a local model (plus one API-key provider for V8)? Decided: run live checks V1–V19 on the installed 2.3.2 with temporary state (each run still needs the owner's go-ahead).
2. **Q2 Versions**: support only opencode 2.2.3 or later, and refuse 1.x? Decided: support only opencode 2.2.3 or later; refuse 1.x.
3. **Q3 A provider-neutral "user configuration" loadout key** in place of `codex_user_config`? Decided: no provider-neutral user-config key for now; map opencode onto `setting_sources`, keep `codex_user_config`.
4. **Q4 Default permission mode** for opencode: no `--auto` (asks rejected), or `--auto`? Decided: no `--auto`.
5. **Q5 Structured answers** asked for in the prompt, not enforced: acceptable for planner, reviewer and tester roles? Decided: structured answers are asked for in the prompt and listed as a limit.
6. **Q6 Server model**: one process per turn now, `serve` or ACP later? Decided: one process per turn; ACP is on the roadmap ([ROADMAP.md](../ROADMAP.md)).
7. **Q7 Order of work**: refactor (1–4) before opencode, or opencode first on today's structure? Decided: refactor first (phases 1–4), then opencode.
8. **Q8 MCP servers on opencode** without an approval gate (as decided for Codex in [mcp.md](mcp.md#decisions)): allow them only when the owner marks a server for it, or in general? Decided: MCP servers on opencode follow the same marked-server rule as Codex.
9. **Q9 Global opencode config**: if V15 shows the leak, accept that the user's global `opencode.json` applies to agents (listed), or deny opencode agents some of it through policies only? Decided: accept that the global `opencode.json` applies, and list it as a limit.

See also [mcp.md](mcp.md#wiring-per-provider) for how MCP servers are wired per provider,
[configuration.md](configuration.md#model-and-effort) for model and effort, and
[architecture.md](architecture.md) for the layers.
