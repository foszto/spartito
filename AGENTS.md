# Spartito: notes for coding agents

Spartito runs Claude Code and Codex agents headless and in parallel, each in its own git
worktree, steered through one orchestrator agent from a web dashboard or the command line.
The command is `spartito`, and the Python package is `orchestrator` (Python 3.14, uv).

## Where things are

- `src/orchestrator/`: `cli.py` (the command), `mcp_server.py`, `web/` (dashboard), `gui/`
  (`spartito app`, menu bar), `service.py` (the `Orchestrator` facade), `core/` (operations
  behind it), `runner.py` (one turn per process), `store.py` (SQLite schema and queries),
  `providers/` (Claude, Codex), `roles/` (packaged role prompts).
- Layers: interfaces > runner > core > domain > infra. The interfaces use only the facade
  and never the store. Details: `docs/architecture.md`.
- `tests/`: one `test_<area>.py` per area; `tests/fakeprovider.py` stands in for the CLIs;
  `tests/conftest.py` gives every test a temporary `ORCH_HOME`.

## Checks

```sh
uv run pytest -n auto
uv run ruff check
uv run ruff format --check
for f in src/orchestrator/web/static/*.js; do node --check "$f"; done
```

## Rules

- Schema changes are additive only. Add tables with `CREATE TABLE IF NOT EXISTS` in
  `store.py`'s `SCHEMA`, and columns through the `*_ADDED` lists that `Store` adds with
  `ALTER TABLE … ADD COLUMN`. Older databases and older code must keep working
  (`tests/test_store.py`).
- A change users notice adds a line under `## [Unreleased]` in `CHANGELOG.md` (the versioning
  rule: `docs/releasing.md#the-version`).
- `tests/test_architecture.py` enforces the layer rule. Its list of exceptions may only
  shrink.
- Texts that tell someone to run the command name it through `config.CLI_NAME`: `CLI` in
  `cli.py`, and `{cli}` in help docstrings. `tests/test_command_name.py` guards this. The
  internal names stay as they are: the `orch` MCP server and its `mcp__orch__` tools,
  `ORCH_*`, `~/.orchestrator`, `orch.db` and the `orch/` branches.
- Docs are in English, and every command and option they show must exist.
  `tests/test_docs_links.py` checks the links of `README.md` and `docs/*.md`.
- This is a public repository. Code, tests, fixtures, docs and commit messages hold no real
  names, company names, email addresses, local user paths, hostnames, credentials or recorded
  session data. Use the placeholders of the docs (`my-app`, `~/src/my-app`, `fix-login-3f2a`).
- Never develop or test against the real `~/.orchestrator` or a running dashboard or
  service. Set a temporary `ORCH_HOME` for anything you run, and use a port other than 8787.
  A manual run in a temporary home must also set `ORCH_DESKTOP_NOTIFICATIONS=off`, so that no
  notice reaches the desktop (`docs/desktop.md`).
  The suite replaces an inherited `ORCH_HOME` with a temporary one and fails a test that
  reaches a real home (`tests/home_guard.py`); tests must not undo the suite's environment
  patches. Only children that inherit `os.environ` get the temporary home, and no child gets
  the refusal: a child given an environment of its own must be given `ORCH_HOME`.
  Run the checkout's code with `uv run spartito …` or `uv run python -m orchestrator …`, not
  an installed `spartito`.
- Nothing visible on the desktop while testing. Do not run `spartito app`, `spartito menubar`,
  `spartito service install|uninstall` or `spartito app install|uninstall` for real, and do
  not open a browser or post notifications. The tests fake all of these.

## Documents

- `CONTRIBUTING.md`: setup, checks, conventions, pull requests.
- `docs/architecture.md`: processes, state, code layout, layers, limitations.
- `docs/concepts.md`: orchestrator, leads, roles, worktrees, turns.
- `docs/usage.md`: every command and option, the dashboard.
- `docs/configuration.md`: model and effort, settings, loadout, roles, providers, files.
- `docs/agents.md`: MCP tools, dependencies, notifications, decisions, compaction.
- `docs/desktop.md`: background service, notifications, `spartito app`, launcher, menu bar.
- `docs/board.md`: the task board.
- `docs/state.md`: backup and restore of the state, moving a home.
- `docs/releasing.md`: the version, PyPI and the release workflow.
- `CHANGELOG.md`: what each release changed.
- `ROADMAP.md`: where the project is heading.
