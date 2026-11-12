---
name: cli-command
description: How to add or change a `spartito` command or option. Use when the command line changes.
---

1. Commands are Typer functions in `src/orchestrator/cli.py` (groups such as `app`,
   `service` and `board` are `typer.Typer` instances added with `app.add_typer`). A command
   calls the `Orchestrator` facade (`service.py`), never the store.
2. Texts that tell someone to run the command name it through `config.CLI_NAME`. In f-strings,
   use the module's `CLI`. Help docstrings cannot be f-strings: write `{cli}`, which
   `name_the_command` replaces. Never name a subcommand after the command's old name.
3. Look at the help page with a temporary home:

   ```sh
   export ORCH_HOME="$(mktemp -d)"
   uv run python -m orchestrator <command> --help
   rm -rf "$ORCH_HOME"
   ```

   Do not run commands that open windows, post notifications or install a service or
   launcher (`app`, `menubar`, `service install|uninstall`, `app install|uninstall`).
4. Tests: behaviour in the matching `tests/test_<area>.py` (with `typer.testing.CliRunner`,
   as in `tests/test_interfaces.py`). `tests/test_command_name.py` checks every help page
   automatically.
5. Docs: the command and its options in `docs/usage.md` (Commands at a glance, and the
   Command reference), plus the topic's own doc where there is one. The README only when the
   front-page path changes.
6. Run the `check` skill.
