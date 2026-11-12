"""Filesystem layout. Everything the orchestrator owns lives under ORCH_HOME."""

from __future__ import annotations

import os
from pathlib import Path

# Display names only: ids, the role and the package keep "orchestrator".
PROJECT_NAME = "Spartito"  # the tool itself (Italian: sheet music, the score everyone plays from)
ORCHESTRATOR_NAME = "Maestro"  # the top-level orchestrator agent
# The command (pyproject's [project.scripts]): every text that tells someone to run one names it
# through this. Processes and the definitions written to disk never call it: they run
# `<python> -m orchestrator`. The internal names keep "orch": the MCP server `orch` and its
# `mcp__orch__` tools, the `[orch notification]` marker, ORCH_* variables, ~/.orchestrator and
# orch.db, a project's .orch/ folder and the agents' `orch/` branches.
CLI_NAME = "spartito"
# The home of the instance whose agent's turn this process descends from: the runner gives it
# to the agent's CLI and MCP server, and everything they start inherits it. The outermost one
# is kept, so a Spartito started from an agent's shell on another home (a test run in a
# temporary ORCH_HOME), and all it starts, knows it is nested (desktop.py: shows nothing).
TURN_HOME_ENV = "ORCH_TURN_HOME"
# "off" (or 0, false, no): no desktop notification by any path (CI, manual runs of a checkout);
# notices are still recorded and traced. The setting `desktop_notifications` records none.
NOTIFICATIONS_ENV = "ORCH_DESKTOP_NOTIFICATIONS"


def home() -> Path:
    path = Path(os.environ.get("ORCH_HOME", "~/.orchestrator")).expanduser()
    path.mkdir(parents=True, exist_ok=True)
    return path


def turn_home() -> str:
    """TURN_HOME_ENV for a turn started here: the inherited one, else this home (resolved)."""
    return os.environ.get(TURN_HOME_ENV) or str(home().resolve())


def turn_env() -> dict[str, str]:
    """What a child of a turn must get even when it is given an environment of its own (an
    MCP server under Codex): TURN_HOME_ENV, and NOTIFICATIONS_ENV when it is set here."""
    env = {TURN_HOME_ENV: turn_home()}
    if switch := os.environ.get(NOTIFICATIONS_ENV):
        env[NOTIFICATIONS_ENV] = switch
    return env


def db_path() -> Path:
    return home() / "orch.db"


def logs_dir() -> Path:
    path = home() / "logs"
    path.mkdir(exist_ok=True)
    return path


def runtime_dir() -> Path:
    """Files about live processes (markers, locks), not about any one run."""
    path = home() / "run"
    path.mkdir(exist_ok=True)
    return path


def run_dir(run_id: int) -> Path:
    """Per-run files: the CLI's raw output stream, stderr, schemas, the final answer."""
    return logs_dir() / f"run-{run_id}"


def result_file(run_id: int) -> Path:
    """The run's full final answer (a failed run: its last text, else the error), written
    by the runner when the run ends; stopped runs and runner crashes leave none."""
    return run_dir(run_id) / "result.md"


def worktrees_dir(project: str) -> Path:
    return home() / "worktrees" / project


def workspace_dir() -> Path:
    """Working directory of the top-level orchestrator session."""
    path = home() / "workspace"
    path.mkdir(exist_ok=True)
    return path


def user_roles_dir() -> Path:
    return home() / "roles"
