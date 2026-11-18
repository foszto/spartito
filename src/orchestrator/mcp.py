"""MCP servers added in Spartito (origin `spartito`): a server's entry, the checks a new one
must pass, what lists may show of it and the entry Claude Code's `mcpServers` takes. Each
server is a capability named `mcp:<name>` (capabilities.registry), turned on and off like any
other; its row is in the table `mcp_servers` (store.py).

Spartito stores no secret. Every `env` and header value is exactly one `${VAR}` reference,
whose value comes from the environment of the process that runs the turns; a literal value
is refused, and so is `${VAR:-default}` (a default would be a literal). Arguments are literal.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from .store import Store

PREFIX = "mcp:"  # a server's capability name: mcp:<name>
KIND = "mcp"  # capabilities.Capability.kind of a server's capability
ORIGIN = "spartito"  # added in Spartito; the only origin so far
STDIO, HTTP = "stdio", "http"
TRANSPORTS = (STDIO, HTTP)
OWN_SERVER = "orch"  # Spartito's own MCP server, in every lead's turn
RESERVED = ("claude-in-chrome", "computer-use", "workspace")  # names Claude Code keeps
# Starts and ends with a letter or digit, and holds no `__`: Claude Code reads a tool name
# mcp__<server>__<tool> as the server up to the second `__`, so no name may make a tool name
# that reads as another server's (`orch__x` would pass as `orch`).
NAME = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9_-]{0,62}[A-Za-z0-9])?\Z")
NAME_RULE = (
    "letters, digits, '_' and '-', at most 64 characters, starting and ending with a letter"
    " or digit, without '__'"
)
REFERENCE = re.compile(r"\$\{[A-Za-z_][A-Za-z0-9_]*\}\Z")
ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
HEADER_NAME = re.compile(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+\Z")  # an HTTP header's token
LITERAL = "put the value in an environment variable of the service and write ${VAR}"
REACHES_LIMIT = 300


@dataclass(frozen=True)
class Server:
    """One server. `definition`: stdio {"command", "args", "env"}, http {"url", "headers"};
    `own_reaches`: the owner's line on what it reaches (None: none given)."""

    name: str
    transport: str
    definition: Mapping[str, Any]
    own_reaches: str | None = None
    origin: str = ORIGIN
    created_at: str | None = None
    updated_at: str | None = None

    @property
    def capability(self) -> str:
        return PREFIX + self.name

    @property
    def target(self) -> str:
        """What it runs or where it sends: the command, or the URL's origin."""
        if self.transport == STDIO:
            return self.definition["command"]
        return url_origin(self.definition["url"])

    @property
    def generated_reaches(self) -> str:
        if self.transport == STDIO:
            return (
                f"runs {self.target} on this machine as you, with your files, your network"
                " and the environment it is given"
            )
        return (
            f"sends the agent's tool calls, and what they carry, to {self.target}, with the"
            " credentials configured for it"
        )

    @property
    def reaches(self) -> str:
        """The generated line, and the owner's after it."""
        own = f"; {self.own_reaches}" if self.own_reaches else ""
        return self.generated_reaches + own

    def shown(self) -> dict[str, Any]:
        """The definition as lists and JSON show it: a URL as its origin only. Env and header
        values are `${VAR}` references, never values."""
        if self.transport == STDIO:
            return {
                "command": self.definition["command"],
                "args": list(self.definition["args"]),
                "env": dict(self.definition["env"]),
            }
        return {"url": self.target, "headers": dict(self.definition["headers"])}

    def entry(self) -> dict[str, Any]:
        """The server as an entry of Claude Code's `mcpServers` (a turn's --mcp-config)."""
        if self.transport == STDIO:
            return {
                "command": self.definition["command"],
                "args": list(self.definition["args"]),
                "env": dict(self.definition["env"]),
            }
        return {
            "type": "http",
            "url": self.definition["url"],
            "headers": dict(self.definition["headers"]),
        }


def capability_name(name: str) -> str:
    """`docs-db` or `mcp:docs-db` as the capability name `mcp:docs-db`."""
    return name if name.startswith(PREFIX) else PREFIX + name


def server_name(name: str) -> str:
    """`mcp:docs-db` or `docs-db` as the server name `docs-db`."""
    return name.removeprefix(PREFIX)


def check_name(name: Any) -> str:
    """A name for a new server: NAME, and not one Spartito or Claude Code keeps. Raises
    ValueError. (Whether it is taken is the store's to say.)"""
    if not isinstance(name, str) or not NAME.match(name) or "__" in name:
        raise ValueError(f"invalid MCP server name {name!r}: {NAME_RULE}")
    if name.lower() == OWN_SERVER:
        raise ValueError(f"{name!r} is the name of Spartito's own MCP server; choose another")
    if name.lower() in RESERVED:
        raise ValueError(f"{name!r} is a name Claude Code reserves; choose another")
    return name


def url_origin(url: str) -> str:
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}"


def _reference(value: Any, what: str) -> str:
    if not isinstance(value, str) or not REFERENCE.match(value):
        raise ValueError(
            f"{what}: {value!r} is not a ${{VAR}} reference. Spartito stores no secret and"
            f" refuses literal values: {LITERAL}"
        )
    return value


def _pairs(value: Any, what: str, separator: str) -> list[tuple[Any, Any]]:
    """A mapping, or strings `name<separator>value` (as typed on a command line)."""
    if value is None:
        return []
    if isinstance(value, Mapping):
        return list(value.items())
    if isinstance(value, str) or not isinstance(value, Iterable):
        raise ValueError(f"{what} must be an object, or a list of 'name{separator}value'")
    pairs = []
    for item in value:
        if not isinstance(item, str) or separator not in item:
            raise ValueError(f"{what}: expected 'name{separator}value', got {item!r}")
        name, _, text = item.partition(separator)
        pairs.append((name.strip(), text.strip()))
    return pairs


def _env(value: Any) -> dict[str, str]:
    result = {}
    for name, text in _pairs(value, "env", "="):
        if not isinstance(name, str) or not ENV_NAME.match(name):
            raise ValueError(
                f"env: invalid variable name {name!r}: letters, digits and '_', not starting"
                " with a digit"
            )
        result[name] = _reference(text, f"env {name}")
    return result


def _headers(value: Any) -> dict[str, str]:
    result: dict[str, str] = {}
    for name, text in _pairs(value, "headers", ":"):
        if not isinstance(name, str) or not HEADER_NAME.match(name):
            raise ValueError(f"headers: invalid header name {name!r}")
        if name.lower() in {other.lower() for other in result}:
            raise ValueError(f"headers: {name} is given twice")
        result[name] = _reference(text, f"header {name}")
    return result


def _url(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("url must be an http or https URL")
    url = value.strip()
    if any(char.isspace() for char in url):
        raise ValueError("url must not contain spaces")
    try:
        parts = urlsplit(url)
        parts.port  # noqa: B018 - raises ValueError for a port that is not a number
    except ValueError:
        raise ValueError(f"url {url!r} cannot be read") from None
    if parts.scheme not in ("http", "https"):
        raise ValueError(f"url {url!r} must start with http:// or https://")
    if "@" in parts.netloc:
        raise ValueError(
            f"url: a user name or password in the URL is refused; {LITERAL} in a header"
        )
    if not parts.hostname:
        raise ValueError(f"url {url!r} has no host")
    for parameter in filter(None, parts.query.split("&")):
        name, has_value, value = parameter.partition("=")
        if not has_value or not REFERENCE.match(value):
            raise ValueError(
                f"url: the query parameter {name!r} must have a ${{VAR}} reference as its"
                f" value. Spartito stores no secret and refuses literal values: {LITERAL}"
            )
    return url


def _reaches(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("reaches must be text")
    text = value.strip()
    if "\n" in text or "\r" in text:
        raise ValueError("reaches must be one line")
    if len(text) > REACHES_LIMIT:
        raise ValueError(f"reaches must be at most {REACHES_LIMIT} characters")
    return text or None


def server(
    name: Any,
    *,
    command: Any = None,
    args: Any = (),
    env: Any = None,
    url: Any = None,
    headers: Any = None,
    reaches: Any = None,
) -> Server:
    """A new server from what the owner gives: a `command` (stdio, with `args` and `env`)
    or a `url` (http, with `headers`). `env` and `headers`: mappings, or strings
    `NAME=${VAR}` and `Name: ${VAR}`. Raises ValueError with what to change."""
    check_name(name)
    if (command is None) == (url is None):
        raise ValueError("a server has either a command (stdio) or a URL (http)")
    own = _reaches(reaches)
    if command is not None:
        if headers:
            raise ValueError("headers are for a URL (http) server; a command gets env")
        if not isinstance(command, str) or not command.strip():
            raise ValueError("command must not be empty")
        if "\n" in command or "\r" in command:
            raise ValueError("command must be one line")
        if isinstance(args, str) or not isinstance(args, Iterable):
            raise ValueError("args must be a list of strings")
        args = list(args)
        if not all(isinstance(arg, str) for arg in args):
            raise ValueError("args must be a list of strings")
        definition = {"command": command.strip(), "args": args, "env": _env(env)}
        return Server(name, STDIO, definition, own)
    if args or env:
        raise ValueError("args and env are for a command (stdio) server; a URL gets headers")
    return Server(name, HTTP, {"url": _url(url), "headers": _headers(headers)}, own)


def from_row(row: Mapping[str, Any]) -> Server | None:
    """A stored row as a server, checked again as a new one would be; None when it is not
    readable as one Spartito may wire (another origin, a definition that fails the checks)."""
    if row.get("origin") != ORIGIN or row.get("transport") not in TRANSPORTS:
        return None
    try:
        definition = json.loads(row.get("definition") or "")
        if not isinstance(definition, dict):
            return None
        if row["transport"] == STDIO:
            found = server(
                row["name"],
                command=definition.get("command"),
                args=definition.get("args") or (),
                env=definition.get("env") or {},
                reaches=row.get("reaches"),
            )
        else:
            found = server(
                row["name"],
                url=definition.get("url"),
                headers=definition.get("headers") or {},
                reaches=row.get("reaches"),
            )
    except ValueError, TypeError:
        return None
    return Server(
        found.name,
        found.transport,
        found.definition,
        found.own_reaches,
        created_at=row.get("created_at"),
        updated_at=row.get("updated_at"),
    )


def servers(store: Store) -> list[Server]:
    """Every readable server of the store, by name."""
    return [found for row in store.mcp_servers() if (found := from_row(row)) is not None]


def find(store: Store, name: str) -> Server | None:
    row = store.get_mcp_server(server_name(name))
    return from_row(row) if row else None


def add(store: Store, found: Server) -> Server:
    """Store a new server. Raises ValueError when the name is taken."""
    row = store.add_mcp_server(
        found.name, found.origin, found.transport, dict(found.definition), found.own_reaches
    )
    return from_row(row) or found


def last_statuses(record: Mapping[str, Any] | None) -> dict[str, str]:
    """The servers' statuses a turn's record (run_meta.capabilities) keeps under `mcp`:
    {"<server name>": "<status the CLI reported>"}; anything else counts as none."""
    found = record.get("mcp") if isinstance(record, Mapping) else None
    if not isinstance(found, dict):
        return {}
    return {
        name: status
        for name, status in found.items()
        if isinstance(name, str) and isinstance(status, str)
    }
