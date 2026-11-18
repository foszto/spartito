"""An agent's model and effort: the model/effort settings (global, per project), what an
agent's next turn uses, its own overrides, and what a switch does to the prompt cache."""

from __future__ import annotations

from dataclasses import replace
from typing import Any, Protocol

from .. import capabilities, providers, roles, settings
from ..errors import NeedsConfirmation, OrchestratorError
from ..models import Agent, Project
from ..store import Store
from . import availability, grants
from .dispatch import ORCHESTRATOR_ID


class Facade(Protocol):
    """What these functions use of the facade (they are its methods)."""

    store: Store

    def project(self, name: str) -> Project: ...

    def agent(self, agent_id: str) -> Agent: ...

    def effective(self, agent: Agent) -> dict[str, str | None]: ...

    def inherited(self, agent: Agent) -> dict[str, str | None]: ...

    def setup(self, agent_id: str, wait: bool = True) -> dict[str, Any]: ...

    def _agent_or_orchestrator_draft(self, agent_id: str, wait: bool = True) -> Agent: ...

    def orchestrator(self, provider: str | None = None) -> Agent: ...

    @staticmethod
    def _orchestrator_draft(provider: str | None = None, wait: bool = True) -> Agent: ...


# What a model or effort switch in a resumed session does to the prompt cache: each
# provider's `cache_effect`, else CACHE_EFFECT_UNKNOWN. Measured with real turns: a model
# switch re-wrote Claude's cache on --resume, and codex-cli 0.155.1 read 0 cached tokens
# after `-m` changed on `exec resume`; an effort switch alone kept Codex's cache (12k of 14k
# tokens cached). Claude's effort switch was not measured.
RESTARTS, MAY_RESTART, KEEPS = "restarts", "may restart", "keeps"
CACHE_EFFECT_UNKNOWN = {"model": MAY_RESTART, "effort": MAY_RESTART}
CACHE_WORDS = {
    RESTARTS: "starts a fresh prompt cache",
    MAY_RESTART: "may start a fresh prompt cache",
    KEEPS: "keeps the prompt cache",
}


def cache_effect(provider: str) -> dict[str, str]:
    known = providers.PROVIDERS.get(provider)
    return {**CACHE_EFFECT_UNKNOWN, **(getattr(known, "cache_effect", None) or {})}


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
    scope = orch.project(project).name if project else settings.GLOBAL
    stored = orch.store.get_settings(scope)
    try:
        cleaned = settings.clean(_as_stored(data, stored))
    except ValueError as exc:
        raise OrchestratorError(str(exc)) from None
    if project and "orchestrator" in cleaned:
        raise OrchestratorError("the orchestrator tier can only be set globally")
    for key in settings.SWITCHES:
        if project and key in cleaned:
            raise OrchestratorError(f"{key} can only be set globally")
    _registered(cleaned, stored, orch.store)
    # Kept when not named (the settings dialog sends only its own fields; a page from
    # before a key existed does not send that one).
    kept = (
        settings.ARCHIVE_AFTER,
        settings.COMPACT_AT,
        settings.CAPABILITIES,
        settings.ROLE_CAPABILITIES,
    )
    readable = capabilities.readable(stored)
    for key in kept if project else kept + settings.SWITCHES:
        if isinstance(data, dict) and key not in data and readable.get(key) not in (None, {}):
            cleaned[key] = readable[key]
    orch.store.set_settings(scope, cleaned)
    return cleaned


def _as_stored(data: Any, stored: dict[str, Any]) -> Any:
    """The request, with the capability settings it sends back as stored (a command that
    read the whole scope and writes it) reduced to their readable entries, so that an
    unreadable stored one blocks no write; what the request changes is checked as sent."""
    if not isinstance(data, dict):
        return data
    data = dict(data)
    readable = capabilities.readable(stored)
    for key in (settings.CAPABILITIES, settings.ROLE_CAPABILITIES):
        if key in stored and key in data and data[key] == stored[key]:
            data[key] = readable[key]
    key, before = settings.ROLE_CAPABILITIES, stored.get(settings.ROLE_CAPABILITIES)
    if isinstance(data.get(key), dict) and isinstance(before, dict):
        sent = dict(data[key])
        for role in [role for role in sent if role in before and sent[role] == before[role]]:
            if role in readable[key]:
                sent[role] = readable[key][role]
            else:
                del sent[role]
        data[key] = sent
    return data


def _registered(cleaned: dict[str, Any], stored: dict[str, Any], store: Store) -> None:
    """Refuse a capability setting the request names for a capability that is not registered
    (capabilities.registry, MCP servers included; it would be skipped on every turn); one
    stored with the same value already (a plugin or an MCP server that is gone) stays, and
    removing one is always possible."""
    entries = capabilities.registry(store)
    stored_roles = stored.get(settings.ROLE_CAPABILITIES)
    pairs = [
        (
            cleaned.get(settings.CAPABILITIES, {}),
            capabilities.flags(stored.get(settings.CAPABILITIES)),
        )
    ]
    for role, values in cleaned.get(settings.ROLE_CAPABILITIES, {}).items():
        pairs.append((values, capabilities.role_flags(stored_roles, role)))
    for values, before in pairs:
        for name, on in values.items():
            if name not in entries and before.get(name) is not on:
                try:
                    capabilities.known(name, store)
                except ValueError as exc:
                    raise OrchestratorError(str(exc)) from None


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


def setup(orch: Facade, agent_id: str, wait: bool = True) -> dict[str, Any]:
    """An agent's model and effort: what its next turn uses (`effective`), the values
    set on the agent itself (`model_override`/`effort_override`; null: inherited) and
    what applies without them (`inherited`); `running`: a turn is running (a change
    applies from the next one); `cache_effect`: per setting, what switching it in an
    existing session does to the prompt cache (`restarts`, `may restart`, `keeps`) and
    `cache_hint` says it in words; `capabilities`: what its next turn gets of them
    (capabilities.Resolution.breakdown). The orchestrator is shown before its first use too
    (`exists` false), without creating anything; `wait=False` shows it on the provider it
    would get without waiting for an availability check. The replies of set_overrides and
    set_provider add `note` when they created the orchestrator on a fallback provider."""
    if wait:
        agent = orch._agent_or_orchestrator_draft(agent_id)
    else:
        agent = orch._agent_or_orchestrator_draft(agent_id, wait=False)
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
        "capabilities": grants.breakdown(orch, agent),
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
    note = orch.orchestrator().provider_note if agent.id == ORCHESTRATOR_ID else None
    orch.store.set_overrides(agent.id, changes)
    return _noting(orch.setup(agent.id), note)


def _noting(setup: dict[str, Any], note: str | None) -> dict[str, Any]:
    """`setup` with `note` when the call created the orchestrator on a fallback provider (what
    choosing it had to tell); without the key otherwise."""
    return {**setup, "note": note} if note else setup


def _agent_or_orchestrator_draft(orch: Facade, agent_id: str, wait: bool = True) -> Agent:
    if agent_id == ORCHESTRATOR_ID:
        return orch.store.get_agent(ORCHESTRATOR_ID) or orch._orchestrator_draft(wait=wait)
    return orch.agent(agent_id)


def set_provider(
    orch: Facade, agent_id: str, provider: str, confirm: bool = False
) -> dict[str, Any]:
    """Move an agent to another provider; returns its `setup`.

    The orchestrator before its first use is created on it. An existing agent: the provider
    must be usable here (the same one changes nothing); refused while the agent has a
    running or queued turn. A conversation cannot move between providers: the agent's
    session is kept for the provider it leaves, and the next message continues the one
    kept for the new provider (from an earlier move away from it) or starts a new one.
    With a session that needs `confirm` (else NeedsConfirmation says what happens). The
    agent's own model and effort are cleared (they were the old provider's); its worktree,
    branch, board items and sub-agents stay."""
    if agent_id == ORCHESTRATOR_ID and orch.store.get_agent(ORCHESTRATOR_ID) is None:
        note = orch.orchestrator(provider).provider_note
        return _noting(orch.setup(ORCHESTRATOR_ID), note)
    agent = orch.agent(agent_id)
    if provider == agent.provider:
        return orch.setup(agent.id)
    availability.choose(provider)  # registered and usable, or OrchestratorError
    if orch.store.active_runs(agent.id):
        raise OrchestratorError(busy(agent))
    if agent.session_id and not confirm:
        then = next_message(provider, orch.store.provider_sessions(agent.id).get(provider))
        raise NeedsConfirmation(
            f"{agent.id} has a conversation on {agent.provider}: {then}; the one on"
            f" {agent.provider} is kept and resumed when it moves back"
        )
    _move(orch, agent, provider)
    return orch.setup(agent.id)


def busy(agent: Agent) -> str:
    return f"{agent.id} has a running or queued turn: stop it or wait, then move it"


def next_message(provider: str, session_id: str | None) -> str:
    """What the next message does after a move to `provider`, where `session_id` (None:
    nothing) is the conversation kept for it."""
    if session_id:
        return f"the next message continues the earlier conversation on {provider} (session {session_id})"
    return f"the next message starts a new conversation on {provider}"


def _move(orch: Facade, agent: Agent, provider: str) -> None:
    """The one place an agent changes provider (Store.move_provider, one transaction): its
    session is kept for the provider it leaves, the one kept for `provider` (if any) becomes
    its session, its own model and effort go; the log notes it on the latest run."""
    moved, resumed = orch.store.move_provider(agent.id, provider)
    if not moved:
        raise OrchestratorError(busy(agent))
    last = orch.store.last_run(agent.id)
    if last is None:
        return
    text = f"moved from {agent.provider} to {provider}: {next_message(provider, resumed)}"
    if agent.model or agent.effort:
        text += "; the agent's own model and effort were cleared"
    if agent.session_id:
        text += (
            f"; the conversation on {agent.provider} (session {agent.session_id}) is kept and"
            " resumed when it moves back"
        )
    orch.store.add_event(last.id, agent.id, "info", text)
