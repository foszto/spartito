"""Filesystem layout. Everything the orchestrator owns lives under ORCH_HOME."""

from __future__ import annotations

import os
from pathlib import Path

# Display names only: ids, the role, the `orch` command and the package keep "orchestrator".
PROJECT_NAME = "Spartito"  # the tool itself (Italian: sheet music, the score everyone plays from)
ORCHESTRATOR_NAME = "Maestro"  # the top-level orchestrator agent


def home() -> Path:
    path = Path(os.environ.get("ORCH_HOME", "~/.orchestrator")).expanduser()
    path.mkdir(parents=True, exist_ok=True)
    return path


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
