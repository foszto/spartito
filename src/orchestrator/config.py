"""Filesystem layout. Everything the orchestrator owns lives under ORCH_HOME."""

from __future__ import annotations

import os
from pathlib import Path


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


def worktrees_dir(project: str) -> Path:
    return home() / "worktrees" / project


def workspace_dir() -> Path:
    """Working directory of the top-level orchestrator session."""
    path = home() / "workspace"
    path.mkdir(exist_ok=True)
    return path


def user_roles_dir() -> Path:
    return home() / "roles"
