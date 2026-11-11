"""An agent's model and effort: the model/effort settings (global, per project), what an
agent's next turn uses, its own overrides, and what a switch does to the prompt cache."""

from __future__ import annotations

from dataclasses import replace
from typing import Any, Protocol

from .. import providers, roles, settings
from ..errors import OrchestratorError
from ..models import Agent, Project
from ..store import Store
from .dispatch import ORCHESTRATOR_ID


class Facade(Protocol):
    """What these functions use of the facade (they are its methods)."""

    store: Store

    def project(self, name: str) -> Project: ...

    def agent(self, agent_id: str) -> Agent: ...

    def effective(self, agent: Agent) -> dict[str, str | None]: ...

    def inherited(self, agent: Agent) -> dict[str, str | None]: ...

    def setup(self, agent_id: str) -> dict[str, Any]: ...

    def _agent_or_orchestrator_draft(self, agent_id: str) -> Agent: ...

    def orchestrator(self) -> Agent: ...

    @staticmethod
    def _orchestrator_draft() -> Agent: ...


# What a model or effort switch in a resumed session does to the prompt cache. Measured
# with real turns: a model switch re-wrote Claude's cache on --resume, and codex-cli
# 0.155.1 read 0 cached tokens after `-m` changed on `exec resume`; an effort switch alone
# kept Codex's cache (12k of 14k tokens cached). Claude's effort switch was not measured.
RESTARTS, MAY_RESTART, KEEPS = "restarts", "may restart", "keeps"
CACHE_EFFECT = {
    "claude": {"model": RESTARTS, "effort": MAY_RESTART},
    "codex": {"model": RESTARTS, "effort": KEEPS},
}
CACHE_EFFECT_UNKNOWN = {"model": MAY_RESTART, "effort": MAY_RESTART}
CACHE_WORDS = {
    RESTARTS: "starts a fresh prompt cache",
    MAY_RESTART: "may start a fresh prompt cache",
    KEEPS: "keeps the prompt cache",
}


def cache_effect(provider: str) -> dict[str, str]:
    return CACHE_EFFECT.get(provider, CACHE_EFFECT_UNKNOWN)


def cache_hint(provider: str, keys: tuple[str, ...] = ("model", "effort")) -> str:
    """What switching these settings in an existing session does to the prompt cache."""
    effect = cache_effect(provider)
    text = "; ".join(f"switching the {key} {CACHE_WORDS[effect[key]]}" for key in keys)
    text = text[0].upper() + text[1:] + "."
    if any(effect[key] != KEEPS for key in keys):
        text += " With a fresh cache the next turn re-reads the whole conversation at full price."
    return text


def stored_settings(orch: Facade, project: str | None = None) -> dict[str, Any]:
    return orch.store.get_settings(orch.project(project).name if project else settings.GLOBAL)


def update_settings(orch: Facade, data: Any, project: str | None = None) -> dict[str, Any]:
    """Replace the model/effort settings globally or, with `project`, for one project."""
    try:
        cleaned = settings.clean(data)
    except ValueError as exc:
        raise OrchestratorError(str(exc)) from None
    if project and "orchestrator" in cleaned:
        raise OrchestratorError("the orchestrator tier can only be set globally")
    for key in settings.SWITCHES:
        if project and key in cleaned:
            raise OrchestratorError(f"{key} can only be set globally")
    scope = orch.project(project).name if project else settings.GLOBAL
    # Kept when not named (the settings dialog sends only its own fields; a page from
    # before a key existed does not send that one).
    kept = (settings.ARCHIVE_AFTER, settings.COMPACT_AT)
    for key in kept if project else kept + settings.SWITCHES:
        if isinstance(data, dict) and key not in data:
            stored = orch.store.get_settings(scope).get(key)
            cleaned.update({} if stored is None else {key: stored})
    orch.store.set_settings(scope, cleaned)
    return cleaned


def effective(orch: Facade, agent: Agent) -> dict[str, str | None]:
    """Model and effort the agent's next turn will use."""
    project = orch.store.get_project(agent.project) if agent.project else None
    try:
        role = roles.load(agent.role, project.path if project else None)
    except KeyError:  # the role file was removed; the next turn would fail anyway
        return {"model": agent.model, "effort": agent.effort}
    return settings.resolve(orch.store, agent, role)


def inherited(orch: Facade, agent: Agent) -> dict[str, str | None]:
    """Model and effort the agent's next turn would use without its own values."""
    return orch.effective(replace(agent, model=None, effort=None))


def setup(orch: Facade, agent_id: str) -> dict[str, Any]:
    """An agent's model and effort: what its next turn uses (`effective`), the values
    set on the agent itself (`model_override`/`effort_override`; null: inherited) and
    what applies without them (`inherited`); `running`: a turn is running (a change
    applies from the next one); `cache_effect`: per setting, what switching it in an
    existing session does to the prompt cache (`restarts`, `may restart`, `keeps`) and
    `cache_hint` says it in words. The orchestrator is shown before its first use too
    (`exists` false), without creating anything."""
    agent = orch._agent_or_orchestrator_draft(agent_id)
    return {
        "agent_id": agent.id,
        "provider": agent.provider,
        "exists": orch.store.get_agent(agent.id) is not None,
        "effective": orch.effective(agent),
        "model_override": agent.model,
        "effort_override": agent.effort,
        "inherited": orch.inherited(agent),
        "session_id": agent.session_id,
        "running": orch.store.running_run(agent.id) is not None,
        "cache_effect": cache_effect(agent.provider),
        "cache_hint": cache_hint(agent.provider),
    }


def set_overrides(orch: Facade, agent_id: str, changes: dict[str, Any]) -> dict[str, Any]:
    """Set or clear an agent's own model and/or effort; returns its `setup`.

    `changes` holds `model` and/or `effort`; a string sets the value, None clears it
    (back to the tier default), a missing key keeps it. The values are resolved when a
    turn starts, so a running turn is not affected; the next one uses them. The
    orchestrator is created on first use, like a message to it does.
    """
    if not isinstance(changes, dict):
        raise OrchestratorError("expected an object with 'model' and/or 'effort'")
    unknown = sorted(set(changes) - set(settings.FIELDS))
    if unknown:
        raise OrchestratorError(f"cannot set {', '.join(unknown)} (use: model, effort)")
    if not changes:
        raise OrchestratorError("nothing to change: give 'model' and/or 'effort'")
    agent = orch._agent_or_orchestrator_draft(agent_id)
    if changes.get("model") is not None:
        try:
            settings.check_model(changes["model"])
        except ValueError as exc:
            raise OrchestratorError(str(exc)) from None
    if "effort" in changes:
        effort = changes["effort"]
        try:
            allowed = providers.get(agent.provider).efforts
        except KeyError as exc:
            raise OrchestratorError(str(exc.args[0])) from None
        if effort is not None and effort not in allowed:
            raise OrchestratorError(
                f"{agent.provider} has no effort {effort!r} (use: {', '.join(allowed)})"
            )
    if agent.id == ORCHESTRATOR_ID:
        orch.orchestrator()
    orch.store.set_overrides(agent.id, changes)
    return orch.setup(agent.id)


def _agent_or_orchestrator_draft(orch: Facade, agent_id: str) -> Agent:
    if agent_id == ORCHESTRATOR_ID:
        return orch.store.get_agent(ORCHESTRATOR_ID) or orch._orchestrator_draft()
    return orch.agent(agent_id)
