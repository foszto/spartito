"""Model and effort defaults per tier, set globally and overridable per project.

Three tiers: the top-level `orchestrator`, a project's `lead` (any role that may run
sub-agents) and `worker` (every other role). Settings are kept per provider because model
names and effort levels are provider-specific:

    {"worker": {"claude": {"model": "haiku", "effort": "low"}, "codex": {"model": "gpt-6-luna"}}}

Precedence for one turn, first hit wins:
    explicit value on the agent -> project setting -> global setting -> the role's model
    -> nothing (the CLI's own default)

Values are resolved when a turn starts, so a changed setting applies to an existing
agent's next message.

One more key, `archive_after_hours`, is not a tier: after how many idle hours a finished
agent whose work is safe is archived automatically (0: never). A project's value overrides
the global one; without either it is ARCHIVE_AFTER_DEFAULT.
"""

from __future__ import annotations

from typing import Any

from . import providers
from .models import Agent
from .roles import Role
from .store import Store

TIERS = ("orchestrator", "lead", "worker")
GLOBAL = ""
FIELDS = ("model", "effort")
ARCHIVE_AFTER = "archive_after_hours"
ARCHIVE_AFTER_DEFAULT = 24.0


MODEL_MAX_CHARS = 200


def check_model(value: Any) -> str:
    """A model name an agent may be set to: any name the provider might accept, but one
    safe as the single argument after `--model`/`-m`. Raises ValueError."""
    if not isinstance(value, str):
        raise ValueError("model must be a string (or null to inherit)")
    shown = repr(value[:40] + ("…" if len(value) > 40 else ""))
    if not value:
        raise ValueError("model must not be empty; clear it to inherit the default instead")
    if len(value) > MODEL_MAX_CHARS:
        raise ValueError(f"model {shown} is too long (at most {MODEL_MAX_CHARS} characters)")
    if any(char.isspace() for char in value) or not value.isprintable():
        raise ValueError(f"model {shown} must be one word, without spaces or control characters")
    if value.startswith("-"):
        raise ValueError(f"model {shown} must not start with '-'")
    return value


def tier_of(agent: Agent, role: Role) -> str:
    if agent.project is None:
        return "orchestrator"
    return "lead" if role.lead else "worker"


def archive_hours(value: Any) -> float | None:
    """An `archive_after_hours` value: hours (>= 0; 0 or "off": never), None for empty
    (inherit). Raises ValueError."""
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    if isinstance(value, str) and value.strip().lower() == "off":
        return 0.0
    try:
        hours = float(value)
    except TypeError, ValueError:
        raise ValueError(f"{ARCHIVE_AFTER} must be a number of hours or 'off'") from None
    if not hours >= 0 or hours == float("inf") or isinstance(value, bool):
        raise ValueError(f"{ARCHIVE_AFTER} must be 0 or more hours ('off' or 0: never)")
    return hours


def archive_after(store: Store, project: str | None) -> float:
    """Idle hours after which the project's agents are archived automatically; 0: never."""
    for scope in ([project] if project else []) + [GLOBAL]:
        value = store.get_settings(scope).get(ARCHIVE_AFTER)
        if value is not None:
            try:
                return archive_hours(value) or 0.0
            except ValueError:
                return 0.0  # unreadable: do not archive
    return ARCHIVE_AFTER_DEFAULT


def clean(data: Any) -> dict[str, Any]:
    """Validate a settings document and drop empty values. Raises ValueError."""
    if not isinstance(data, dict):
        raise ValueError("settings must be an object")
    result: dict[str, Any] = {}
    for tier, per_provider in data.items():
        if tier == ARCHIVE_AFTER:
            hours = archive_hours(per_provider)
            if hours is not None:
                result[ARCHIVE_AFTER] = int(hours) if hours.is_integer() else hours
            continue
        if tier not in TIERS:
            raise ValueError(f"unknown tier '{tier}' (use: {', '.join(TIERS)})")
        for provider_name, values in (per_provider or {}).items():
            try:
                provider = providers.get(provider_name)
            except KeyError as exc:
                raise ValueError(exc.args[0]) from None
            kept = {
                key: str(values[key]).strip()
                for key in FIELDS
                if str((values or {}).get(key) or "").strip()
            }
            where = f"{tier}/{provider_name}"
            if "model" in kept:
                try:
                    check_model(kept["model"])
                except ValueError as exc:
                    raise ValueError(f"{where}: {exc}") from None
            effort = kept.get("effort")
            if effort and effort not in provider.efforts:
                raise ValueError(
                    f"{where}: {provider_name} has no effort '{effort}'"
                    f" (use: {', '.join(provider.efforts)})"
                )
            if kept:
                result.setdefault(tier, {})[provider_name] = kept
    return result


def resolve(store: Store, agent: Agent, role: Role) -> dict[str, str | None]:
    """The model and effort an agent's next turn runs with."""
    tier = tier_of(agent, role)
    scopes = [store.get_settings(agent.project)] if agent.project else []
    scopes.append(store.get_settings(GLOBAL))
    resolved: dict[str, str | None] = {}
    for key in FIELDS:
        value = getattr(agent, key)
        for scope in scopes:
            value = value or scope.get(tier, {}).get(agent.provider, {}).get(key)
        resolved[key] = value or (role.model if key == "model" else None)
    return resolved
