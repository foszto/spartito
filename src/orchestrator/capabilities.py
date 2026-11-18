"""Capabilities: named extras an agent's CLI gets for its turns, beyond its role prompt and
its loadout (a set of tools, a flag, later an MCP server). The first one is `chrome`, the
browser tools of Claude in Chrome.

The registry is data: each entry says, per provider that supports it, what it adds to a
turn when it is on and when it is off (providers/base.Support); the providers place those
additions on their own command lines. A provider an entry does not name does not support
it: an agent on it skips the capability, with a note.

Whether an agent's turn has a capability, the last level that says something wins:

    the default (DEFAULT_ON) -> global setting -> the project's setting -> the role's setting
    (the project's entry for the role, else the global one) -> the agent's own setting

Settings (settings.py): `capabilities` {"chrome": true} in the global and a project's scope,
`role_capabilities` {"<role>": {"chrome": false}} in either scope; the agent's own setting
is `agents.capabilities` (the same JSON shape). `false` turns one off, an absent name
inherits. A role's `false` is final: the agent's own setting cannot turn it back on.
Unreadable stored values count as absent. Resolved when a turn starts (runner.py), so a
change applies from an agent's next message.

ORCH_CAPABILITIES="package.module:attribute,..." registers more entries after the built-in
ones; the attribute is a Capability or a sequence of them. Their capabilities are off until
turned on.
"""

from __future__ import annotations

import importlib
import json
import os
import re
import sys
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

from . import providers
from .models import Agent
from .providers.base import Support, Wiring
from .providers.claude import CHROME as CLAUDE_CHROME
from .store import Store

SETTING = "capabilities"
ROLE_SETTING = "role_capabilities"
NAME = re.compile(r"^[a-z][a-z0-9-]{0,31}$")
PLUGINS_ENV = "ORCH_CAPABILITIES"
GLOBAL_SCOPE = ""  # settings.GLOBAL (settings imports this module)

ON, OFF, SKIPPED = "on", "off", "skipped"
# The levels, in the order they are applied (a later one wins).
DEFAULT, GLOBAL, PROJECT, ROLE, AGENT = "default", "global", "project", "role", "agent"
UNKNOWN, UNAVAILABLE, AVAILABLE = "unknown", "unavailable", "available"
NOT_REGISTERED = "not registered here"


@dataclass(frozen=True)
class Capability:
    name: str  # NAME: what is turned on and off
    label: str  # what people read ("Claude in Chrome")
    description: str  # one line
    reaches: str  # one line: what it can reach
    providers: Mapping[str, Support]  # provider name -> how it carries it
    requires: tuple[str, ...] = ()  # what it needs, one phrase each
    # (status, reason): UNKNOWN, UNAVAILABLE or AVAILABLE; instant and offline, it reads no
    # file and runs nothing. None: UNKNOWN.
    check: Callable[[], tuple[str, str]] | None = None
    docs: str = ""


# Variables with which Claude Code does not use a claude.ai login (its docs: Claude in
# Chrome is off then). Only whether they are set is looked at, never their values.
NOT_CLAUDE_AI = (
    "ANTHROPIC_API_KEY",
    "CLAUDE_CODE_OAUTH_TOKEN",
    "CLAUDE_CODE_USE_BEDROCK",
    "CLAUDE_CODE_USE_VERTEX",
)
CHROME_UNSEEN = (
    "needs the Claude in Chrome extension, a running browser and a claude.ai login,"
    " which Spartito cannot see"
)


def _is_set(name: str) -> bool:
    return (os.environ.get(name) or "").strip().lower() not in ("", "0", "false", "no", "off")


def chrome_check() -> tuple[str, str]:
    """Best effort, from this process's environment only."""
    for name in NOT_CLAUDE_AI:
        if _is_set(name):
            return UNAVAILABLE, f"{name} is set: Claude in Chrome needs a claude.ai login"
    return UNKNOWN, CHROME_UNSEEN


CHROME = Capability(
    name="chrome",
    label="Claude in Chrome",
    description="Claude Code's browser tools: open tabs, read pages, click, type and submit",
    reaches=(
        "your real browser, signed in as you: its open tabs, the pages and accounts you are"
        " logged in to; it reads what pages show and acts on them (click, type, submit) as you"
    ),
    providers={"claude": CLAUDE_CHROME},
    requires=(
        "Claude Code with --chrome",
        "the Claude in Chrome extension, version 1.0.36 or later, in Chrome or another"
        " supported Chromium browser, running while the turn runs",
        "Claude Code logged in with a claude.ai account: not an API key, a long-lived token"
        " or a third-party cloud provider (Bedrock, Vertex)",
    ),
    check=chrome_check,
    docs="docs/capabilities.md#chrome",
)

# The product's default: the capabilities every agent has unless a level turns them off.
DEFAULT_ON: frozenset[str] = frozenset()
BUILTIN: tuple[Capability, ...] = (CHROME,)
REGISTRY: dict[str, Capability] = {}
PLUGINS: set[str] = set()  # names registered from ORCH_CAPABILITIES


def register(capability: Capability, plugin: bool = False) -> None:
    """Add an entry. Raises ValueError for an invalid or a taken name; warns (stderr) for
    a provider that is not registered (it may be one that only another process loads)."""
    if not isinstance(capability, Capability):
        raise ValueError(f"not a Capability: {capability!r}")
    if not NAME.match(capability.name):
        raise ValueError(
            f"invalid capability name {capability.name!r}: lower case letters, digits and"
            " '-', starting with a letter, at most 32 characters"
        )
    if capability.name in REGISTRY:
        raise ValueError(f"capability {capability.name!r} is registered twice")
    for provider, support in capability.providers.items():
        if not isinstance(support, Support):
            raise ValueError(f"capability {capability.name!r}: {provider} has no Support")
        if provider not in providers.PROVIDERS:
            print(
                f"capability {capability.name!r} names provider {provider!r}, which is not"
                " registered here",
                file=sys.stderr,
            )
    REGISTRY[capability.name] = capability
    if plugin:
        PLUGINS.add(capability.name)


def _load_plugins(spec: str) -> None:
    """ORCH_CAPABILITIES="package.module:attribute,...": each attribute a Capability or a
    sequence of them. A broken spec raises, as a broken ORCH_PROVIDERS does."""
    for item in filter(None, (part.strip() for part in spec.split(","))):
        module, _, attribute = item.partition(":")
        found = getattr(importlib.import_module(module), attribute)
        entries = [found] if isinstance(found, Capability) else list(found)
        for entry in entries:
            register(entry, plugin=True)


for _builtin in BUILTIN:
    register(_builtin)
_load_plugins(os.environ.get(PLUGINS_ENV, ""))


def check_name(name: Any) -> str:
    """A capability name as stored: syntax only (a plugin may be gone). Raises ValueError."""
    if not isinstance(name, str) or not NAME.match(name):
        raise ValueError(
            f"invalid capability name {name!r}: lower case letters, digits and '-',"
            " starting with a letter"
        )
    return name


def known(name: str) -> Capability:
    """A registered capability; ValueError naming the known ones otherwise."""
    if name not in REGISTRY:
        raise ValueError(f"unknown capability {name!r} (known: {', '.join(REGISTRY) or 'none'})")
    return REGISTRY[name]


def flags(value: Any) -> dict[str, bool]:
    """A stored {name: true|false} as read: entries with an invalid name or a value that is
    not a boolean, and anything that is not an object, count as absent."""
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return {}
    if not isinstance(value, dict):
        return {}
    return {
        name: on
        for name, on in value.items()
        if isinstance(name, str) and NAME.match(name) and isinstance(on, bool)
    }


def role_flags(value: Any, role: str) -> dict[str, bool]:
    """The entry for `role` of a stored `role_capabilities`, read like `flags`."""
    return flags(value.get(role)) if isinstance(value, dict) else {}


def parse(text: str) -> dict[str, bool]:
    """`chrome,-other` as {"chrome": True, "other": False}; "" is {}. Raises ValueError."""
    result = {}
    for part in filter(None, (part.strip() for part in text.split(","))):
        name = part.removeprefix("-").strip()
        result[check_name(name)] = not part.startswith("-")
    return result


def clean(value: Any, key: str = SETTING) -> dict[str, bool]:
    """A `capabilities` setting to store: {name: bool}; None drops a name (inherit).
    Raises ValueError."""
    if not isinstance(value, dict):
        raise ValueError(f"{key} must be an object of capability names and true or false")
    result = {}
    for name, on in value.items():
        check_name(name)
        if on is None:
            continue
        if not isinstance(on, bool):
            raise ValueError(f"{key}: {name} must be true or false (null: inherit)")
        result[name] = on
    return result


def clean_roles(value: Any) -> dict[str, dict[str, bool]]:
    """A `role_capabilities` setting to store: {role: {name: bool}}. Raises ValueError."""
    if not isinstance(value, dict):
        raise ValueError(f"{ROLE_SETTING} must be an object of role names")
    result = {}
    for role, entries in value.items():
        if not isinstance(role, str) or not role.strip():
            raise ValueError(f"{ROLE_SETTING}: role names must be non-empty strings")
        if entries is None:
            continue
        cleaned = clean(entries, f"{ROLE_SETTING}.{role}")
        if cleaned:
            result[role] = cleaned
    return result


# -- resolution ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Item:
    """One capability's state for an agent's next turn."""

    name: str
    label: str
    state: str  # ON, OFF or SKIPPED (on by its levels, but not given: see note)
    source: str  # the level that decided it: DEFAULT, GLOBAL, PROJECT, ROLE or AGENT
    override: bool | None  # the agent's own setting (None: none)
    final: bool  # the role turned it off: the agent's own setting cannot turn it on
    note: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "label": self.label,
            "state": self.state,
            "source": self.source,
            "override": self.override,
            "final": self.final,
            "note": self.note,
        }


@dataclass(frozen=True)
class Resolution:
    items: tuple[Item, ...]
    wirings: tuple[Wiring, ...] = field(default=())  # what the provider adds to the turn

    @property
    def on(self) -> list[str]:
        return [item.name for item in self.items if item.state == ON]

    @property
    def skipped(self) -> dict[str, str]:
        return {item.name: item.note or "" for item in self.items if item.state == SKIPPED}

    def record(self) -> dict[str, Any]:
        """What run_meta.capabilities keeps of a turn."""
        return {"on": self.on, "skipped": self.skipped}

    def breakdown(self) -> dict[str, Any]:
        """The agent views' `capabilities`."""
        return {"on": self.on, "items": [item.to_dict() for item in self.items], "error": None}


def _settings(store: Store, scope: str) -> dict[str, Any]:
    data = store.get_settings(scope)
    return data if isinstance(data, dict) else {}


def role_values(store: Store, project: str | None, role: str) -> dict[str, tuple[bool, str]]:
    """The role level for `role`: per name its value and the scope that decides it (the
    project's name, whose entry wins, or GLOBAL_SCOPE)."""
    found = {
        name: (on, GLOBAL_SCOPE)
        for name, on in role_flags(_settings(store, GLOBAL_SCOPE).get(ROLE_SETTING), role).items()
    }
    if project:
        stored = _settings(store, project).get(ROLE_SETTING)
        found.update({name: (on, project) for name, on in role_flags(stored, role).items()})
    return found


def resolve(store: Store, agent: Agent) -> Resolution:
    """The capabilities of the agent's next turn on its provider, by the levels above. Never
    raises for stored values: unreadable ones count as absent."""
    globally = _settings(store, GLOBAL_SCOPE)
    levels: list[tuple[str, dict[str, bool]]] = [(GLOBAL, flags(globally.get(SETTING)))]
    project = _settings(store, agent.project) if agent.project else {}
    if agent.project:
        levels.append((PROJECT, flags(project.get(SETTING))))
    role = {
        **role_flags(globally.get(ROLE_SETTING), agent.role),
        **role_flags(project.get(ROLE_SETTING), agent.role),
    }
    own = flags(agent.capabilities)
    names = list(REGISTRY) + [
        name for level in (*levels, (ROLE, role), (AGENT, own)) for name in level[1]
    ]
    items, wirings = [], []
    for name in dict.fromkeys(names):
        on, source = name in DEFAULT_ON, DEFAULT
        for level, values in (*levels, (ROLE, role)):
            if name in values:
                on, source = values[name], level
        final = role.get(name) is False
        note = None
        if name in own:
            if own[name] and final:
                note = f"off for role {agent.role}: the role's setting wins over the agent's"
            else:
                on, source = own[name], AGENT
        capability = REGISTRY.get(name)
        if capability is None and not on:
            continue  # nothing to say about an unknown name that is off
        support = capability.providers.get(agent.provider) if capability else None
        state = ON if on else OFF
        if note:
            state = SKIPPED
        elif on and capability is None:
            state, note = SKIPPED, NOT_REGISTERED
        elif on and support is None:
            state, note = SKIPPED, f"{agent.provider} does not support it"
        if support is not None:
            wirings.append(support.on if state == ON else support.off)
        label = capability.label if capability else name
        items.append(Item(name, label, state, source, own.get(name), final, note))
    return Resolution(tuple(items), tuple(wiring for wiring in wirings if wiring != Wiring()))


def describe_record(record: Mapping[str, Any]) -> str:
    """`capabilities for this turn: chrome; skipped: jira (not registered here)`."""
    on = ", ".join(record.get("on") or []) or "none"
    skipped = record.get("skipped") or {}
    text = f"capabilities for this turn: {on}"
    if skipped:
        text += "; skipped: " + ", ".join(f"{name} ({why})" for name, why in skipped.items())
    return text


def previous_record(records: Iterable[str | None]) -> dict[str, Any] | None:
    """The last readable record of `records` (run_meta.capabilities, oldest first)."""
    found = None
    for stored in records:
        try:
            data = json.loads(stored) if stored else None
        except ValueError:
            continue
        if isinstance(data, dict):
            found = data
    return found


# -- status -------------------------------------------------------------------------------


def status(capability: Capability, provider_states: Iterable[Mapping[str, Any]]) -> tuple[str, str]:
    """Whether the capability can work for this process, best effort: (UNKNOWN, reason) when
    nothing can be seen, (UNAVAILABLE, reason) when it is known not to work, AVAILABLE only
    from the entry's own check. `provider_states`: the providers' availability entries
    (core/availability.listing: name, label, usable, reason). Information only: a turn
    passes a capability that is on whatever its status."""
    states = {entry.get("name"): entry for entry in provider_states}
    supported = [states[name] for name in capability.providers if name in states]
    if not supported:
        return UNAVAILABLE, "no provider registered here supports it"
    if not any(entry.get("usable") for entry in supported):
        entry = supported[0]
        label = entry.get("label") or entry.get("name")
        reason = entry.get("reason") or "not usable"
        return UNAVAILABLE, f"needs {label}: {reason}"
    if capability.check is None:
        return UNKNOWN, "Spartito cannot check it"
    try:
        found, reason = capability.check()
    except Exception as exc:  # a plugin's check must not break the listing
        return UNKNOWN, f"its check failed: {exc}"
    if found not in (UNKNOWN, UNAVAILABLE, AVAILABLE):
        return UNKNOWN, f"its check gave an unknown status {found!r}"
    return found, reason
