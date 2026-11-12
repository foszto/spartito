"""Roles: a markdown prompt plus a few defaults, in the spirit of Claude Code agents.

Lookup order, first hit wins:
  <project>/.orch/roles/<name>.md  ->  $ORCH_HOME/roles/<name>.md  ->  packaged defaults

Frontmatter keys (all optional):
  description  one line shown in listings
  lead         "true" gives the agent the orchestrator MCP tools so it can run sub-agents
  worktree     "false" makes the agent work in the project checkout by default
  provider     default provider for the role
  model        default model for the role
  output_schema  a JSON Schema file for the final answer when an agent hands off to another
               (MCP spawn_agent/send_message); looked up like roles, first hit wins.
               `none` or absent: free text

Loadout keys (`tools`, `mcp`, `setting_sources`, `slash_commands`, `codex_user_config`)
declare what the agent loads besides this prompt; see loadout.py. A role that declares none
gets the fallback there: no user-level settings, plugins, skills or MCP servers.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path

from . import config
from .loadout import KEYS as LOADOUT_KEYS


@dataclass
class Role:
    name: str
    prompt: str
    description: str = ""
    lead: bool = False
    worktree: bool = True
    provider: str | None = None
    model: str | None = None
    loadout: dict[str, str] = field(default_factory=dict)  # declared loadout keys only
    source: str = ""  # the role file, for error messages
    output_schema: str | None = None  # schema file name; None: free text


class SchemaError(Exception):
    """A role's output schema that cannot be used; the message names the role and file."""


# Per provider: the CLI credited after Spartito in the closing line of a pull request
# description, and how a commit's Co-Authored-By trailer names Spartito. Claude Code asks for
# a trailer (with the model's name); Codex has none of its own, so none is to be invented.
_TRAILER = "`Co-Authored-By: <name> via {project} <email>`"
_CREDITS = {
    "claude": (
        "[Claude Code](https://claude.com/claude-code)",
        "take the Co-Authored-By trailer your CLI asks for, keep its email exactly, and add "
        "` via {project}` right after the name: " + _TRAILER + ".",
    ),
    "codex": (
        "[Codex](https://github.com/openai/codex)",
        "when you add a Co-Authored-By trailer, keep its email exactly and add "
        "` via {project}` right after the name: " + _TRAILER + "; do not invent a trailer "
        "or an email.",
    ),
}


def attribution(provider: str) -> str:
    """The system prompt section on attribution lines for an agent of this provider.

    Every agent's system prompt carries it after the role prompt (a role file overrides only
    the prompt) and so does the orchestrator's session. Empty for a provider without a credit.
    """
    if provider not in _CREDITS:
        return ""
    credit, trailer = _CREDITS[provider]
    project = config.PROJECT_NAME
    line = f"🤖 Generated with [{project}]({config.PROJECT_URL}) + {credit}"
    return (
        "## Attribution\n"
        "This is the owner's instruction for attribution lines and takes precedence over any "
        "attribution guidance from your CLI (such as a system reminder naming the CLI alone).\n"
        f"- End every pull request description you write with exactly this line:\n  {line}\n"
        f"- In commit messages, {trailer.format(project=project)}"
    )


def _parse(name: str, text: str, source: str) -> Role:
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
        # An empty value is kept so that it is reported, not read as "unrestricted".
        loadout={key: meta[key] for key in LOADOUT_KEYS if key in meta},
        source=source,
        output_schema=None
        if meta.get("output_schema", "none").lower() in ("", "none")
        else meta["output_schema"],
    )


def _packaged_root():
    return resources.files("orchestrator") / "roles"


def _packaged() -> dict[str, str]:
    return {
        entry.name.removesuffix(".md"): entry.read_text(encoding="utf-8")
        for entry in _packaged_root().iterdir()
        if entry.name.endswith(".md")
    }


def _user_files(filename: str, project_path: str | Path | None) -> list[Path]:
    candidates = []
    if project_path:
        candidates.append(Path(project_path) / ".orch" / "roles" / filename)
    candidates.append(config.user_roles_dir() / filename)
    return candidates


def output_schema(role: Role, project_path: str | Path | None = None) -> dict | None:
    """The role's default output schema, or None for free text. Raises SchemaError."""
    if role.output_schema is None:
        return None
    filename = role.output_schema
    where = f"role '{role.name}' ({role.source}): output_schema `{filename}`"
    if Path(filename).name != filename:
        raise SchemaError(f"{where} must be a file name next to the role file, not a path")
    found = [path for path in _user_files(filename, project_path) if path.is_file()]
    entry = found[0] if found else _packaged_root() / filename
    if not entry.is_file():
        raise SchemaError(f"{where} not found in .orch/roles, $ORCH_HOME/roles or the package")
    try:
        schema = json.loads(entry.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SchemaError(f"{where}: cannot read {entry}: {exc}") from None
    if not isinstance(schema, dict):
        raise SchemaError(f"{where}: {entry} must hold a JSON object")
    return schema


def load(name: str, project_path: str | Path | None = None) -> Role:
    for path in _user_files(f"{name}.md", project_path):
        if path.is_file():
            return _parse(name, path.read_text(encoding="utf-8"), str(path))
    packaged = _packaged()
    if name not in packaged:
        raise KeyError(f"unknown role '{name}' (available: {', '.join(sorted(packaged))})")
    return _parse(name, packaged[name], f"packaged role '{name}'")


def available(project_path: str | Path | None = None) -> list[Role]:
    names = set(_packaged())
    for directory in (
        config.user_roles_dir(),
        Path(project_path) / ".orch" / "roles" if project_path else None,
    ):
        if directory and directory.is_dir():
            names.update(path.stem for path in directory.glob("*.md"))
    return [load(name, project_path) for name in sorted(names)]
