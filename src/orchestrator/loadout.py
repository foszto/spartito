"""What an agent loads besides its role prompt: built-in tools, MCP servers, setting sources.

Without a loadout every agent would inherit the user's whole global Claude Code / Codex
configuration (connectors, plugins, skills, MCP servers), which multiplies the size of
every request. Keys, in a role's frontmatter or a loadout.toml table:

  tools              comma-separated built-in tools (Claude `--tools`); `default` = the CLI's
                     default set, `none` = no built-in tools. MCP tools are not affected.
  mcp                `strict` = only the orchestrator's own server (`--strict-mcp-config`);
                     `inherit` = also user, project, plugin and claude.ai connector servers
  setting_sources    Claude setting sources (`--setting-sources`); `project,local` drops the
                     user level (user plugins, skills, commands, hooks) but keeps the
                     project's CLAUDE.md and .claude/; `user,project,local` = inherit
  slash_commands     `false` disables every skill and slash command (`--disable-slash-commands`)
  codex_user_config  `false` = Codex ignores $CODEX_HOME/config.toml (`--ignore-user-config`)
  memory             `true` = Claude also gets Write and Edit, but only for its own memory
                     directory (~/.claude/projects/<project>/memory/); a hook refuses every
                     other target (memory_guard.py). Only for headless turns whose hooks
                     are proven to run (runner.py); otherwise the turn runs without it.
                     No effect when `tools` already grants a file-writing tool (`default`
                     does), in `orch chat`, or for Codex. Bash is not confined.

Resolved per key, first hit wins:
  <project>/.orch/loadout.toml [<role>] -> [default] -> $ORCH_HOME/loadout.toml [<role>]
  -> [default] -> the role's frontmatter -> FALLBACK

Dropping the `user` setting source drops all of ~/.claude/settings.json (env, apiKeyHelper,
model, ...), and `codex_user_config = false` drops model_provider and profiles too; users
who rely on those set `setting_sources = "user,project,local"` / `codex_user_config = true`.
"""

from __future__ import annotations

import re
import tomllib
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from . import config

if TYPE_CHECKING:
    from .roles import Role

KEYS = ("tools", "mcp", "setting_sources", "slash_commands", "codex_user_config", "memory")
SOURCES = ("user", "project", "local")
FILENAME = "loadout.toml"
DEFAULT_TABLE = "default"
# For a role that declares nothing: strip the user's global extras, keep the CLI's tools
# (including Skill, so the project's skills and commands stay usable).
FALLBACK: dict[str, Any] = {
    "tools": "default",
    "mcp": "strict",
    "setting_sources": "project,local",
    "slash_commands": True,
    "codex_user_config": False,
    "memory": False,
}
TOOL_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class LoadoutError(Exception):
    """A loadout value or file that cannot be used; the message names where it came from."""


@dataclass(frozen=True)
class Loadout:
    """The default instance restricts nothing: the CLI loads what it would on its own."""

    tools: tuple[str, ...] | None = None  # None: the CLI's default set
    strict_mcp: bool = False
    setting_sources: tuple[str, ...] | None = None  # None: every source
    slash_commands: bool = True
    codex_user_config: bool = True
    memory: bool = False  # Write/Edit confined to the memory directory


def _items(value: Any, key: str) -> list[str]:
    if isinstance(value, str):
        return [item.strip() for item in value.split(",") if item.strip()]
    if isinstance(value, list) and all(isinstance(item, str) for item in value):
        return [item.strip() for item in value if item.strip()]
    raise LoadoutError(f"`{key}` must be a comma-separated string or a list of strings")


def _flag(value: Any, key: str) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.strip().lower() in ("true", "false"):
        return value.strip().lower() == "true"
    raise LoadoutError(f"`{key}` must be true or false, not {value!r}")


def _value(key: str, value: Any) -> Any:
    """Validate one value and convert it to the type of the Loadout field."""
    if key == "tools":
        if isinstance(value, str) and value.strip().lower() == "default":
            return None
        if isinstance(value, str) and value.strip().lower() == "none":
            return ()
        names = _items(value, key)
        if not names and isinstance(value, str):
            raise LoadoutError("`tools` is empty; use `none` for no built-in tools")
        bad = [name for name in names if not TOOL_NAME.match(name)]
        if bad:
            raise LoadoutError(f"`tools` has invalid tool names: {', '.join(bad)}")
        return tuple(dict.fromkeys(names))
    if key == "mcp":
        if value not in ("strict", "inherit"):
            raise LoadoutError(f"`mcp` must be `strict` or `inherit`, not {value!r}")
        return value == "strict"
    if key == "setting_sources":
        sources = _items(value, key)
        unknown = [source for source in sources if source not in SOURCES]
        if unknown or not sources:
            raise LoadoutError(
                f"`setting_sources` takes a non-empty list of {', '.join(SOURCES)}, not {value!r}"
            )
        return None if set(sources) == set(SOURCES) else tuple(dict.fromkeys(sources))
    if key in ("slash_commands", "codex_user_config", "memory"):
        return _flag(value, key)
    raise LoadoutError(f"unknown loadout key `{key}` (use: {', '.join(KEYS)})")


def _check(table: dict[str, Any], where: str) -> dict[str, Any]:
    for key, value in table.items():
        try:
            _value(key, value)
        except LoadoutError as exc:
            raise LoadoutError(f"{where}: {exc}") from None
    return table


def load_file(path: Path) -> dict[str, dict[str, Any]]:
    """Tables of one loadout.toml, every value validated; {} when the file does not exist."""
    if not path.exists():
        return {}
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise LoadoutError(f"{path}: cannot read: {exc}") from None
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise LoadoutError(f"{path}: invalid TOML: {exc}") from None
    for name, table in data.items():
        if not isinstance(table, dict):
            raise LoadoutError(
                f"{path}: `{name}` must be a table ([default] or [<role>]), not a plain value"
            )
        _check(table, f"{path} [{name}]")
    return data


def resolve(
    role: Role,
    project_path: str | Path | None = None,
    warn: Callable[[str], None] | None = None,
) -> Loadout:
    """The loadout an agent with this role runs with. Raises LoadoutError.

    Tables naming no known role are ignored (a home-level file may name a role that exists
    only in another project) and reported through `warn`.
    """
    from . import roles  # roles imports KEYS from here

    files = []
    if project_path:
        files.append(Path(project_path) / ".orch" / FILENAME)
    files.append(config.home() / FILENAME)
    layers: list[tuple[str, dict[str, Any]]] = []
    known: set[str] | None = None
    for path in files:
        tables = load_file(path)
        if warn and set(tables) - {DEFAULT_TABLE, role.name}:
            if known is None:
                known = {other.name for other in roles.available(project_path)}
            unknown = sorted(set(tables) - {DEFAULT_TABLE} - known)
            if unknown:
                names = ", ".join(f"[{name}]" for name in unknown)
                warn(f"{path}: ignoring {names}: no such role here")
        for name in (role.name, DEFAULT_TABLE):
            layers.append((f"{path} [{name}]", tables.get(name, {})))
    layers.append((f"{role.source} (frontmatter)", role.loadout))
    layers.append(("fallback", FALLBACK))
    values: dict[str, Any] = {}
    for key in KEYS:
        where, layer = next((where, layer) for where, layer in layers if key in layer)
        try:
            values[key] = _value(key, layer[key])
        except LoadoutError as exc:
            raise LoadoutError(f"{where}: {exc}") from None
    return Loadout(
        tools=values["tools"],
        strict_mcp=values["mcp"],
        setting_sources=values["setting_sources"],
        slash_commands=values["slash_commands"],
        codex_user_config=values["codex_user_config"],
        memory=values["memory"],
    )
