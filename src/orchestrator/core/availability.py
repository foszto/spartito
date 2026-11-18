"""The supported harnesses (providers) and which of them this process can start.

A check runs the CLI's `--version` (providers.locate.probe) and, for a usable one, asks it
for its models. The results are kept per process for TTL_SECONDS, on purpose not in the
home: what can be started depends on the PATH of the process that starts the runners, and
the command line, the service and the MCP servers can each see a different one.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Any, Protocol

from .. import capabilities, providers, roles
from ..errors import OrchestratorError
from ..providers import locate
from ..providers.base import Provider, model_choices
from ..store import now

TTL_SECONDS = 300.0
NOT_CHECKED = "not checked yet"
NO_EXECUTABLE = "declares no executable; not checked"
MODELS_LOADING = "model list loading"
MODELS_UNAVAILABLE = "the CLI is not available"
USABLE = (locate.AVAILABLE, locate.UNKNOWN)
DOCS = "docs/providers.md"


class Facade(Protocol):
    """What these functions use of the facade (they are its methods): nothing."""


class _Checked:
    def __init__(self, probe: locate.Probe, models: dict[str, Any] | None) -> None:
        self.probe = probe
        self.models = models
        self.at = time.monotonic()
        self.checked_at = now()


_lock = threading.Lock()
_checked: dict[str, _Checked] = {}
_generation = 0  # reset() starts a new one: a check under an older one is dropped
_refresher: threading.Thread | None = None


def reset() -> None:
    """Forget every check (tests: providers are replaced between them)."""
    global _generation
    with _lock:
        _checked.clear()
        _generation += 1


def probe(provider: Provider) -> locate.Probe:
    """The availability check of one provider; runs its CLI's version command only. A CLI
    older than the provider's `min_version` is not installed in a usable version."""
    executable = getattr(provider, "executable", None)
    if not executable:
        return locate.Probe(locate.UNKNOWN, None, None, NO_EXECUTABLE)
    found = locate.probe(
        executable,
        tuple(getattr(provider, "version_args", None) or ("--version",)),
        getattr(provider, "login_args", None),
    )
    minimum = getattr(provider, "min_version", None)
    if minimum and found.version and _version(found.version) < tuple(minimum):
        where = found.reason.partition("; ")[0]
        oldest = ".".join(str(part) for part in minimum)
        reason = f"{where}; version {found.version} is older than {oldest}, the oldest supported"
        return locate.Probe(locate.NOT_INSTALLED, found.version, found.path, reason)
    return found


def _version(text: str) -> tuple[int, ...]:
    """A version as locate.VERSION finds it, `2.10.1` -> (2, 10, 1)."""
    return tuple(int(part) for part in text.split("."))


def usable(entry: dict[str, Any] | locate.Probe) -> bool:
    status = entry.status if isinstance(entry, locate.Probe) else entry["status"]
    return status in USABLE


def _check(name: str, provider: Provider, generation: int, models_too: bool = True) -> None:
    """Probe one provider and, with `models_too`, ask a usable one for its models (None:
    not asked)."""
    found = probe(provider)
    models = None
    if usable(found) and models_too:
        try:
            models = model_choices(provider)
        except Exception as exc:  # a plugin's list must not break the listing
            models = {"models": [], "models_note": f"model list unavailable: {exc}"}
    with _lock:
        if generation == _generation:
            _checked[name] = _Checked(found, models)


def _stale(name: str, models_too: bool = True) -> bool:
    checked = _checked.get(name)
    if checked is None or time.monotonic() - checked.at >= TTL_SECONDS:
        return True
    return models_too and checked.models is None and usable(checked.probe)


def _refresh(names: list[str], generation: int, models_too: bool = True) -> None:
    for name in names:
        if generation != _generation:  # reset meanwhile: what it would find is dropped
            return
        provider = providers.PROVIDERS.get(name)
        if provider is not None:
            _check(name, provider, generation, models_too)


def _refresh_in_background(names: list[str]) -> None:
    """One daemon thread at a time checks `names`; a call while it runs starts none."""
    global _refresher
    with _lock:
        if _refresher is not None and _refresher.is_alive():
            return
        _refresher = threading.Thread(
            target=_refresh, args=(names, _generation), name="provider-check", daemon=True
        )
        _refresher.start()


def describe(provider: Provider) -> dict[str, Any]:
    """What a provider is and supports: its own attributes, or what its methods show."""
    compact_prompt = getattr(provider, "compact_prompt", None)
    instructions = False
    if compact_prompt is not None:
        try:
            compact_prompt("x")
            instructions = True
        except ValueError:
            pass
    return {
        "name": provider.name,
        "label": getattr(provider, "label", None) or provider.name,
        "description": getattr(provider, "description", None) or "",
        "executable": getattr(provider, "executable", None),
        "efforts": list(provider.efforts),
        "features": {
            "cost": getattr(provider, "reports_cost", None),
            "compaction": compact_prompt is not None,
            "compaction_instructions": instructions,
            "context_size": getattr(provider, "reports_context", None),
            "role_prompt": getattr(provider, "role_prompt", None),
            "attribution": bool(
                roles.attribution(getattr(provider, "credit", None), bot_coauthor=False)
            ),
        },
        "limits": list(getattr(provider, "limits", None) or ()),
        # The registered capabilities it carries (capabilities.py).
        "capabilities": [
            entry.name
            for entry in capabilities.REGISTRY.values()
            if provider.name in entry.providers
        ],
    }


def _entry(name: str, provider: Provider, checked: _Checked | None) -> dict[str, Any]:
    entry = describe(provider)
    entry["name"] = name
    found = checked.probe if checked else locate.Probe(locate.UNKNOWN, None, None, NOT_CHECKED)
    models: dict[str, Any] = {"models": [], "models_note": MODELS_LOADING}
    if checked and checked.models is not None:
        models = checked.models
    elif not usable(found):
        models = {"models": [], "models_note": MODELS_UNAVAILABLE}
    return {
        **entry,
        **models,
        "status": found.status,
        "usable": usable(found),
        "version": found.version,
        "path": found.path,
        "reason": found.reason,
        "checked_at": checked.checked_at if checked else None,
        "default": False,
    }


def listing(refresh: bool = False, wait: bool = True, models: bool = True) -> list[dict[str, Any]]:
    """Every registered provider, in registry order, with its availability and models.

    `refresh`: check again even what was checked less than TTL_SECONDS ago. `wait=False`
    never blocks: what was not checked yet comes back `unknown` ("not checked yet", models
    loading) while one background thread checks it. `models=False` (with `wait`): does not
    ask the CLIs for their models, which may take a while. `default` marks the first usable
    one: the provider a new project gets."""
    names = list(providers.PROVIDERS)
    models_too = models or not wait
    with _lock:
        due = [name for name in names if refresh or _stale(name, models_too)]
        generation = _generation
    if due and wait:
        _refresh(due, generation, models_too)
    elif due:
        _refresh_in_background(due)
    with _lock:
        checked = dict(_checked)
    entries = [
        _entry(name, provider, checked.get(name)) for name, provider in providers.PROVIDERS.items()
    ]
    first = next((entry for entry in entries if entry["usable"]), None)
    if first is not None:
        first["default"] = True
    return entries


def checked() -> list[dict[str, Any]]:
    """Every registered provider as the last check left it, starting none: what was not
    checked yet is `unknown` ("not checked yet")."""
    with _lock:
        found = dict(_checked)
    return [
        _entry(name, provider, found.get(name)) for name, provider in providers.PROVIDERS.items()
    ]


def status(name: str, refresh: bool = False, wait: bool = True) -> dict[str, Any]:
    """One provider's entry of `listing`."""
    providers.get(name)  # KeyError for an unknown one
    return next(entry for entry in listing(refresh, wait) if entry["name"] == name)


def provider_list(
    orch: Facade, refresh: bool = False, wait: bool = True, models: bool = True
) -> list[dict[str, Any]]:
    """The supported harnesses with their availability (listing), for the facade."""
    return listing(refresh, wait, models)


@dataclass(frozen=True)
class Choice:
    """The provider chosen for something new, and what to tell about it (a fallback)."""

    provider: str
    note: str | None = None


class NoneUsable(OrchestratorError):
    """No registered provider can be started here; the message lists each one's reason."""


def _state(entry: dict[str, Any]) -> str:
    """`not installed (<reason>)`: an entry's status and reason in words."""
    return f"{entry['status'].replace('_', ' ')} ({entry['reason']})"


def choose(
    requested: str | None, defaults: list[tuple[str | None, str]] = (), wait: bool = True
) -> Choice:
    """The provider for a new agent or project.

    `requested` (explicit) must be registered and usable: otherwise OrchestratorError, with
    the reason and the usable alternatives. Else the first named of `defaults` ((name, what
    it is: "the project's default")) is the intended one; when it is not usable, the first
    usable provider in registry order is chosen, with a note that says so. Without
    defaults: the first usable one, with a note when that is not the first registered one.
    `unknown` counts as usable. Nothing usable: NoneUsable.
    `wait=False` never waits for a check (what is not checked yet counts as usable)."""
    entries = {entry["name"]: entry for entry in listing(wait=wait, models=False)}
    usable_names = [name for name, entry in entries.items() if entry["usable"]]
    intended = requested or next((name for name, _ in defaults if name), None)
    if intended is not None and intended not in entries:
        raise OrchestratorError(f"unknown provider '{intended}' (available: {', '.join(entries)})")
    if intended is not None and entries[intended]["usable"]:
        return Choice(intended)
    if requested:
        others = ", ".join(usable_names) or "none"
        raise OrchestratorError(
            f"{requested} is not available: {_state(entries[requested])}. Available: {others}."
            f" {locate.says_more()}"
        )
    if not usable_names:
        each = "; ".join(f"{name}: {_state(entry)}" for name, entry in entries.items())
        raise NoneUsable(f"no provider is available here: {each}. See {DOCS}")
    chosen = usable_names[0]
    if intended is None:
        first = next(iter(entries))  # nothing named: still say what is skipped
        if chosen == first:
            return Choice(chosen)
        intended, what = first, "the first supported provider"
    else:
        what = next(what for name, what in defaults if name)
    note = (
        f"{intended} ({what}) is not available: {_state(entries[intended])}; "
        f"this agent runs on {chosen}"
    )
    return Choice(chosen, note)


def default_note(provider: str) -> str | None:
    """A project's default `provider` when it is not usable here: what its new agents get."""
    entries = {entry["name"]: entry for entry in listing(models=False)}
    entry = entries.get(provider)
    if entry is None or entry["usable"]:
        return None
    usable_names = [name for name, other in entries.items() if other["usable"]]
    then = (
        f"new agents run on {usable_names[0]} until it is"
        if usable_names
        else f"no other provider is available here either (see {DOCS})"
    )
    return f"The default provider {provider} is not available: {_state(entry)}; {then}."
