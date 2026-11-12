# Contributing to Spartito

Bug reports, fixes, documentation and features are welcome. For a larger feature, open an
issue first to agree on the direction. Questions go to the repository's issues as well. New
to the project? Read the [README](README.md), [docs/concepts.md](docs/concepts.md) and
[docs/architecture.md](docs/architecture.md).

## Setup

You need Python 3.14, [uv](https://docs.astral.sh/uv/), `git` and, for the dashboard
scripts, `node`. Fork the repository on GitHub, then:

```sh
git clone <your fork's URL> spartito
cd spartito
uv sync
```

`uv sync` is all the test suite needs. On macOS, `uv sync --extra app --extra menubar` adds the
optional desktop packages, and with them one more test runs. On Linux the `app` extra
installs PyQt6 and PyQt6-WebEngine, which are GPL-3.0-only, plus about 200 MB of Qt
libraries, and no test needs them. They are optional dependencies, not part of this
repository ([docs/desktop.md](docs/desktop.md#optional-dependencies)).

**Run the tool from the checkout, never against your real state.** Spartito keeps its state
in `~/.orchestrator/` unless `ORCH_HOME` says otherwise. If you develop against that
directory, you can damage your own projects, agents and database. Point `ORCH_HOME` at a
temporary directory in every shell you develop in, and switch desktop notifications off
there (`ORCH_DESKTOP_NOTIFICATIONS=off`: notices are recorded, nothing is shown), or an agent
you start there notifies on your desktop when it finishes:

```sh
export ORCH_HOME="$(mktemp -d)"
export ORCH_DESKTOP_NOTIFICATIONS=off
uv run spartito --help
```

After `uv sync`, `uv run spartito` runs the checkout's own script (`.venv/bin/spartito`), which
imports the package from `src/`. To check this, run `uv run which spartito`. If it shows
another path, or the command is missing, run `uv sync` again, or use
`uv run python -m orchestrator …`, which always runs the checkout's code. Do not develop
with an installed `spartito` from your `PATH`. Be careful with `spartito service install`
and `spartito app install`: a temporary home gets a service of its own, but the launcher
always goes to the same place (`~/Applications/Spartito.app`, or `spartito.desktop` on
Linux) and replaces an existing one ([docs/desktop.md](docs/desktop.md)).

## Checks

Every change must pass:

```sh
uv run pytest -n auto
uv run ruff check
uv run ruff format --check
for f in src/orchestrator/web/static/*.js; do node --check "$f"; done
```

GitHub Actions runs them on every push to `master` and every pull request
(`.github/workflows/ci.yml`): the suite on Linux and on macOS (there with the extras), and the
linters, the scripts and the link and command-name tests once on Linux.

The suite runs on a fake provider (`tests/fakeprovider.py`) with real git repositories and
worktrees in temporary directories. It never calls `claude` or `codex` and needs no network.
`tests/conftest.py` sets a temporary `ORCH_HOME` for every test. In parallel
(`pytest-xdist`) it takes about one minute on a recent Mac without the extras (1215 passed,
3 skipped). The skips:

- `tests/test_autostart.py`, two cases: skipped on macOS (`systemd only on Linux`), and on
  Linux without `systemd-analyze`;
- `tests/test_notifier.py`, one case: skipped on Linux, and on macOS without the `app` extra
  (`could not import 'UserNotifications'`);
- `tests/test_decisions.py`, three tests: skipped on Linux (`runners decide by the platform`);
- `tests/test_loadout.py`, one case: skipped as a user who can read every file (root).

Three tests guard the rules below:

- `tests/test_docs_links.py`: every relative link in `README.md` and `docs/*.md` points to a
  file that exists, and every `#anchor` matches a heading there.
- `tests/test_architecture.py`: the interfaces (`cli`, `mcp_server`, `web`, `gui`) use only
  the facade in `service.py` and never import the store. Every module imports only from its
  own layer or a lower one (interfaces > runner > core > domain > infra), without cycles; the
  list of exceptions may only shrink.
- `tests/test_command_name.py`: nothing the tool starts or writes calls the command by name
  (it runs `<python> -m orchestrator …`), and no text tells anyone to run the command under
  its old name ([Upgrading](docs/usage.md#upgrading-from-orch) is the one exception).

## Code conventions

- **Layout and layers**: [docs/architecture.md](docs/architecture.md#code-layout). New
  behaviour goes into the module of its area; the interfaces reach it through the
  `Orchestrator` facade.
- **No daemon.** Every turn is its own detached runner process, and all shared state is in
  one SQLite database (`store.py`). Nothing may depend on a long-running process.
- **Schema changes are additive.** New tables use `CREATE TABLE IF NOT EXISTS`, and new
  columns are added with `ALTER TABLE … ADD COLUMN` when the store opens, so that an existing
  database keeps working, with older code too. There are no migration scripts.
- **Comments and docstrings.** Every module starts with a docstring that says what it is
  for. Docstrings and comments are plain English sentences that explain what happens and
  why, including edge cases, races and platform differences. They do not repeat the
  signature. Code is typed, and modules use `from __future__ import annotations`. Ruff sets
  the style (line length 100).
- **Tests come with behaviour changes.** A change in behaviour comes with a test in the
  matching `tests/test_<area>.py`. Tests make no network calls and do not call real
  providers, browsers, notification centres or service managers. They use the fake provider
  and the fakes in `tests/conftest.py` and the test modules instead.

## Documentation conventions

- English, short and concrete. Details go in `docs/`; the README is a front page that links.
- Every command and option shown must exist: check with `uv run spartito <command> --help`.
- Relative links are checked by `tests/test_docs_links.py`.

## Privacy in a public repository

Code, tests, fixtures, docs and commit messages must not contain real names, company or
customer names, email addresses, local user paths, hostnames, credentials, or recorded
session data (agent conversations, logs, databases). Use the placeholders the docs already
use, for example the project `my-app` at `~/src/my-app` (`/home/dev/src/my-app`) and the
agent `fix-login-3f2a`.

## Git and pull requests

Every change reaches `master` through a pull request from a branch, the maintainers' own
changes included. Its CI run must be green before it is merged. Nobody pushes to `master`
directly, and the branch of a pull request is never force-pushed.

- Branch from `master`; keep each pull request small and focused on one change.
- The description says what changed, why, and how you tested it.
- Commit subjects follow the history: a short lowercase area, a colon, then what is now true,
  for example `docs: the task board` or `spent total: keep what agents deleted for good had
  spent`. The body, if any, explains the change in prose.
- Maintainers squash or merge as they see fit. Review happens when time allows; there is no
  promised response time.

## Releases

Maintainers publish to PyPI from a version tag, through GitHub Actions:
[docs/releasing.md](docs/releasing.md).

## Licence

Spartito is under the MIT licence ([LICENSE](LICENSE)). By contributing, you agree that your
contribution is licensed under the same terms. There is no contributor licence agreement.

## Reporting bugs

Open an issue with:

- the commit you run (`git rev-parse --short HEAD` in your checkout) or the version in
  `pyproject.toml`;
- the platform and its version (macOS or the Linux distribution) and the Python version;
- the provider CLI versions (`claude --version`, `codex --version`);
- the steps that reproduce it, and what you expected instead;
- the relevant log excerpt, with private data removed. Each turn logs to
  `$ORCH_HOME/logs/run-<id>/` (default `~/.orchestrator/logs/`): the CLI's output, stderr,
  `result.md`. The service logs to `logs/web.log` on macOS and to the journal on Linux (see
  `spartito service logs`); the window of `spartito app` to `logs/app.log`.
