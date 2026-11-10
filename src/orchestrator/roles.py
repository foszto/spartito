"""Roles: a markdown prompt plus a few defaults, in the spirit of Claude Code agents.

Lookup order, first hit wins:
  <project>/.orch/roles/<name>.md  ->  $ORCH_HOME/roles/<name>.md  ->  packaged defaults

Frontmatter keys (all optional):
  description  one line shown in listings
  lead         "true" gives the agent the orchestrator MCP tools so it can run sub-agents
  worktree     "false" makes the agent work in the project checkout by default
  provider     default provider for the role
  model        default model for the role
"""

from __future__ import annotations

from dataclasses import dataclass
from importlib import resources
from pathlib import Path

from . import config


@dataclass
class Role:
    name: str
    prompt: str
    description: str = ""
    lead: bool = False
    worktree: bool = True
    provider: str | None = None
    model: str | None = None


def _parse(name: str, text: str) -> Role:
    meta: dict[str, str] = {}
    body = text
    if text.startswith("---"):
        _, header, body = text.split("---", 2)
        for line in header.strip().splitlines():
            key, _, value = line.partition(":")
            meta[key.strip()] = value.strip()
    return Role(
        name=name,
        prompt=body.strip(),
        description=meta.get("description", ""),
        lead=meta.get("lead", "false").lower() == "true",
        worktree=meta.get("worktree", "true").lower() != "false",
        provider=meta.get("provider") or None,
        model=meta.get("model") or None,
    )


def _packaged() -> dict[str, str]:
    root = resources.files("orchestrator") / "roles"
    return {
        entry.name.removesuffix(".md"): entry.read_text(encoding="utf-8")
        for entry in root.iterdir()
        if entry.name.endswith(".md")
    }


def load(name: str, project_path: str | Path | None = None) -> Role:
    candidates = []
    if project_path:
        candidates.append(Path(project_path) / ".orch" / "roles" / f"{name}.md")
    candidates.append(config.user_roles_dir() / f"{name}.md")
    for path in candidates:
        if path.is_file():
            return _parse(name, path.read_text(encoding="utf-8"))
    packaged = _packaged()
    if name not in packaged:
        raise KeyError(f"unknown role '{name}' (available: {', '.join(sorted(packaged))})")
    return _parse(name, packaged[name])


def available(project_path: str | Path | None = None) -> list[Role]:
    names = set(_packaged())
    for directory in (
        config.user_roles_dir(),
        Path(project_path) / ".orch" / "roles" if project_path else None,
    ):
        if directory and directory.is_dir():
            names.update(path.stem for path in directory.glob("*.md"))
    return [load(name, project_path) for name in sorted(names)]
