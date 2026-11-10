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


def tier_of(agent: Agent, role: Role) -> str:
    if agent.project is None:
        return "orchestrator"
    return "lead" if role.lead else "worker"


def clean(data: Any) -> dict[str, dict[str, dict[str, str]]]:
    """Validate a settings document and drop empty values. Raises ValueError."""
    if not isinstance(data, dict):
        raise ValueError("settings must be an object")
    result: dict[str, dict[str, dict[str, str]]] = {}
    for tier, per_provider in data.items():
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
            effort = kept.get("effort")
            if effort and effort not in provider.efforts:
                raise ValueError(
                    f"{provider_name} has no effort '{effort}' (use: {', '.join(provider.efforts)})"
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
