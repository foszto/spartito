---
name: check
description: Runs the project's full check set (tests, ruff, dashboard scripts) and reads the result. Use before committing a change or when asked whether the branch is green.
---

Run each command from the repository root, with a temporary `ORCH_HOME`, so that nothing
touches the real `~/.orchestrator`:

```sh
export ORCH_HOME="$(mktemp -d)"
uv run pytest -n auto
uv run ruff check
uv run ruff format --check
for f in src/orchestrator/web/static/*.js; do node --check "$f"; done
rm -rf "$ORCH_HOME"
```

Then read the result:

1. pytest ends with `N passed, M skipped`. The usual skips are platform-bound. On macOS:
   `systemd only on Linux` (2) and, without the `app` extra, `could not import
   'UserNotifications'` (1). On Linux: `runners decide by the platform` (3),
   `could not import 'UserNotifications'` (1), and `no systemd-analyze` (2) where it is
   missing. As root, also `running as a user who can read anything` (1). Any other skip or
   failure needs a look. Rerun a single failing test with `uv run pytest tests/<file>.py -k <name>`.
2. `ruff check` must say `All checks passed!`. For `ruff format --check` failures, run
   `uv run ruff format` and review the diff.
3. `node --check` prints nothing for a valid script.
4. For a change to docs only, `uv run pytest -n auto tests/test_docs_links.py tests/test_command_name.py`
   is the fast subset. The whole suite is still the final check.

Report the counts and every failure as it appears, without rewording it.
