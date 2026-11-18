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
> - **[observed]**: seen in the installed CLI: in its help output, or in the live checks
>   V1–V19 (the check is named, e.g. "V5").
>
> opencode was installed on the research machine: version **2.3.2**. The research first read
> only `--version` and `--help` output, with opencode's config, data, cache and state
> directories pointed at an empty temporary directory. The live checks V1–V19 then ran turns on
> the same version in isolated temporary state (§5, "Live verification").

## Contents

1. Current state of the provider layer
2. opencode: what it offers a headless orchestrator
3. Target design
4. The opencode adapter
5. Phases, risks, verification, decided questions
6. Google Antigravity and Gemini CLI (research, not built)

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
- `--help` output of 2.3.2;
- the live checks V1–V19 on 2.3.2 (§5).

**Two lines.** opencode 2.x ("V2") is not compatible with 1.x:
- V2 changes config keys, the permission model and the server API. **[documented]** <https://opencode.ai/docs/migrate-v1/>
- V2 installs as the Homebrew formula `opencode-v2` and through `/v2/install`; mise before 2026.10.9 installs 1.x. **[documented]** <https://opencode.ai/docs/>
- The repository's release page still marks a 1.19.x release "Latest" next to 2.3.x releases. **[observed on the releases page]**

**This design targets 2.x only.**

### 2.1 Non-interactive mode

- `opencode run [flags] [<message...>]` takes these flags: `--continue/-c`, `--session/-s <id>`, `--fork`, `--worktree/-w`, `--model/-m provider/model#variant`, `--agent`, `--format default|json`, `--file/-f`, `--title`, `--archived`, `--thinking` and `--auto`. The global flags `--standalone`, `--server <url>`, `--log-level`, `--print-logs` apply too. **[observed]**
- The docs show only `opencode run "<prompt>"`. **[documented]** <https://opencode.ai/docs/cli/>
- **The prompt**: positional words and, when stdin is not a TTY, stdin. With no positional message the prompt comes from stdin; with both, the stored text is the argument, a newline, then stdin. Empty input fails with exit 1 ("You must provide a message"). **[observed, V3]** <https://github.com/anomalyco/opencode/blob/dev/packages/cli/src/run/run.ts>
- **JSON output**: one object per line. **[observed, V1]** <https://github.com/anomalyco/opencode/blob/dev/packages/cli/src/run/noninteractive.ts>
  - Every event carries `type`, `timestamp` and `sessionID`. Before a session exists, `sessionID` is `""`.
  - `step_start`: `part` with `id`, `messageID`, `sessionID`, `snapshot`, `type`. It arrives at the model's first token, so the first event of a turn marks the time to first token. **[observed, V12]**
  - `step_finish`: the same fields plus `reason` (`tool-calls` or `stop`), `cost`, `tokens`.
  - `text`: `part` with `id`, `messageID`, `sessionID`, `text`, `time`, `type`. **Corrected by V17:** `part.text` is the part's **whole text**, emitted **once, when the part ends** (`part.time.start/end`); there is no streaming of partial text. **[observed, V17]**
  - `reasoning` (only with `--thinking`): not seen; `--thinking` was not tried. **[unverified; seen in source]**
  - `tool_use`: `part` with `partID` (the part id), `id` (the model provider's `call_…` id), `tool`, `messageID`, `sessionID`, `type`, and `state` (`status`, `input`, `output` or `error`, `title`, `metadata`, `time`). Tools appear only on completion, with no running state; a 20 s MCP call gave no event at all until it completed. **[observed, V1, V12]**
  - Errors inside Code Mode arrive as `status: "completed"`, with the error text in `output`. **[observed, V1]**
  - `retry` (`attempt`, `nextRetryAt`, `error`): never occurred in the checks. **[unverified; seen in source]**
  - `background_wait`: `{jobs: [{id, kind: "shell", title}]}`. **[observed, V12]**
  - `error`: **nested**, not `{type, message}`: a final line `{"type": "error", "timestamp", "sessionID", "error": {"type", "message"[, "status", "response"]}}`. Error types seen: `provider.no-route` (unknown model or variant), `provider.auth` (401), `unknown` (bad session id, empty prompt). **[observed, V1]**
- **Tokens**: `{input, output, reasoning, cache: {read, write}}`. `input` **excludes both cache reads and cache writes**. **[observed, V8]** <https://github.com/anomalyco/opencode/blob/dev/packages/schema/src/token-usage.ts>
- **How a turn ends**: the process waits until the session's execution has succeeded, failed or been interrupted, then for the prompt's background jobs and sub-sessions, and exits. **[unverified; seen in source]** A background shell job was observed: the process waited, and when the job ended the model was woken for one more billed step before the process exited 0. **[observed, V12]**
- **Exit codes**: 0 on success; 1 with the final nested `error` line on failure. **[observed, V1]**
- **SIGTERM** (corrected by V16; the earlier reading of the source said exit 1 with an `error` line): **[observed, V16]**
  - SIGTERM to the CLI's process group emptied the group in 0.07 s, with exit status **143**.
  - The stream ended after `step_start`, with **no `error` and no `step_finish` line**. The session was stored with outcome `interrupted`.
  - The process tree is CLI → `opencode serve --stdio --port 0` → a pty helper under `$XDG_CACHE_HOME/opencode/bin` → the shell → its command. The server, the pty helper and the shell each have **their own process group**. All were gone within about 1 s (not timed precisely).
  - SIGKILL was not tested.

### 2.2 Sessions

- **Ids**:
  - session ids start with `ses`. Native ones are `ses_` followed by 12 hex digits and 14 alphanumerics. An id without `ses` fails with exit 1. **[observed, V2]** <https://github.com/anomalyco/opencode/blob/dev/packages/schema/src/session-id.ts>
  - `--session <id>` resumes an existing session. A missing one is **created with that id**, unless `--fork` is given: `--session ses_<26 hex>` created the session, and a second turn resumed it with its context. **[observed, V2]** <https://github.com/anomalyco/opencode/blob/dev/packages/cli/src/session-target.ts>
  - So Spartito can choose the id up front, as it does with Claude's `--session-id`. A resumed id whose session was deleted silently starts an empty conversation.
  - A session's `projectId` is the repository's root commit, and its `directory` is the realpath. **[observed, V2]**
- **`--continue` and `--fork`**: `--continue` picks the newest top-level session of the exact directory. `--fork` copies a session first. **[unverified; seen in source]**
- **Storage**:
  - a SQLite database, normally `~/.local/share/opencode/opencode.db`. **[documented]** <https://opencode.ai/docs/troubleshooting/>
  - `OPENCODE_DB` moves it. **[documented]** <https://opencode.ai/docs/cli/>
  - three parallel `--standalone` turns on one `opencode.db` (WAL mode) all exited 0 with correct answers and no lock errors, and all three sessions were stored. **[observed, V14]**
- **Session commands**: `opencode session list|delete|export|import`. **[observed]** `session list` is scoped by `PWD`. **[observed, V7]**
- **Archived sessions** (corrected by V18): `--archived` sets the session's `time.archived`, but `session list` (as text and with `--format json`) **still lists it**. **[observed, V18]**

### 2.3 Models and effort

- **Model ids**:
  - `provider/model` with an optional `#variant`. The provider ends at the first `/`; ids are case-sensitive catalogue ids. **[documented]** <https://opencode.ai/docs/models/>
  - The default is `model` in the config if enabled and available, otherwise "the newest available supported model". **[documented]** (same page)
- **Variants**:
  - variants are named per model ("often for reasoning effort"). `low`, `high` and `max` are examples, and not every model has them.
  - an unknown variant is a model-resolution error.
  - there is no `--variant` flag: the variant goes into `--model …#variant`. **[documented]** (same page)
  - a variant without a model fails ("Cannot select a variant before selecting a model"). **[unverified; seen in source]**
- **Listing models**: `opencode models` exists, with no options in 2.3.2. **[observed]** What it prints **[observed, V10]**:
  - the output is plain `provider/model` lines: no variants, no JSON;
  - it lists only providers with credentials, plus 12 free `opencode/*` models; without a key, only the free ones;
  - `models --standalone` with `OPENCODE_DISABLE_MODELS_FETCH=1` printed the same 371 lines as without it, in about 0.45 s. It was not tested with the network actually cut.

### 2.4 System prompt and instructions

- **Agents**:
  - agents are defined under `agents.<id>` in config or in markdown files. **[documented]** <https://opencode.ai/docs/agents/>
  - an agent's `system` **replaces** the provider's base prompt; project instructions are still added. **[documented]** (same page)
  - built-in agents: `build` (default), `plan`, `general`, `explore`. **[documented]**
- **Instruction files**:
  - `AGENTS.md` from the working directory up to home or the project root;
  - `CLAUDE.md` where a directory has no `AGENTS.md`;
  - a global `~/.config/opencode/AGENTS.md`, or `~/.claude/CLAUDE.md` in its place. **[documented]** <https://opencode.ai/docs/instructions/>
- **`instructions`**: files, globs (absolute paths allowed) or URLs, loaded **after** the `AGENTS.md` files. Entries from all config documents are combined. **[documented]** (same page) `opencode debug config` accepts an absolute `instructions` path from the turn's config file; whether the file then reaches the prompt was not tested. **[observed, V4]**
- **Switches**: `OPENCODE_DISABLE_CLAUDE_CODE_PROMPT`, `OPENCODE_DISABLE_CLAUDE_CODE` and `OPENCODE_DISABLE_PROJECT_CONFIG`. The last one skips project `AGENTS.md` discovery. **[documented]** (same page)
- **No append flag**: there is no flag or variable that appends to the system prompt per run. **[documented by absence]** An `instructions` entry pointing at a per-turn file is the equivalent.

### 2.5 Tools, permissions, approvals

- **Built-in tools**: `read`, `glob`, `grep`, `edit`, `write`, `patch`, `shell`, `webfetch`, `websearch`, `question`, `skill`, `subagent`, `execute` (Code Mode), the browser namespace, and `opencode` namespace tools (`session_move`, `worktree_create`, …). **[documented]** <https://opencode.ai/docs/tools/>
- **The `opencode` namespace** **[observed, V6]**:
  - Code Mode exposes `tools.opencode.*`: `session_move`, `session_rename`, `worktree_create`, `worktree_list`, `worktree_remove`, `list_mcp_resources`, `read_mcp_resource`, `models`. Part of this list comes from the model reading the `execute` description.
  - Their permission actions are `opencode_<function>`: denying `opencode_worktree_list` and `opencode_session_rename` removed them ("Unknown tool 'opencode.worktree_list'"). `opencode_worktree_create` and `opencode_session_move` were not tested directly.
  - The docs say the worktree tools other than `worktree_remove` request no permission, yet the catalogue filter works by these names.
- **A deny removes a tool from the catalogue**: denying `shell` and `question` cut the first step's input from 5210 to 4696 tokens, and the model had no `shell` left. **[observed, V6]**
- **The `build` agent** allows `question` by default and denies `browser`. **[observed, V6]**
- **Permission rules**:
  - `permissions` is an ordered list of `{action, resource, effect}` with `allow`, `ask` or `deny`. The last matching rule wins.
  - the order is lower-priority config, then global rules, then agent rules.
  - across config documents, a later document's rules come after an earlier one's (order: 2.10). So **project `permissions` win over the `OPENCODE_CONFIG` file's**: `debug agents` showed the turn file's `grep deny` followed by the project's `grep allow`, and in the turn grep ran. `OPENCODE_CONFIG_CONTENT`'s permissions come last and win. **[observed, V5]**
  - `edit` covers `edit`, `write` and `patch`; MCP tools are `<server>_<tool>`.
  - the base policy allows `*`, asks for `*.env` reads, and allows `external_directory`. **[documented]** <https://opencode.ai/docs/permissions/>
- **Policies**: `policies` hold hard `allow`/`deny` statements for `tool.use`, `mcp.use`, `plugin.use`, `skill.use`, `provider.use`, …. Broader config wins, so a repository cannot lift a global deny. `mcp.use`, `plugin.use` and `skill.use` need 2.2.3 or later. **[documented]** <https://opencode.ai/docs/policies/> The `OPENCODE_CONFIG` file's policies held against the project: its `tool.use` `glob:*` `deny` blocked glob ("Blocked by configuration policy") despite a project `allow`. **[observed, V5]**
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
- **Deferred schemas**: none documented. **Code Mode** (default) groups a server's tools behind `execute`, and it does keep their schemas out of the request: with a fake server of 43 tools (91 KB of schemas) the first step's input was 5,886 tokens in Code Mode and 20,571 with `codemode: false`. **[observed, V12]**
- **MCP calls in the stream** **[observed, V9]**:
  - in Code Mode an MCP call appears as the tool **`execute`**, with input such as `{"code": "return tools.fake.add({a: 2, b: 3});"}` and output `{"result": 5}`. The server's name can be read only from `tools.<server>.<tool>` in the code.
  - with `codemode: false` the calls appear as `<server>_<tool>` (`fake_add`, `fake_echo`), with raw output.
- **A server that fails to start is silent** in the JSON stream: only a WARN `mcp connect failed` on stderr with `--print-logs`, and the turn goes on without the server. **[observed, V9]**
- **Commands**: `opencode mcp list|add|auth|logout`. **[observed]** `mcp list --standalone` says "No MCP servers configured" even when `OPENCODE_CONFIG` or `OPENCODE_CONFIG_CONTENT` defines a server. **[observed, V9]**

### 2.7 Working directory and worktrees

- **Directory**: `opencode run` takes its directory from **`$PWD`**, falling back to the process's cwd: with the cwd in another directory and `PWD` in the repository, glob returned the repository's files. **[observed, V7]** A launcher that sets only the cwd must also set `PWD`.
- **Worktrees**: `--worktree` makes opencode create a git worktree itself. Spartito must not use it. **[observed]**
- **Outside the worktree**: V2 allows access outside the project by default (`external_directory`). **[documented]** <https://opencode.ai/docs/migrate-v1/>
- **No sandbox**: opencode has no OS sandbox like Codex's.

### 2.8 Usage and cost

- **Per step**: every `step_finish` carries `cost` and `tokens`. **[observed, V1, V8]** Summed over a turn they give per-turn figures, not session totals.
- **Units**: `cost` is in **USD**; it matched the catalogue prices exactly by hand (e.g. 0.00861675 for 3 input, 4 output and 6875 cache-write tokens). **[observed, V8]** Its value for subscription sign-ins (a ChatGPT plan, GitHub Copilot) is **[unverified]** (needs a browser sign-in).
- **Hidden calls**: the stored session cost (`session export`, `info.cost`) also includes calls that never show as `step_finish`: title generation (about $0.00005 per untitled session) and compaction summaries. A killed turn reports no cost in the stream. **[observed, V8, V13, V16]**
- **Totals**: `opencode stats --cost --json` reports totals across sessions. **[observed]** Its shape: `{range, sessions, subagents, prompts, steps, tokens: {input, output, reasoning, cache: {read, write}}, cost, tools: {totals, usage[]}, activeDays, streak, activity, models[]}`. Its cost is the summed `step_finish` costs plus the steps of killed turns. **[observed, V8]**

### 2.9 Login state

- **Connecting a provider**: credentials are entered with `/connect` in the TUI; some providers use environment variables or cloud credential chains. **[documented]** <https://opencode.ai/docs/providers/>
- **Credential storage**: credentials live in the database. **[unverified; seen in source]** <https://github.com/anomalyco/opencode/blob/dev/packages/core/src/credential.ts>
- **Listing logins**: `opencode auth list --format json` lists integrations. **[observed]** `auth export` prints secrets and must never be used. **[observed]**
- **For a login check** **[observed, V11]**:
  - `auth list --standalone --format json` is accepted, though `--format` is not in its help.
  - it is **secret-free**: with provider variables set it lists `{id, name, connections: [{type: "env", name: "OPENROUTER_API_KEY"}]}`, names only, no values.
  - it **misses providers keyed in a config file**: with the key in the config it prints `[]`. (Corrected: the earlier reading was that it misses providers configured by environment variable.)
  - variable names: `OPENROUTER_API_KEY` and `ANTHROPIC_API_KEY` observed; `OPENCODE_API_KEY` and `OLLAMA_API_KEY` documented.

### 2.10 Config and isolation

- **Config files**:
  - managed or system config, then global `~/.config/opencode/opencode.json(c)`, then project `opencode.json(c)` and `.opencode/` from the directory upwards. Managed values cannot be overridden. **[documented]** <https://opencode.ai/docs/config/>
  - JSON or JSONC, with `{env:…}` and `{file:…}` substitution. **[documented]**
- **Variables** (all **[unverified; seen in source]**, <https://github.com/anomalyco/opencode/blob/dev/packages/cli/src/server-process.ts>):
  - `OPENCODE_CONFIG` (a config file), `OPENCODE_CONFIG_CONTENT` (inline JSON), `OPENCODE_CONFIG_DIR`;
  - `OPENCODE_DISABLE_PROJECT_CONFIG`, `OPENCODE_DISABLE_CLAUDE_CODE*`;
  - `OPENCODE_DISABLE_MODELS_FETCH`, `OPENCODE_FLAGS`.
  - `OPENCODE_DISABLE_FILEWATCHER` exists in the source; its effect was not tested. The server sets file watchers on every parent directory up to `/`. **[observed, live checks]**
- **Precedence** (corrected by V4 and V5): `debug config --standalone` lists the sources in this order: the global document, the global directory, the `OPENCODE_CONFIG` file, the project `opencode.json`, then `OPENCODE_CONFIG_CONTENT` (no path). **[observed, V4, V5]**
  - so `OPENCODE_CONFIG` loads **before** project config, and project `permissions` win over it; `OPENCODE_CONFIG_CONTENT` loads **after** project config, and its permissions win (2.5).
  - `OPENCODE_DISABLE_PROJECT_CONFIG=1` drops the project document. **[observed, V5]**
  - `debug config` shows a provider key as `"***"`. **[observed, V4]**
- **`update: "disable"`** turns off automatic updates, and is honoured only in the global config ("Project-level values are ignored"). **[documented]** Whether `OPENCODE_CONFIG` counts as global config is **[unverified]**.
- **Machine-wide config** (`/Library/Application Support/opencode`, managed preferences) applies whatever the XDG directories are, so moving them does not avoid it. **[unverified; noted in the live checks]**
- **XDG directories**: `XDG_CONFIG_HOME` moves `cli.json` **[documented]** <https://opencode.ai/docs/cli/config/>. **[observed, V19]**:
  - `opencode --version` runs in 0.06–0.19 s and prints `opencode v2.3.2`, with a **leading `v`**.
  - it creates `$XDG_CACHE_HOME/opencode/bin` (empty), `$XDG_CONFIG_HOME/opencode`, `$XDG_STATE_HOME/opencode`, `$XDG_DATA_HOME/opencode/` with `repos` and `log/opencode.log`, `$TMPDIR/opencode`, and a Bun `.dylib` in `$TMPDIR`.
  - the log holds one line, `cli starting`, with the version, `channel=latest` and the arguments.
- **The background service**:
  - by default every CLI command uses **one shared background server per user**. `--standalone` runs a private server for that command. **[documented]** <https://opencode.ai/docs/cli/>
  - the server reads the `OPENCODE_DISABLE_*` variables at its start; for the service they must be persisted with `opencode service set env`. **[documented]** <https://opencode.ai/docs/instructions/>
  - **A turn's environment therefore reaches opencode's configuration only with `--standalone`.**
- **Counterparts of Claude's `--strict-mcp-config` and `--setting-sources`, and Codex's `--ignore-user-config`**: there is no single switch.
  - a policy `mcp.use` `deny *` plus `allow` per wanted server excludes other MCP servers.
  - `plugin.use` `deny *` excludes plugins.
  - `OPENCODE_DISABLE_PROJECT_CONFIG` drops project config.
  - the global config can only be avoided by moving `XDG_CONFIG_HOME`. That variable is inherited by the commands the agent runs, where `git`, `gh` and others read it too. **[observed, V15]**:
    - the agent's shell printed the temporary `XDG_CONFIG_HOME`: the leak is real.
    - without other settings the shell was `/bin/zsh +o nomatch -c …`.
    - **a workaround worked**: the config setting `"shell": "<wrapper>"`, where the wrapper exports another `XDG_CONFIG_HOME` and `exec`s bash. The agent's command then saw the wrapper's value. MCP servers have their own `environment` (2.6); the adapter will set the same value there (4.6).

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
  - **automatic compaction runs mid-turn, between steps, with nothing in the JSON stream.** It is stored as a message `type: "compaction"`, `reason: "auto"`, with its own cost, which is not in any `step_finish`; it shows only in `session export`. **[observed, V13]**
  - `/compact` as a prompt is plain text. **[observed, V13]**
  - **manual compaction works through the CLI**: `opencode api --standalone post /api/session/<id>/compact --data '{}'` makes no model call itself and returned `{"data": {"id": "msg_…", "type": "compaction", "delivery": "steer"}}` in 0.16 s. The compaction then ran at the start of the next `run` (`reason: "manual"`); that turn took 4.5 s instead of about 1.4 s. **[observed, V13]**
- **Server mode**:
  - `opencode serve` runs the V2 API: sessions, prompts, events, permission replies, an OpenAPI document at `/docs/openapi.json`. **[documented]** <https://opencode.ai/docs/api>
  - the event stream is "volatile by contract". **[documented]** (same page)
  - the client library is TypeScript (`@opencode/client`). **[documented]** <https://opencode.ai/docs/build/client/>
  - `opencode acp` speaks the Agent Client Protocol over stdio. **[observed]**
- **Decided: one `opencode run --standalone --format json` process per turn**, like the other providers.
  - It keeps the runner's model: one process group per turn, killable, a stdout stream, no daemon, isolation through the environment.
  - A long-lived `serve` would need daemon management, an HTTP client in Python, a password, per-location config and handling of missed events.
  - ACP is interesting as a later, generic provider for several harnesses, but it needs a long-lived process per session.
  - ACP is on the roadmap: [ACP as a generic provider](../ROADMAP.md#acp-as-a-generic-provider).

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
  env:   OPENCODE_CONFIG=<private file>, OPENCODE_CONFIG_CONTENT=<permission rules>,
         PWD=<agent cwd>, the wirings' env
```

- **`--standalone`**: the turn's environment and config apply only to its own server, and the user's shared service is neither used nor started.
- **New session**: `<ses_id>` is chosen by Spartito (`ses_` and a random hex suffix; V2 confirmed it). `--archived` and `--title` are given only then. `--title` also avoids the hidden title call (2.8). `--archived` only marks the session; in 2.3.2 it still shows in `session list` (V18).
- **The turn's config file** is written as an `Invocation.private_files` entry, at the path the runner already gives as `ctx.mcp_config`, and named by `OPENCODE_CONFIG`. It holds:
  - `instructions`: the absolute path of a per-turn file in the scratch directory holding the role prompt and attribution. **Role prompt: every turn**, after the project's `AGENTS.md`.
  - `policies`, from the loadout (4.2). The `permissions` go into `OPENCODE_CONFIG_CONTENT` instead, because project config ranks above the `OPENCODE_CONFIG` file and below `OPENCODE_CONFIG_CONTENT` (V5); every deny is also backed by a `tool.use` or `mcp.use` policy.
  - `mcp.servers`: the `orch` MCP server for leads, and the `mcp:` servers (4.3).
  - `update: "disable"`, so a turn never updates the CLI. The docs honour it only in the global config; whether the `OPENCODE_CONFIG` file counts as global is not verified (4.6).
  - `shell`: the wrapper that restores the user's `XDG_CONFIG_HOME`, when the global config is moved away (4.2, Q9).

### 4.2 Loadout keys

| key | opencode |
|---|---|
| `tools` | Claude tool names map to actions: `Bash`→`shell`; `Read`→`read`; `Edit`/`Write`/`MultiEdit`/`NotebookEdit`→`edit`; `Glob`→`glob`; `Grep`→`grep`; `WebFetch`→`webfetch`; `WebSearch`→`websearch`; `Task`/`Agent`→`subagent`; `Skill`→`skill`. A built-in action that no listed tool covers gets a `deny` rule and a `tool.use` deny policy. `default` denies nothing extra; `none` denies every built-in action. `ToolSearch` and unknown names are ignored, with a note |
| always | `question` denied (nobody answers). The `opencode` namespace tools that create, move or remove worktrees and sessions are denied (Spartito owns both): `opencode_session_move`, `opencode_session_rename`, `opencode_worktree_create`, `opencode_worktree_list`, `opencode_worktree_remove` (V6; `worktree_remove` also uses the `edit` permission). `external_directory` denied except the turn's `writable_dirs` (the shared `.git`) |
| `mcp` | `strict`: policies `mcp.use` `deny *`, then `allow` for the `orch` MCP server and each turn server. `inherit`: no policy |
| `setting_sources` | without `project`: `OPENCODE_DISABLE_PROJECT_CONFIG=1`. This also drops project `AGENTS.md`, unlike Claude, where `project` keeps `CLAUDE.md`. Without `user`: policies `plugin.use` `deny *`, as before, and the global config is moved away: `XDG_CONFIG_HOME` points at a temporary config home of the turn, and the turn config's `"shell"` is a wrapper that gives the agent's commands the user's own value back; MCP servers get it through their `environment` (Q9, 4.6) |
| `slash_commands` | `false`: `skill` denied and `skill.use` `deny *` |
| `memory` | no effect (opencode has no memory directory). A listed limit |
| `tool_search` | no effect. Code Mode, opencode's default for MCP servers, is its own way: it keeps the schemas out of the request (V12) |
| `codex_user_config` | no effect |

### 4.3 Capabilities and the `orch` MCP server

- **`chrome`**: not supported (Claude only). An agent on opencode skips it with the existing note.
- **`mcp:<name>`**: supported (`carries_mcp_servers = True`). The canonical entry is translated:
  - `command` + `args` → `command: [command, *args]`; `env` → `environment`; `url`/`headers` → `type: "remote"`;
  - `${NAME}` → `{env:NAME}`; `${NAME:-default}` → `{env:NAME}` when the turn's environment has it set, else the default written literally.
  - The pre-turn check of unset variables stays as it is.
  - Server statuses: the JSON output has none, and a server that fails to start is silent there (V9), so `mcp` statuses stay "not reported". The only possible source is stderr with `--print-logs` (`mcp connected` / `mcp connect failed`).
  - MCP servers on opencode follow the same marked-server rule as Codex (Q8, decided).
- **The `orch` MCP server** (leads):
  - a local server with the runner's `McpServer`, `timeout.execution` well above the longest wait, allowed in policies and permissions.
  - its tools are named `orch_<tool>`. The role prompts name no tool by its full Claude name, so they need no change.

### 4.4 The rest

| topic | opencode |
|---|---|
| permission modes | `default` (the default): no `--auto`, so every `ask` is rejected and the turn goes on. `auto`: `--auto`. Other values are refused when set |
| model and effort | `--model <model>#<effort>`. Effort without a model: ignored, with `Invocation.notes`. A model that already contains `#` keeps its own variant. `efforts = ("low", "medium", "high", "max")` as offered names; a model may reject one (the turn fails with opencode's error) |
| model list | `opencode models` through `kit.run_cli`, with `OPENCODE_DISABLE_MODELS_FETCH=1`: plain lines, credentialed providers only, no variants (V10) |
| sessions | Spartito's own `ses_` id; resume with the same `--session`. A lost session restarts empty instead of failing: a listed limit |
| compaction | no `compact_prompt` in the first version, so the provider cannot compact. opencode compacts on its own; this is not reported (V13). V13 showed a way to offer it after all (4.6) |
| context size | from the last `step_finish` of the turn: `input + cache.read + cache.write` (confirmed by V8). No window, so `reports_context = True` with no `windows` |
| structured answers | `kit.schema_in_prompt`. The stream's `finish()` sets `structured` from `kit.extract_json` of the last text |
| cost and usage | summed over the turn's `step_finish` parts, in USD: per turn, `cumulative=False`. `reports_cost = True`, but a subscription sign-in may report `0` (not verified). Hidden title and compaction calls are not counted, and a killed turn reports no cost (V8, V16) |
| stream | text parts accumulate per part id and are emitted when a step ends (each arrives whole, once, V17); `tool_use` → `tool` (error state → `tool_result`; Code Mode errors come as `completed` with the error in `output`); `retry` and `background_wait` → `info`; `error` → `error` and a failed result, read from the nested `error.error.type`/`.message` (V1); `finish(code)` → `result` with the last message's text and the turn's usage. A signal exit (143, no closing line, V16) counts as stopped |
| lead turns | the turn ends when the process exits. opencode waits for background jobs first, and wakes the model for one more step when they end (V12) |
| attribution | the Codex rule ("when you add a Co-Authored-By trailer…"), credited as `[OpenCode](https://opencode.ai)` |
| availability | `opencode --version` (the leading `v` of `opencode v2.3.2` stripped), with `min_version = (2, 2, 3)` (policies). An older version is not usable: "opencode 1.x is not supported". Login check: `auth list --format json` is secret-free but misses providers keyed in a config file (V11) |
| attach | `opencode --session <id>`, the interactive UI on the user's shared service |
| `config_env` | `XDG_CONFIG_HOME`, `XDG_DATA_HOME`, `OPENCODE_DB` |

### 4.5 Gaps, and what the user notices

- **No Claude in Chrome.** The capability is skipped with a note.
- **No `memory`.**
- **The answer schema is asked for, not enforced.** An answer that is not JSON leaves `structured` empty, and a lead gets the text.
- **The Compact button and `spartito compact` refuse an opencode agent** in the first version (4.6 has a way to offer it). opencode's own compaction runs unseen, and the context gauge may drop without a log line.
- **No context window in the gauge.**
- **Costs may show `$0.00` for subscription sign-ins**, and per-turn costs miss opencode's hidden title and compaction calls.
- **No OS sandbox.** The agent's shell can write outside its worktree. Only opencode's own tools are held back.
- **Agent commands run in bash, not the user's shell** (for example zsh), when the global config is moved away (4.2): the `shell` wrapper `exec`s bash.
- **Sessions are stored in the user's opencode database**, marked archived but still listed by `opencode session list` (V18), and visible in opencode's own UI.
- **A lost session restarts empty** instead of failing.
- **`spartito chat` stays Claude only.**

### 4.6 What the live checks change in the adapter

For phases 5b–5d; the facts are in §2, the results in §5.

1. **Turn config**: permission rules go into `OPENCODE_CONFIG_CONTENT`, which ranks above project config; every deny is backed by a `tool.use` or `mcp.use` policy. The namespace tools are denied by their action names `opencode_session_move`, `opencode_session_rename`, `opencode_worktree_create`, `opencode_worktree_list`, `opencode_worktree_remove` (V5, V6).
2. **`update: "disable"`** may be ignored outside the global config; whether `OPENCODE_CONFIG` counts as global is not verified.
3. **Errors**: read the nested `error.error.type` and `.message`; treat Code Mode output text as a possible error (V1).
4. **Stream timing**: text arrives once per part, at its end, and tools only on completion. The dashboard sees nothing during long tool calls. Text keeps being accumulated per part id (V12, V17).
5. **MCP in the stream**: in Code Mode, MCP calls show as `execute` with code; the server name can only be read from `tools.<server>.<tool>` in it (V9).
6. **MCP status**: a failed server start is silent in the stream, and `mcp list` does not see turn servers. The only possible source is stderr with `--print-logs` (V9).
7. **SIGTERM**: expect exit 143 with no closing line, and treat a signal exit as stopped. The turn spans three extra process groups (server, pty helper, shell); the runner's tracking of descendants should cover them. The SIGKILL path is untested (V16).
8. **Usage**: context = `input + cache.read + cache.write` is confirmed, and cost is USD. Per-turn cost misses hidden title and compaction calls, and a killed turn reports none. Always pass `--title` on new sessions (V8).
9. **Compaction**: the Compact button could be offered after all, by queueing `opencode api --standalone post /api/session/<id>/compact` and running the next turn (no instructions possible). Automatic compaction leaves no trace in the stream; it shows only in `session export` (V13).
10. **`--archived`** does not hide sessions in 2.3.2 (V18); §2.2, §4.1 and §4.5 say so.
11. **Model list**: `opencode models` with `OPENCODE_DISABLE_MODELS_FETCH=1` is usable: plain lines, credentialed providers only, no variants (V10).
12. **Login check**: `auth list --format json` is secret-free but misses providers keyed in a config file (V11).
13. **Isolation** (Q9): the adapter will point `XDG_CONFIG_HOME` at the turn's temporary config home and set the turn config's `"shell"` to a wrapper that restores the user's original `XDG_CONFIG_HOME` (or unsets it, when it was unset) and `exec`s bash. MCP servers get the same original value through their `environment`. A test with the fake-CLI dialect fails if the wrapper or the MCP `environment` is no longer applied; no live run is needed (V15).
14. **Version**: strip the leading `v` of `opencode v2.3.2` (V19).
15. **First event**: `step_start` comes at the first model token, so the first event of a turn is the time to first token (V12).
16. **Further settings to consider**: the server watches every parent directory up to `/`; `OPENCODE_DISABLE_FILEWATCHER` exists in the source, its effect untested. Machine-wide config applies whatever the XDG directories are.

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
| 5b | `providers/opencode.py`: invocation, turn config, loadout mapping, MCP translation; the `XDG_CONFIG_HOME` move with the `shell` wrapper and its test (about S of it, Q9); registered last in `BUILTIN`; a fake `opencode` on `PATH` in the same change | `providers/opencode.py`, `providers/__init__.py`, `tests/test_opencode.py` | 2, 3, 4, 5a | M |
| 5c | opencode stream: usage, context, structured answers, the dialect | same owner as 5b | 5b | M |
| 5d | Model list, availability, docs (`providers.md`, `configuration.md`, `capabilities.md`, `mcp.md`, `architecture.md`), `CHANGELOG.md` | docs, small code | 5b | M |

- **Total**: about 7.5 worker-days.
- **Critical path**: 1 → 2 (or 4) → 5b → 5c (5d alongside), about 4 days with three workers.
- **In parallel**: 2, 3 and 4 run alongside each other after 1.

**Risks**:
- **opencode's surface changes**: undocumented `run` flags and JSON events, weekly releases. Mitigations: `min_version`, defensive parsing, a recorded fixture per tested version, and a note of the tested version in `providers.md`.
- **Config precedence**: the `OPENCODE_CONFIG` file loads before project config, so a repository's `opencode.json` can widen its permissions; `OPENCODE_CONFIG_CONTENT` loads after it. The turn's permission rules therefore go into `OPENCODE_CONFIG_CONTENT`, and policies, which held against the project, back every deny (V5).
- **Isolation**: moving `XDG_CONFIG_HOME` leaks into the agent's commands (V15) unless the `shell` wrapper and the MCP servers' `environment` restore the user's value. Risks: one of them is not applied (covered by the test of 4.6, item 13), or a future opencode release changes or drops the `shell` setting.
- **Shared database**: concurrent standalone processes share one SQLite database, as does the user's own opencode. Three parallel turns ran without lock errors (V14).
- **Silent new session**: a missing `PWD` sends the turn to the wrong directory; a lost session id silently starts a new one.
- **Refactor drift**: mitigated by the existing tests, the normal review and phase-by-phase pull requests.

**Live verification.**
- The plan: every check uses temporary `XDG_DATA_HOME`/`XDG_CONFIG_HOME`/`XDG_STATE_HOME`/`XDG_CACHE_HOME` and a throwaway git repository; model turns run on a local model through Ollama (mechanics only) or with an API key for one provider (usage and cost); no login of the owner's real opencode data is touched; each run needs the owner's go-ahead.
- **The run**: the checks were run on opencode **2.3.2** (`opencode v2.3.2`):
  - about 36 `run` invocations, 31 of them billed (four failed before any model call on purpose; one was rejected for a dummy key);
  - all opencode state in isolated temporary directories, with `--standalone`; the binary was unchanged afterwards;
  - a small OpenAI model and a small Anthropic model through OpenRouter (`openai/gpt-4.1-mini` for the mechanics, `anthropic/claude-haiku-4.5` for V8);
  - about US$0.09 in all: the `step_finish` costs, two killed steps, two compaction summaries and the automatic titles;
  - a fake stdio MCP server stood in for MCP servers, including the long waits of a lead.

| # | check | how | model turn | account | result |
|---|---|---|---|---|---|
| V1 | event shapes, exit codes, the end of a turn | `run --standalone --format json` with a short prompt, then one that fails | yes | yes | **partly**: shapes and exit codes as in 2.1; the `error` line is nested; Code Mode errors come as `completed` |
| V2 | `--session ses_<hex>` creates, then resumes, with context | two turns | yes | yes | **verified**; a non-`ses` id fails |
| V3 | prompt from stdin without a positional message | one turn | yes | yes | **verified**; with both, argument + newline + stdin; empty fails |
| V4 | `instructions` with an absolute path is appended | `opencode debug config` in the test repository | no | no | **partly**: accepted in the config; reaching the prompt not tested |
| V5 | `OPENCODE_CONFIG` against project config: permissions and policies | `debug config`/`debug agents`, then one turn | partly | partly | **differs**: project permissions win over `OPENCODE_CONFIG`; its policies hold; `OPENCODE_CONFIG_CONTENT` wins |
| V6 | action names of the `opencode` namespace tools; `tools` deny rules hold | `debug agents`, one turn asking for a denied tool | yes | yes | **verified**: `opencode_<function>`; a deny removes the tool |
| V7 | `PWD` decides the directory | one turn with cwd ≠ `PWD` | yes | yes | **verified**; `session list` too |
| V8 | token fields (cache inside `input`?), cost unit, cost under a subscription | turns; compare with `opencode stats --cost --json` | yes | yes | **verified**: `input` excludes cache reads and writes; USD; hidden title and compaction costs; subscription not verified |
| V9 | Code Mode and the context cost of a stdio MCP server | a local fake MCP server; one turn | yes | yes | **verified**, two surprises: MCP calls show as `execute`; a failed server is silent |
| V10 | `opencode models` offline, its format, variants | the command with fetching off | no | yes (to list models) | **verified**: plain lines, no variants; network not actually cut |
| V11 | `auth list --format json` offline and secret-free | the command on the temporary data directory | no | no | **verified**: secret-free; misses providers keyed in a config file |
| V12 | start-up time per turn; a lead turn with the `orch` MCP server, long waits, background jobs | timed turns | yes | yes | **verified** with a fake MCP server: short turns 1.55–1.83 s; `step_start` at the first token; background jobs waited for; no event during a long call |
| V13 | compaction under `run`: any visible event; a prompt-level compact | a long session | yes | yes | **verified**, more than expected: automatic compaction leaves no event; `/compact` is plain text; manual compaction through `opencode api` works |
| V14 | parallel standalone turns on one database | three turns at once | yes | yes | **verified**: no lock errors |
| V15 | whether `XDG_CONFIG_HOME` reaches the agent's shell | a turn that prints its environment | yes | yes | **verified leak**; a `shell` wrapper avoids it |
| V16 | SIGTERM ends the process group within the runner's grace time | stop a running turn | yes | yes | **differs**: group gone in 0.07 s, but exit 143 with no closing line; three extra process groups |
| V17 | `text` parts as suffixes | one turn | yes | yes | **differs**: one event per part, whole text, at the part's end |
| V18 | `--archived` keeps sessions out of `session list` | after V2 | no | no | **differs**: still listed |
| V19 | what `--version` writes (directories, logs) | temporary directories | no | no | **verified**: as in 2.10; leading `v` |

**Not verified**:
- cost under a subscription sign-in (needs a browser sign-in);
- a lead turn with Spartito's real `orch` MCP server (a fake MCP server covered the long wait);
- `retry` and `reasoning` events (none occurred; `--thinking` not tried);
- `opencode models` with the network actually cut (V10);
- SIGKILL instead of SIGTERM (V16);
- whether `update: "disable"` in the `OPENCODE_CONFIG` file counts as global config;
- whether an `instructions` file reaches the prompt (V4);
- denying `opencode_worktree_create` and `opencode_session_move` directly; part of V6's function list comes from the model's own report;
- the effect of `OPENCODE_DISABLE_FILEWATCHER`;
- the spend after the last `opencode stats` reading is an estimate.

**Installing opencode for these checks**: not needed. It is already installed (2.3.2). Upgrading would need the owner's go-ahead, because releases are frequent and the checks should run on one pinned version.

**Decided questions**:

Any of them can still change during implementation, but only with the owner.

Decided: no golden tests for now; the existing tests and the normal review carry the refactor.

1. **Q1 Live checks**: run V1–V19 with the installed 2.3.2, temporary state, and a local model (plus one API-key provider for V8)? Decided: run live checks V1–V19 on the installed 2.3.2 with temporary state (each run still needs the owner's go-ahead).
2. **Q2 Versions**: support only opencode 2.2.3 or later, and refuse 1.x? Decided: support only opencode 2.2.3 or later; refuse 1.x.
3. **Q3 A provider-neutral "user configuration" loadout key** in place of `codex_user_config`? Decided: no provider-neutral user-config key for now; map opencode onto `setting_sources`, keep `codex_user_config`.
4. **Q4 Default permission mode** for opencode: no `--auto` (asks rejected), or `--auto`? Decided: no `--auto`.
5. **Q5 Structured answers** asked for in the prompt, not enforced: acceptable for planner, reviewer and tester roles? Decided: structured answers are asked for in the prompt and listed as a limit.
6. **Q6 Server model**: one process per turn now, `serve` or ACP later? Decided: one process per turn; ACP is on the roadmap ([ACP as a generic provider](../ROADMAP.md#acp-as-a-generic-provider)).
7. **Q7 Order of work**: refactor (1–4) before opencode, or opencode first on today's structure? Decided: refactor first (phases 1–4), then opencode.
8. **Q8 MCP servers on opencode** without an approval gate (as decided for Codex in [mcp.md](mcp.md#decisions)): allow them only when the owner marks a server for it, or in general? Decided: MCP servers on opencode follow the same marked-server rule as Codex.
9. **Q9 Global opencode config**: if V15 shows the leak, accept that the user's global `opencode.json` applies to agents (listed), or deny opencode agents some of it through policies only? Decided anew after V15, replacing the earlier answer (accept the global config and list it as a limit): when `setting_sources` leaves out `user` (4.2), the adapter moves the global config away by pointing `XDG_CONFIG_HOME` at the turn's temporary config home. The turn config sets `"shell"` to a wrapper that restores the user's original `XDG_CONFIG_HOME` (unsets it when it was unset) and `exec`s bash; MCP servers get the same value through their `environment`. So `git`, `gh` and the user's other tools keep reading the user's own config (for example the `gh` login under `~/.config/gh`). Accepted side effect: agent commands run in bash, not the user's shell (listed in 4.5). A test with the fake-CLI dialect fails if the wrapper is no longer applied. About S, inside phase 5b.

See also [mcp.md](mcp.md#wiring-per-provider) for how MCP servers are wired per provider,
[configuration.md](configuration.md#model-and-effort) for model and effort, and
[architecture.md](architecture.md) for the layers.

---

## 6. Google Antigravity and Gemini CLI (research, not built)

> **Status: research only; decided against a direct provider.** Nothing here is built, and
> neither Antigravity nor Gemini CLI becomes a provider of its own. Antigravity may come up only
> through the generic ACP provider on the roadmap (6.7). Facts were checked on 2026-11-18
> against the pages linked; the quotes of the Antigravity FAQ and terms and of Gemini CLI's
> terms page were re-checked on 2026-11-19. Google's doc pages carry no dates. Marks: **[documented]** in Google's docs or
> repositories; **[unverified; …]** from a third-party page, a forum, or absent from the docs;
> **[observed]** seen on the research machine. An `agy` executable is installed there; it was
> not run, so its version is unknown. `gemini` is not installed.

### 6.1 Antigravity: what it offers for headless use

- **Products.** The Antigravity app (Agent Manager) and the Antigravity IDE are desktop apps with no headless mode. **[documented by absence]** <https://codelabs.developers.google.com/getting-started-agy-ide>
  The scriptable surface is the separate **Antigravity CLI**, command `agy`, written in Go, on its own version line (1.5.11 at the time of writing). **[documented]** <https://antigravity.google/docs/changelog>, <https://github.com/google-gemini/gemini-cli/discussions/27274>
- **One process per turn**: `agy -p` "Run a single prompt non-interactively in headless mode and print the response." **[documented]** <https://antigravity.google/docs/cli/reference>
- **Prompt**: the value of `-p`, or one `{"event":"user","message":{"content":…}}` line on stdin with `--input-format stream-json`. That needs `--output-format stream-json`; closing stdin ends the run after the turn, with exit 0. **[documented]** <https://antigravity.google/docs/cli/headless>
- **Events**: `--output-format text|json|stream-json`. In stream-json: `init` (cwd, tools, permission_mode, model), `step_update` (conversation_id, step_type, text_delta, tool_info, usage, subagent_info), and a final `result` with status `SUCCESS|ERROR|CANCELED|INTERRUPTED|…`. **[documented]** (same page)
- **Session**: `--conversation <id>` resumes; the id is `conversation_id` in the stream. It is chosen by `agy`, not by the caller. **[documented]** (same page)
- **Usage**: `input_tokens`, `output_tokens`, `thinking_tokens`, `cache_read_tokens`, `total_tokens`. There is **no cost** and no context-window field. In a stdin session, usage is cumulative across turns. **[documented]** (same page)
- **Structured answers**: `--json-schema <string|file>` **enforces** the schema; the result carries `structured_output`. **[documented]** (same page)
- **Exit codes**: 0 on success. Failures are non-zero (an unknown model gives 1; a slash command on stdin gives 2), with `status`/`error` in the JSON. **[documented]** (same page)
- **Model and effort**: `--model` (list them with `agy models`), `--effort low|medium|high|xhigh|max` depending on the model. An unknown model fails and does not fall back. **[documented]** (same page) The output format of `agy models` is **[unverified]**.
- **Permissions**:
  - `--mode accept-edits|auto|plan`, `--sandbox`, `--dangerously-skip-permissions`. **[documented]** <https://antigravity.google/docs/cli/reference>
  - Allow/ask/deny rules (`command(git)`, `write_file(src/)`, `mcp(server/*)`) are documented only in the global `~/.gemini/antigravity-cli/settings.json`; no per-run flag or variable is documented. **[documented]** <https://antigravity.google/docs/permissions?tab=cli>
  - In headless mode an action that needs approval is **soft-denied**: the run goes on, exits 0, and names the tool on stderr. **[documented]** <https://antigravity.google/docs/cli/headless>
  - `auto` became the default mode for `-p` in 1.5.7. **[unverified; third-party release tracker]** <https://www.havoptic.com/releases/antigravity-cli/2026/11>
- **MCP**:
  - Servers are read from `~/.gemini/config/mcp_config.json` or `<workspace>/.agents/mcp_config.json`, with fields `command`/`args`/`env`/`cwd` or `serverUrl` (Streamable HTTP only), and `enabledTools`/`disabledTools`/`timeoutSeconds`. **[documented]** <https://antigravity.google/docs/mcp?tab=cli>
  - The default tool-call timeout is 180 s; `timeoutSeconds` < 0 turns it off. **[documented]** (same page)
  - There is no per-run MCP file or flag, and nothing says whether `-p` loads the servers. **[unverified]**
- **Isolation**: no documented variable moves the config home, and there is no flag to ignore the user's settings, plugins or hooks. **[documented by absence]**
- **ACP**: `agy acp` is an ACP server over stdio. It supports session load/resume/list/close, permission requests, and a model and thinking-level picker. `--process-per-session` is "required for HTTP MCP servers that your editor configures". **[documented]** <https://antigravity.google/docs/ide/extensions/acp/> It was added in 1.5.0. **[unverified; third-party release tracker]**
- **SDK**: a Python SDK (`google-antigravity`) with API key or Vertex auth, MCP, policies and session persistence. It is not a per-turn CLI. **[documented]** <https://antigravity.google/docs/sdk/overview>

### 6.2 Gemini CLI on the same points

- **Status**:
  - Free-tier and Google AI Pro/Ultra users lost Gemini CLI on 2026-06-18. It "will remain accessible via paid Gemini and Gemini Enterprise Agent Platform API keys". **[documented]** <https://developers.googleblog.com/an-important-update-transitioning-gemini-cli-to-antigravity-cli/>
  - It is "maintained for enterprise Gemini Code Assist workflows" and is not tuned for Gemini 4 models. **[documented]** <https://github.com/google-gemini/gemini-cli>
- **Prompt**: `-p` "Appended to stdin input if provided. Forces non-interactive mode." **[documented]** <https://github.com/google-gemini/gemini-cli/blob/main/docs/cli/cli-reference.md>
- **Events**: `-o text|json|stream-json` with events `init`, `message`, `tool_use`, `tool_result`, `error`, `result`. The field names are not documented. **[documented; fields unverified]** <https://github.com/google-gemini/gemini-cli/blob/main/docs/cli/headless.md>
- **Session**: `-r latest|<index>`, and the examples resume by id. **[documented]** (cli-reference)
- **Permissions**:
  - `--approval-mode default|auto_edit|yolo|plan`. **[documented]** (cli-reference)
  - `--allowed-tools` is deprecated in favour of the Policy Engine. **[documented]** (cli-reference)
- **MCP**:
  - `mcpServers` in the user or project `settings.json`, with `includeTools`/`excludeTools`, `trust` and `${VAR}`. **[documented]** <https://geminicli.com/docs/tools/mcp-server>
  - Tool names are `mcp_<server>_<tool>`. **[documented]** (same page)
  - `--allowed-mcp-server-names` limits which servers load. **[documented]** (cli-reference)
- **Model and effort**: `-m` (default `auto`); no effort flag. **[documented by absence]**
- **Usage and output**:
  - Per-model token stats, no cost, no output-schema flag. **[documented by absence]** (headless.md)
  - Exit codes: 0 success, 1 error, 42 input error, 53 turn limit. **[documented]** (headless.md)
- **ACP**: `--experimental-acp`, "Experimental feature." **[documented]** (cli-reference)
- **Account and config relation**:
  - Both CLIs keep their configuration under `~/.gemini`. `agy` imports Gemini CLI extensions and settings and moves session tokens to the OS keyring. **[documented]** <https://antigravity.google/docs/cli/gcli-migration>
  - Both read `GEMINI.md` and `AGENTS.md`. **[documented]** (same page)
  - The Antigravity terms sanction "your Antigravity and/or Gemini CLI accounts" together. **[documented]** <https://antigravity.google/terms>
  - Whether one sign-in serves both is **[unverified]**.

### 6.3 Fit with the kit

Technically, `agy -p` is closer to Claude Code than Codex is: it has stream-json, an enforced schema, resume by id, model and effort flags. A per-turn module, `providers/antigravity.py` (not to be built, 6.7), would contain:

- **Invocation**: `agy -p --input-format stream-json --output-format stream-json --disable-slash-commands [--conversation <id>] [--model …] [--effort …] [--mode …] [--json-schema <kit.schema_file>] [--add-dir <.git>]`, with the prompt on stdin.
- **Role prompt**: `kit.with_role_prefix`, because there is no system-prompt flag.
- **MCP servers**: the `orch` MCP server and the `mcp:` servers in a `.agents/mcp_config.json` written into the worktree and kept out of git, with `timeoutSeconds: -1` for the `orch` MCP server.
- **Stream**: a `kit.JsonLines` stream that maps `step_update` deltas and tools, and `result` with usage and `structured_output`.

| # | phase | depends on | size |
|---|---|---|---|
| G0 | Live checks L1–L6 | the owner's go-ahead | S |
| G1 | Adapter and config: invocation, stdin prompt, MCP file, mode mapping, fake `agy` on `PATH` | opencode phases 3–4 (stream hook, fake-CLI harness) | M |
| G2 | Stream parser, usage (per run or cumulative), soft-deny notices, structured output | G1 | M |
| G3 | Model list, availability (`--version` not documented), API-key-only check, docs | G1 | S |

A Gemini CLI provider would take the same phases and sizes, but without an enforced schema. An ACP route is the roadmap's generic provider ([ROADMAP.md](../ROADMAP.md#acp-as-a-generic-provider)), L or more for the runner; `agy acp` then costs S on top. That is cheaper only when it serves several harnesses.

**Gaps a user would notice (agy) compared with Claude Code**:
- **Structured answers**: these do work; the schema is enforced.
- **Not available**: `chrome`, `memory`, and compaction (no headless compact, no compaction event; `spartito compact` refuses).
- **Cost**: not reported. Tokens only, so costs show as unknown.
- **Per-role tool restriction**: not possible. Only global rules exist; per turn there are only `--mode` and `--sandbox`.
- **Denied tools**: a denied tool does not fail the turn (soft-deny, exit 0).
- **Isolation**: the user's global settings, MCP servers, plugins, hooks and `GEMINI.md` apply to agents. No config-home variable is documented.
- **Sessions**: they live in the user's own Antigravity state.

### 6.4 Licensing and terms

- **Antigravity FAQ**: "Antigravity CLI headless mode (`agy -p`) is intended for direct scripting and CI workflows." The same entry says that "Wrapping `agy` inside third-party coding agents or applications" … "is not supported under consumer plans". **[documented]** <https://antigravity.google/docs/faq>
- **Same FAQ, third-party software**: "Using third-party software, tools, or services to access Antigravity is a violation of our Terms of Service". It recommends "a Gemini Enterprise or Google AI Studio API key" instead. **[documented]** (same page)
- **Terms §6**: "Using third party software, tools, or services to access the Service" … "may be grounds for suspension or termination of your Antigravity and/or Gemini CLI accounts." **[documented]** <https://antigravity.google/terms>
- **Forum replies say the opposite** ("Yes, your described pattern is compliant"). The answerers are not marked as Google staff, and the FAQ contradicts them. **[unverified; forum]** <https://discuss.ai.google.dev/t/is-it-allowed-for-an-open-source-tool-to-call-the-official-agy-cli-in-headless-mode/183429>, <https://discuss.ai.google.dev/t/clarification-request-is-local-ipc-pty-wrapping-of-agy-cli-for-open-standard-editor-bridges-e-g-acp-tos-compliant/175905>
- **Gemini CLI**:
  - The code is Apache 2.0. Using third-party software with its OAuth login "is a violation of applicable terms and policies". **[documented]** <https://geminicli.com/docs/resources/tos-privacy>
  - With an API key, the Gemini API terms apply. **[documented]** (same page)
- **Not found**:
  - redistribution clauses;
  - the `agy` binary's licence;
  - rate limits beyond "Refer to the Plans documentation";
  - whether a client that is not an editor may use `agy acp` with a personal login.

### 6.5 Recommendation and outcome

- **Outcome: not built as a direct provider** (6.7). Neither Antigravity nor Gemini CLI gets a per-turn adapter of its own.
- **Antigravity is a candidate only through ACP**: if the roadmap's generic provider ([ACP as a generic provider](../ROADMAP.md#acp-as-a-generic-provider)) is built, `agy acp` may be considered for it, subject to Google's terms. Whether a client that is not an editor may use it under a consumer plan is the open point.
- **The research's recommendation** was the same in substance: integrate no Google harness now, because with a personal Google login, Spartito running `agy` is what the FAQ calls unsupported and the terms call grounds for suspension; prefer Antigravity CLI to Gemini CLI, which is closed to consumers and kept for enterprise; reconsider through ACP.

### 6.6 Proposed live checks (none run)

None of these is planned. They would only matter if the ACP provider is built and Antigravity is considered for it, and each would still need the owner's go-ahead.

| # | check | touches |
|---|---|---|
| L1 | `agy --help` and `agy -p --help`: is the installed `agy` the CLI, which version, which flags | `HOME` pointed at an empty temporary directory; it may still read the OS keyring |
| L2 | whether a temporary `HOME` isolates `settings.json` and `mcp_config.json` | same, no model turn |
| L3 | one headless turn with `GEMINI_API_KEY`: events, `conversation_id`, usage, exit codes, soft-deny | temporary `HOME`, an API key, a few cents |
| L4 | `--conversation` across processes: context kept, usage per run or cumulative | as L3 |
| L5 | a workspace `.agents/mcp_config.json` stdio server under `-p`: loaded, tool names, timeout | as L3, a throwaway repository |
| L6 | `agy models`: format, offline | temporary `HOME`; possibly an account |

### 6.7 Decided questions

1. **A1 A direct provider**: integrate Antigravity at all, given the FAQ and §6 of the terms? Decided: no direct provider, neither for Antigravity nor for Gemini CLI.
2. **A2 Sign-ins** (API key or enterprise only?): moot after A1.
3. **A3 Gemini CLI as a provider**: moot after A1 (no direct provider).
4. **A4 Per-turn or ACP?** Decided: only via ACP, through the generic ACP provider on the roadmap ([ACP as a generic provider](../ROADMAP.md#acp-as-a-generic-provider)), never a per-turn adapter of its own.
5. **A5 A written answer from Google** on a local orchestrator calling `agy -p` or `agy acp`: moot after A1.
6. **A6 Live checks L1–L6 now**: moot after A1.
