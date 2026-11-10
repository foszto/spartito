# Spartito

*Spartito* is Italian for sheet music: the score everyone plays from; the top-level agent is called Maestro. The command stays `orch`.

A simple orchestrator for steering coding agents (Claude Code, Codex). You talk to one
orchestrator agent; it directs projects, project lead agents and, below them, worker agents
working in worktrees.

```
you ── orchestrator (interactive Claude Code session, `orch chat`)
        └─ project ── lead agent (in the project's main checkout, can start its own sub-agents)
                        ├─ worker    (own git worktree + `orch/<id>` branch)
                        ├─ reviewer  (in the worker's worktree, runs after it)
                        └─ ...
```

## Installation

Requirements: Python 3.14, `uv`, `git`, and the `claude` and/or `codex` CLI, logged in.

```sh
uv tool install --editable .     # puts the `orch` command on the PATH
# or without installing, from the repo:  uv run orch ...
```

## Usage

```sh
orch project add ~/Projects/valami        # register a git repo as a project
orch chat                                 # open the orchestrator (Claude Code)
orch web                                  # web interface: http://127.0.0.1:8787
```

### Web interface

Everything can be controlled from the `orch web` interface:

- **Maestro** (top left): conversation with the top-level orchestrator agent, which
  registers projects, briefs lead agents and starts agents.
- **Add project** (**+** next to Projects): pick the repo in a folder picker (git repos are
  marked); the **✕** next to a project removes its registration.
- **Lead**: conversation with the project's lead agent (the first message creates it).
- **+ Agent**: start a new agent (task, role, Claude/Codex, model, own or shared worktree,
  "start after" dependencies).
- **Models and effort** (slider icon at the top): see below.
- Click any agent to see its conversation and tool calls, write to it (Enter sends), stop it
  (**Stop**) or archive it (**Archive**; see below). Messages in the **Queued** list can be
  edited (pencil) or withdrawn (**✕**) before they start.
- **Model and effort in the agent's header** (for every agent, the orchestrator and the leads
  too): the drop-down lists the provider's models with their versions (for Claude, the ones the
  installed Claude Code's `/model` picker offers: from the CLI's `initialize` response, without a
  model request, fetched once per process; if that fails, the aliases with `model list
  unavailable: showing aliases` next to them), then the model the last turn actually used
  (`→ claude-fable-5-2`), any name under `other…` (Enter applies it, Esc or leaving the field
  discards it), and the `inherit (…)` row, which clears the agent's own value (in parentheses,
  what then applies: the tier setting or `CLI default`). The own value is highlighted and marked
  `own`. The change applies from the next turn; it does not affect the running turn. For an
  existing session a model switch starts a new prompt cache (measured with Claude and Codex):
  the next turn re-reads the whole conversation at full price. Switching effort keeps the cache
  with Codex; with Claude it may start a new one (not measured). Codex switches models in a
  resumed session too (`codex exec resume -m`; it writes its own warning to the log).
- The **idle · 3 agents working** state means the agent itself is not working, only the agents
  it started: you can still write to it now, and it is notified when they finish. Waiting
  messages show in the **Queued** list; notifications (`notification`) appear as faint system
  messages in the conversation.

The web orchestrator is a headless agent (`orchestrator`); `orch chat` is a separate
interactive session; they see the same state. The interface has no login: it is reachable
only from your own machine and rejects requests from other websites. Do not expose it to a
network (`--host 0.0.0.0`) in an untrusted environment, because anyone who reaches it can start
code-running agents.

In `orch chat` you ask in natural language, for example: *"In the valami project, get X done.
Break it into tasks; do the implementation with Claude and the review with Codex."* The
orchestrator works with the `orch` MCP tools (`spawn_agent`, `ask_project_lead`,
`wait_for_agents`, `send_message`, `withdraw_message`, `get_agent`, `agent_log`, `stop_agent`,
`archive_agent`, …). `withdraw_message` withdraws a message you sent that has not started yet,
or rewrites its text with `replacement`.

The same works from the command line:

```sh
orch spawn -p valami -n login "Build the login endpoint ..."      # worker, own worktree
orch spawn -p valami -r reviewer --provider codex --share login-ab12 --after login-ab12 "Review the change."
orch ask valami "Design and implement the ... feature."          # to the project's lead agent
orch ps                    # project → agent → sub-agent tree, status, model/effort, tokens, cost
orch logs login-ab12 -f    # what it is doing right now
orch status login-ab12     # last result, working sub-agents, waiting messages, commits
orch send login-ab12 "Fix the review findings: ..."              # continues the conversation
orch wait login-ab12       # waits for it (exit code: 0 done, 1 failure, 2 timeout)
orch attach login-ab12     # manual takeover: interactive claude/codex in the agent's session
orch withdraw 42           # withdraw message 42, which has not started yet (#42 in the `orch status` list)
orch edit 42 "New text"    # replaces the text of the same message before it starts
orch stop login-ab12       # stops its running and waiting turns
orch archive login-ab12    # archive (see below); orch restore login-ab12 brings it back
```

In the status column of `orch ps`, `idle·3↓` means the agent itself is idle but 3 of its
sub-agents are still working (and `running·3↓` that its own turn is running meanwhile); `+2`
marks two more queued turns.

### Archiving

Finished agents can be archived: they disappear from the default view of `orch ps`, the web
list and `list_agents`, but everything is kept (record, runs, log, branch, worktree). The
archive: `orch ps --archived`, the web **Archive** view (at the bottom of the list on the
left), or `list_agents(archived=true)`. An agent is brought back with `orch restore <id>`, the
**Restore** button in the interface, or `archive_agent(restore=true)`; a message sent to an
archived agent (CLI, web, `send_message`, `ask_project_lead`) brings it back too.

```sh
orch archive login-ab12          # asks first if there are unmerged commits or uncommitted changes (--yes: doesn't)
orch archive --safe -p valami    # every idle agent whose work is safe
orch ps --archived [-p valami]
orch restore login-ab12
orch rm login-ab12 [--delete-branch]   # permanent deletion: archived agents only, together with the worktree
orch config archive-after 48 [-p valami]   # automatic archiving after this many idle hours; 0 or off: never
```

A working agent (it, or one of its sub-agents, has a waiting or running turn) cannot be
archived. The work is **safe** if the agent has no worktree of its own (planner, reviewer,
shared worktree), or every commit of its branch and of the HEAD checked out in the worktree
(detached HEAD or another branch) is contained in the project's base branch (not the case
after a squash merge), and the worktree has no uncommitted changes (or the worktree no longer
exists). **Automatic archiving** (by default after 24 idle hours; settable globally and per
project, also in the **Models and effort** window) archives only idle agents whose work is
safe, and only if their parent has already received their result and no other agent is working
in their worktree; never a lead or the orchestrator, and not on a git error or timeout either
(one pass, git calls included, takes at most about 5 seconds). There is no daemon for it: the
listings (web poll, `orch ps`, `list_agents`) run it, at most every 5 minutes across processes.
Permanent deletion is only possible from the archive and only by the user; agents can only
archive through MCP.

### Keeping awake

While an agent's turn is running (queued and idle agents do not count), the machine does not
go to sleep on idle: on macOS `caffeinate -i -w <runner pid>`, on Linux `systemd-inhibit
--what=idle`; it ends together with the runner, after a crash too. It only prevents idle sleep
(the display may sleep); it does not protect against closing the laptop lid without an external
display or forced sleep (e.g. `pmset sleepnow`, Apple menu → Sleep), nor on Linux desktop
environments whose power management puts the machine to sleep on its own timer. To turn it off:
`orch config keep-awake off` or in the **Models and effort** window (default `on`).

### Decisions and desktop notifications

What waits for the owner's decision does not stay buried in the conversations: the
orchestrator and the leads record it with the `request_decision(question, options,
recommendation)` MCP tool (question at most 500 characters, at most 6 short options, one
recommendation), and meanwhile carry on with whatever does not depend on it; if it has become
moot, `withdraw_decision(decision_id)` closes it (only their own open decision). Open decisions
are visible in one place, across projects: in the header of the web interface the
question-mark button shows their number, and in the **Decisions** window (project, agent,
question, options, recommendation, age) you can answer with an option or free text. From the
command line:

```bash
orch decisions            # the open decisions (--all: the closed ones too)
orch decide 12 2          # option 2; or free text: orch decide 12 "Postgres instead"
```

The answer reaches the requesting agent as an ordinary user message (`Decision #12 —
<question>` and `Answer: <answer>`), and the decision is closed; a closed decision takes no
further answer. The message brings back an archived agent; if the agent was deleted, the
decision closes without an answer (`note`).

Desktop notification (macOS only, `osascript`; nothing elsewhere): when a decision is waiting,
and when an agent you wrote to directly (its latest message was sent by the user) has finished:
its turn ended, and neither it nor its descendants have a running or queued turn. So Maestro
signals when it has finished too, after its agents; a worker started by a lead never does. To
turn it off: `orch config desktop-notifications off` or in the **Models and effort** window
(default `on`).

### Task board

Ideas and collected tasks live on a persistent board per project, not only in the
orchestrator's conversation: they survive into a new session. Every item belongs to a
registered project and is in one of four states: `idea`, `ready`, `in_progress`, `done`; items
are ordered within a state (a new item and an item that changes state go to the end). An item
can name the agent and the branch working on it. The orchestrator and the leads use it with the
`list_board(project, state)`, `add_board_item(project, title, description, state)` and
`update_board_item(item_id, state, title, description, agent, branch)` MCP tools (a lead only
its own project's). In the web interface the board button in the project's row opens it: one
list per state, new item, editing, moving up/down, changing state. From the command line:

```bash
orch board -p demo [--state ready]               # the board (without -p: every project's)
orch board add -p demo "Login page" -d "With OAuth" [--state idea]
orch board edit 7 --state ready [--title …] [--description …] [--agent …] [--branch …]
orch board move 7 up                              # or down, within its state
orch board dispatch -p demo                      # the ready items to the lead
```

**Dispatch** (in the interface after a confirmation) sends all of the project's `ready` items
to the project's lead in **one** message, as if you had written it (per item its id, title and
description, and the request to keep the states up to date with `update_board_item`); the items
move to `in_progress`, linked to the lead, so another dispatch does not send them again. If
there is no `ready` item, nothing is sent. A dispatch never starts on its own.

### Model and effort per tier

Three tiers can be set separately, per provider (Claude / Codex): the **orchestrator**, the
**project lead** and the **workers** (every other role: worker, planner, reviewer, tester).
There is a global default, which can be overridden per project.

```sh
orch config set orchestrator --model opus --effort high
orch config set lead --model sonnet --effort medium
orch config set worker --model haiku --effort low
orch config set worker --provider codex --model gpt-6-luna
orch config set worker --model sonnet -p valami      # only in the `valami` project
orch config show [-p valami]
```

Precedence for a turn: the value given explicitly for the agent (`orch spawn --model/--effort`,
the form's fields) → project setting → global setting → the role's `model` field → the CLI's
own default. The setting takes effect when the turn starts, so it also affects the next message
of agents that already exist.

An existing agent's own value (in the interface header or from the command line) applies from
the next turn:

```sh
orch model login-ab12                       # effective, own and inherited value
orch model login-ab12 --model opus --effort high
orch model login-ab12 --model ""            # clears it: the tier setting applies again
orch model orchestrator --effort high       # the orchestrator too (created on first use)
```

The model can be any name, but a single word: a name with a space, a control character, a
leading `-` or longer than 200 characters is not accepted (`orch spawn`, `orch config set` and
the MCP `spawn_agent` all check this; values stored earlier can still be read). Web API: `POST
/api/agents/<id>/model` `{"model": "...", "effort": null}` (`null` clears, a missing key does
not change).

`orch chat` starts with the global Claude setting of the **orchestrator** tier; `--model` and
`--effort` override it.

### Token and cost tracking

Every run stores its own tokens (input, output, cache read, cache write), its cost and the
model the CLI actually used (`run_usage` table). Codex does not report a cost. In a resumed
session the CLIs partly report a number totalled over the whole session (Claude
`total_cost_usd`/`modelUsage`, Codex `turn.completed`); in that case the sum of the previous
runs is subtracted and a per-run value is stored. `orch ps`/`orch status`, the `list_agents`
and `get_agent` MCP tools (the latter with the data of the last 10 runs) and the web interface
show them. The `model`/`effort` field shows the value that applies to the next turn; the value
given explicitly for the agent appears in the detailed view (`get_agent`, `orch status`) in the
`model_override`/`effort_override` field. The lists (`list_agents`, `wait_for_agents`) give per
agent only the `cost_usd` sum and a short `tokens` object (`in`, `out`, `cache_read`,
`cache_write`).

### Context and compaction (compact)

Every run also stores how large the conversation was at the end of the turn and how large the
model's window is (`run_usage.context_tokens`/`context_window`). Claude: the prompt of the
turn's last main (not subagent, not synthetic) request (input + cache read + cache write); the
window is the model's `contextWindow` value (currently 1M for every Claude model);
`result.usage` is no good for this, because it adds up all the requests of the turn. Codex: the
last `token_count` event of the thread's rollout file
(`$CODEX_HOME/sessions/…/rollout-…-<thread>.jsonl`) (read only; with a missing file or an
unknown format there is no data). `orch ps`/`orch status`, `list_agents`/`get_agent` (`context`
field) and the web interface (header: bar and `780k/1M 78%` and the automatic compaction
threshold; agent list: `ctx 78%`) show it; above the threshold (if automatic compaction is
off: from 80%) in a warning colour. After a compaction, until there has been another request,
the size is unknown: `compacted` is shown then.

```sh
orch compact <agent> ["what the summary should keep"]
```

The same in the web interface is the **Compact** button (with optional instructions), API:
`POST /api/agents/<id>/compact` `{"instructions": …}`. It is queued as a message (a run of kind
`compact`) and runs the CLI's own compaction in the same session: with Claude it passes the
`/compact [instructions]` command exactly as is (without `{{result:…}}` substitution and output
schema, without `--disable-slash-commands`, otherwise with the agent's usual loadout). Its
result is a short summary (`compacted 91k -> 1k tokens`); it is not delivered to the parent
(neither a notification nor `wait_for_agents`), wakes nobody, and is not the agent's answer:
the agent's status, its `last_run` and the `{{result:<agent>}}` reference show its last real
turn; the compaction's output is shown separately (the `compaction` line of `orch status`,
`get_agent` `last_compaction`, in the interface the indication of a failed compaction). A
queued compaction can be withdrawn (`orch withdraw <run>`), but not edited. Compaction can lose
details from the earlier part of the conversation. With Codex `/compact` does not work in
`exec resume`: instead a turn runs with a lower `model_auto_compact_token_limit` value (half the
current size) and an explicit "nothing to do" prompt; no instructions can be given, and it can
only be started when the size is known. Claude Code also compacts on its own (even mid-turn)
when the conversation reaches the window − 33k tokens (967k with a 1M window); the run's `info`
event and the `compact_*` fields record this.

For a lead role (`lead: true`: the orchestrator and the project leads) a compaction without
instructions (the automatic one and `orch compact <lead>` too) gets short default instructions
(`service.LEAD_KEEP`): keep the owner's standing rules, the current state, the open decisions
and open items, and the work in progress with agent ids and branches. The other roles get the
CLI's default summary. Codex takes no instructions: there it runs without them.

#### Automatic compaction

```sh
orch config compact-at 256k [-p valami]   # default 256000; 0 or off: never; "" clears it (inherit)
```

If an agent's turn ends successfully and its context is above the threshold, the runner queues
a compaction before closing the turn (the prompt cache is still warm then), so it runs before
the agent's next turn, never mid-turn. It also goes ahead of a message that is already queued
(which would otherwise pay for the whole context); the message then runs normally afterwards,
even if the compaction fails, is withdrawn or is stopped. No compaction is queued after a
compaction, after a failed or stopped turn, if a compaction is already queued or running, or if
the agent cannot be compacted (then it is silently skipped). The log's `info` event signals it
(`automatic compaction: context 301k is above the threshold of 256k`), and the
`last_compaction` field of `get_agent`/`orch status` has `automatic: true`. Until then the
agent counts as working: the parent's notification and `wait_for_agents` also wait for the
compaction to end, and give the child's real answer, not its compaction.

Not twice in a row: how much context the last compaction (one that started; one that never
started, was stopped or withdrawn does not count, just like the CLI's own mid-turn compaction)
left is decided by the actually measured size, not by the CLI's post-compaction number (Claude's
`post_tokens` value does not include the system prompt and the tools, Codex reports nothing of
the kind): if the compacting turn itself measured a size afterwards, that one; otherwise the size
of the next turn's first model request (`run_usage.first_context_tokens`, Claude), or where
that does not exist (Codex), conservatively the size measured at the end of that turn. If this
is above the threshold, automatic compaction is paused for the agent: the measuring turn writes
an `info` event once (`automatic compaction paused: …`), and the `context.compact_paused` field,
`orch status` and the interface's context indicator show it. A compaction after which the
measured size is below the threshold, or a threshold higher than the measured size, lifts it.
As long as nothing has measured a size since the last compaction, no automatic compaction is
queued. If stopping the turn (`orch stop`) arrives while or after the compaction is being
queued, the turn becomes `stopped` and withdraws the queued compaction.

The threshold can be set globally and per project (the project overrides), also in the
interface's settings window; the value takes effect at the end of the next turn. With Codex it
works the same way as manual compaction (only when the size is known, with the "nothing to do"
turn); if the model does not compact, the compaction fails and automatic compaction is paused.

### What an agent loads (loadout)

Without a loadout every agent would inherit the user's whole global Claude Code / Codex
configuration (claude.ai connectors, plugins, skills, MCP servers): this was about 25–31k
input tokens per request, of which the role and the task were only about 1.6k. That is why
every role declares what it loads:

| key | meaning | CLI |
|---|---|---|
| `tools` | built-in tools, comma-separated; `default` = the CLI's default set, `none` = none at all (does not affect MCP tools) | `--tools` |
| `mcp` | `strict` = only the orchestrator's own server (for lead roles); `inherit` = user, project, plugin and claude.ai connector servers too | `--strict-mcp-config` |
| `setting_sources` | `project,local` = without the user level (user plugins, skills, commands, hooks); the project's `CLAUDE.md` and `.claude/` folder are loaded; `user,project,local` = everything | `--setting-sources` |
| `slash_commands` | `false` = no skills and slash commands at all (not the project's either) | `--disable-slash-commands` |
| `codex_user_config` | `false` = Codex does not read `$CODEX_HOME/config.toml` (the login stays) | `--ignore-user-config` |
| `memory` | `true` = `Write` and `Edit` too, but only into the session's own memory folder (`~/.claude/projects/<project>/memory/`); a hook refuses every other target. No effect if `tools` already grants a writing tool (`default` does too), in `orch chat` and with Codex | `--settings` (PreToolUse hook) |

Built-in defaults: every role has `mcp = strict`, `setting_sources = project,local`,
`slash_commands = false`, `codex_user_config = false`; the tools:

| role | `tools` |
|---|---|
| orchestrator, lead | `Bash,Read,Glob,Grep` (+ the `orch` MCP tools), `memory = true` |
| worker, tester | `Bash,Read,Edit,Write,Glob,Grep` |
| reviewer | `Bash,Read,Glob,Grep` |
| planner | `Bash,Read,Glob,Grep,WebSearch,WebFetch` |

`memory` keeps Claude Code's memory (one file per fact and the `MEMORY.md` index) alive for
the orchestrator and the leads: a lead's memory is the project's own, shared with the owner's
interactive sessions running there. The restriction applies only to the `Write`/`Edit` tools,
not to `Bash`. It only takes effect if the session's hooks are proven to run: the runner sends
the prompt only after the marker file created by the hook; if hooks are turned off
(`disableAllHooks`, policy `allowManagedHooksOnly`), the turn restarts without `Write`/`Edit`.
The agent remembers this (`~/.orchestrator/hooks-off/<agent>.json`): the following turns start
that way without waiting; it tries again in a new session or after a day; an `info` event
signals the change of state. If the CLI exits before the marker file (e.g. an expired login),
the turn fails with its own error, without a restart. That is why `orch chat` does not get it.
If the location of the memory folder cannot be determined reliably (a symlinked `.git`,
environment variables that move the folder), every write is refused; with
`autoMemoryDirectory` only the default folder is writable, not the moved one.

The built-in roles have no `Skill` tool, so skills and slash commands are turned off. To use
project skills/commands, the role in question needs `slash_commands = true` and `Skill` among
its `tools`. (The `scripts/loadout-after.json` measurement was still made with
`slash_commands = true`; the difference is about 30 tokens.)

A custom role file that sets none of the keys: `mcp = strict`,
`setting_sources = project,local`, `tools = default` (including `Skill`),
`slash_commands = true`, `codex_user_config = false`. An empty value (`tools:`) is an error.

Overriding without copying the role file: `<project>/.orch/loadout.toml` and
`~/.orchestrator/loadout.toml`, with `[default]` (every role) and `[<role>]` tables, with the
same keys. Per key the first hit wins: project `[role]` → project `[default]` → home `[role]` →
home `[default]` → the role's frontmatter → the default above. With a faulty file or value the
run fails, naming the file and the error; a table with an unknown role name (e.g. a misspelled
`[wroker]`) is ignored and reported as an `info` event.

**Who is affected, and what to do.** Dropping the `user` source leaves out the whole
`~/.claude/settings.json`, not just the plugins: the `env` block (e.g. Bedrock/Vertex/proxy
routing), `apiKeyHelper`, `model`, the permissions and the hooks too. Anyone who authenticates
this way will find their agents cannot reach the model after the update; anyone who has `model`
there and no orch tier setting silently gets the CLI's default model. With Codex,
`--ignore-user-config` also leaves out the `model`, `model_provider` and profile settings of
`config.toml` (the login stays). In that case, put this in `~/.orchestrator/loadout.toml`:

```toml
[default]
setting_sources = "user,project,local"   # Claude: ~/.claude/settings.json applies again
codex_user_config = true                 # Codex: ~/.codex/config.toml applies again
```

(The model can also be given with the tier setting of `orch config set`.) A project that relies
on user-level plugins, skills and plugin MCP servers turns all three layers back on. `tools` is
given per role, so `Skill` has to be added separately to every role that needs it:

```toml
# <project>/.orch/loadout.toml
[default]
setting_sources = "user,project,local"   # user plugins, skills, commands
mcp = "inherit"                          # plugin, user and connector MCP servers
slash_commands = true

[worker]
tools = "Bash,Read,Edit,Write,Glob,Grep,Skill"

[tester]
tools = "Bash,Read,Edit,Write,Glob,Grep,Skill"

[lead]
tools = "Bash,Read,Glob,Grep,Skill"
```

Every turn gets the flags, the `--resume`/`exec resume` turns too. `orch chat` uses the
`orchestrator` role's loadout (only the home-level `loadout.toml` affects it), and `Bash`
always stays in for the `orch` commands; it never turns off slash commands, though
(`--disable-slash-commands` would also take away the interactive `/compact`, `/clear`, `/model`,
`/context` commands); `slash_commands` only affects the headless agents. Codex does not know the
`tools`/`mcp`/`setting_sources`/`slash_commands` keys; with `codex_user_config = false` the user
configuration's model, MCP servers and plugins are not loaded either (the model is Codex's own
default if none is set), and the lead roles' `orch` tools run without approval.

**Note:** a session started before the change carries the old lists (tools, skills, MCP
instructions) in its conversation history, so turns continued there stay large; the savings
show in full only for new agents.

### Dependent tasks

- `after` / `--after`: the agent starts only after the given agents (or runs) have finished
  successfully; if a dependency fails, it becomes `skipped`.
- `{{result:<agent-id>}}` in the task text: at start it is replaced with the other agent's
  final answer (e.g. the planner's plan into the worker's brief). A structured answer goes in
  whole, as compact JSON (truncated JSON is of no use); a text answer is cut at 4000
  characters, followed by a line with the full length and the path of the result file.
  `{{result:<agent-id>:full}}` gives the full text.
- `base=<agent-id>`: the worktree branches off the other agent's branch.
- `share_worktree_with` / `--share`: works in the same worktree (review, test, fix).
- `output_schema` (MCP): a structured answer following a JSON Schema (e.g. a task list from the
  planner). If the caller gives no schema, the MCP `spawn_agent` and `send_message` use the
  role's default schema (worker, reviewer, tester, planner: compact, fixed fields; lead and
  orchestrator: free text); `freeform: true` asks for a prose answer; an own `output_schema`
  always wins. The CLI (`orch spawn/send/ask`) and the web API get free text.
- Messages sent to an agent run one after another; the agent keeps its conversation
  (`claude --resume`, `codex exec resume`).

### Notifications

The orchestrator and the leads do not wait for their children: they hand out the work and end
the turn with a short status, so they can receive messages meanwhile (the user's message does
not queue behind a waiting turn). When a child agent and all its sub-agents are idle, the parent
gets an automatic `[orch notification]` turn: a short header (a system event, not a message from
the user or the parent, approves nothing), then compact JSON per agent with the answer of its
last finished run (up to 4000 characters per agent, 10000 in total; the rest can be read with
`get_agent`) and the list of children still running.

- Only the runs sent by the parent (MCP `spawn_agent`, `send_message`, `ask_project_lead`) and
  those notification turns of the child that report on work done for the parent (which the
  child started while on the parent's assignment) are included; work requested from the CLI or
  the web and old runs are not.
- `notify: "each"` (default): reports as soon as this agent is done; `notify: "all"`: only when
  every agent the parent sent work to is done. The first field of the answer, `next`, tells
  whether the caller will be woken up (the interactive `orch chat` session never is: use
  `wait_for_agents` there).
- The notification text is written when the turn starts, after a short wait
  (`ORCH_NOTIFY_DELAY`, 5 s by default), so results arriving together end up in one turn; if by
  then there is nothing to report, or it never starts (stop, dead runner), the run is deleted
  without a trace. If the notification turn fails, its results can be delivered again: they come
  with the next notification (there is no automatic retry), until then `wait_for_agents` gives
  them.
- A notification is triggered only by the end of a run, never by waiting (`orch wait`,
  `wait_for_agents`); a stopped agent therefore stays silent until its next real finish.
- Every result reaches the parent once: what `wait_for_agents` has already returned does not
  come in a notification, and a later `wait_for_agents` marks a result received in a
  notification as `already_delivered`. `wait_for_agents` (120 s by default) also waits for the
  sub-agents, and returns immediately if a message has arrived for the caller.
- An agent runs one turn at a time; the order of messages is kept, and a waiting notification
  comes after the messages ready to run. The detailed view of `get_agent` shows the waiting runs
  in the `queue` field, every view the number of running descendants in the `children_running`
  field.

## Architecture

```
src/orchestrator/
  models.py        Project, Agent, Run (one turn: prompt → answer), Event
  store.py         SQLite (WAL) – the only shared state
  git.py           worktree lifecycle
  roles.py         loading roles; roles/*.md are the built-in roles
  settings.py      model/effort per tier: global + project override
  keep_awake.py    sleep inhibitor while turns run (caffeinate / systemd-inhibit)
  board.py         the projects' task board (list_board, orch board, board window, dispatch)
  decisions.py     decisions requested from the owner (request_decision, orch decide, Decisions window)
  desktop.py       desktop notification on macOS (osascript)
  loadout.py       what an agent loads (tools, MCP, setting sources); loadout.toml
  memory_guard.py  PreToolUse hook: Write/Edit of the `memory` key only write into the memory folder
  providers/       base.py: Provider protocol · claude.py · codex.py
  runner.py        executing one run in its own detached process
  service.py       Orchestrator facade – used by the CLI, the MCP server and the web
  mcp_server.py    the tools of the orchestrator and the lead agents
  cli.py           `orch`
  web/             web interface: one page + JSON API on top of the facade
```

**No daemon.** Every run is its own `python -m orchestrator.runner <id>` process: it waits for
its dependencies, creates the worktree if needed, starts the CLI in headless mode
(`claude -p --output-format stream-json`, or `codex exec --json`), and turns the output into
uniform events. The state lives under `~/.orchestrator/` (movable with `ORCH_HOME`): `orch.db`,
`logs/run-<id>/` (raw stream, stderr, `result.md` with the full final answer – for a failed run
the last text or the error –, `result.json` for a structured answer),
`worktrees/<project>/<agent>`. The path of `result.md` is `result_file` in the detailed view
(`get_agent`); it is not in the lists. A stopped run and a crashed runner do not write one.

**Permissions.** Claude agents run with `--permission-mode auto` by default, Codex agents in the
`workspace-write` sandbox; this can be overridden per agent (`orch spawn --permission ...`).

### Extension points

- **New agent type:** implement the `Provider` protocol (`providers/base.py`) and register it
  in `providers/__init__.py`, or from outside:
  `ORCH_PROVIDERS="name=package.module:Class"`.
- **New role:** a markdown file with frontmatter (`description`, `lead`, `worktree`,
  `provider`, `model`, `output_schema` and the loadout keys) under `<project>/.orch/roles/` or
  `~/.orchestrator/roles/`; it overrides the built-in ones by name. A `lead: true` role gets the
  MCP tools. `output_schema: <file>` is the role's default schema (a JSON Schema file name, not
  a path); the file is looked up in the same order as the roles (`<project>/.orch/roles/`,
  `~/.orchestrator/roles/`, built-in), so a project can override a built-in schema too. `none`
  or leaving it out: free text. The schema must be strict for both Claude's `--json-schema` and
  Codex's `--output-schema`: every field in `required`, `additionalProperties: false`, an
  optional value nullable or an empty list.
- **Pipelines (planning → breakdown → test cases → implementation → review → test):** the
  building blocks exist (`after`, `{{result:…}}`, `base`, shared worktree, `output_schema`); a
  declarative pipeline layer can be built on top of `service.py`.

## Development

```sh
uv run pytest          # the tests run with a fake provider, with real git worktrees
uv run ruff check . && uv run ruff format .
```
