"""Agent capabilities for the interfaces (capabilities.py has the registry and the
resolution): the listing with each one's status, setting them globally, per project and per
role, an agent's own setting, and who may give which.

Who may turn a capability on for an agent (its own setting, at spawn_agent or send_message):
the owner's surfaces (CLI, dashboard, the interactive session: no granting agent) and the
orchestrator agent (the one agent without a project) may give any; any other agent only one its own next turn has. Turning
one off or dropping it is always allowed. Nobody turns on what the agent's role turns off.
"""

from __future__ import annotations

import json
from typing import Any, Protocol

from .. import capabilities, config, settings
from ..errors import OrchestratorError
from ..models import Agent, Project
from ..store import Store
from . import availability


class Facade(Protocol):
    """What these functions use of the facade (they are its methods)."""

    store: Store

    def project(self, name: str) -> Project: ...

    def agent(self, agent_id: str) -> Agent: ...

    def orchestrator(self, provider: str | None = None) -> Agent: ...

    def _agent_or_orchestrator_draft(self, agent_id: str, wait: bool = True) -> Agent: ...

    def update_settings(self, data: Any, project: str | None = None) -> dict[str, Any]: ...

    def capability_list(self, project: str | None = None) -> list[dict[str, Any]]: ...


def _flag(value: Any, name: str) -> bool | None:
    if value is not None and not isinstance(value, bool):
        raise OrchestratorError(f"{name}: expected true, false or null (inherit)")
    return value


def _known(name: Any) -> capabilities.Capability:
    try:
        return capabilities.known(capabilities.check_name(name))
    except ValueError as exc:
        raise OrchestratorError(str(exc)) from None


def capability_list(
    orch: Facade, project: str | None = None, wait: bool = False
) -> list[dict[str, Any]]:
    """Every registered capability: what it is, the providers that support it, its status
    for this process (capabilities.status; from what the provider check knows, starting
    none; with `wait` it checks the versions of what it does not know yet), the default, the global setting (`global`: true, false,
    null: inherit) and the global role settings (`roles`: {role: bool}). With `project` also
    `project` and `project_roles`, that project's."""
    name = orch.project(project).name if project else None
    globally = orch.store.get_settings(settings.GLOBAL)
    scoped = orch.store.get_settings(name) if name else {}
    found = availability.listing(models=False) if wait else availability.checked()
    entries = []
    for capability in capabilities.REGISTRY.values():
        status, reason = capabilities.status(capability, found)
        entry: dict[str, Any] = {
            "name": capability.name,
            "label": capability.label,
            "description": capability.description,
            "reaches": capability.reaches,
            "requires": list(capability.requires),
            "providers": list(capability.providers),
            "status": status,
            "reason": reason,
            "default": capability.name in capabilities.DEFAULT_ON,
            "global": capabilities.flags(globally.get(settings.CAPABILITIES)).get(capability.name),
            "roles": _roles(globally, capability.name),
            "plugin": capability.name in capabilities.PLUGINS,
            "docs": capability.docs,
        }
        if name:
            own = capabilities.flags(scoped.get(settings.CAPABILITIES))
            entry["project"] = own.get(capability.name)
            entry["project_roles"] = _roles(scoped, capability.name)
        entries.append(entry)
    return entries


def _roles(data: dict[str, Any], name: str) -> dict[str, bool]:
    per_role = data.get(settings.ROLE_CAPABILITIES)
    if not isinstance(per_role, dict):
        return {}
    found = {role: capabilities.role_flags(per_role, role).get(name) for role in per_role}
    return {role: value for role, value in sorted(found.items()) if value is not None}


def set_capability(
    orch: Facade,
    name: str,
    state: bool | None,
    project: str | None = None,
    role: str | None = None,
) -> list[dict[str, Any]]:
    """Turn a capability on (True) or off (False) globally or for `project`, with `role`
    for that role in that scope; None removes the setting (inherit). Applies from each
    agent's next turn. Returns capability_list(project)."""
    _flag(state, name)
    if state is not None or name not in _stored_names(orch, project):
        _known(name)
    scope = orch.project(project).name if project else settings.GLOBAL
    if role is not None and (not isinstance(role, str) or not role.strip()):
        raise OrchestratorError("role must be a role name")
    data = orch.store.get_settings(scope)
    if role:
        per_role = data.get(settings.ROLE_CAPABILITIES)
        per_role = dict(per_role) if isinstance(per_role, dict) else {}
        values = capabilities.role_flags(per_role, role.strip())
        per_role[role.strip()] = _changed(values, name, state)
        data[settings.ROLE_CAPABILITIES] = per_role
    else:
        values = capabilities.flags(data.get(settings.CAPABILITIES))
        data[settings.CAPABILITIES] = _changed(values, name, state)
    orch.update_settings(data, project)
    return orch.capability_list(project)


def _stored_names(orch: Facade, project: str | None) -> set[str]:
    """Names some setting of the scope mentions (one may be of a plugin that is gone: it
    can still be removed)."""
    data = orch.store.get_settings(orch.project(project).name if project else settings.GLOBAL)
    names = set(capabilities.flags(data.get(settings.CAPABILITIES)))
    per_role = data.get(settings.ROLE_CAPABILITIES)
    for role in per_role if isinstance(per_role, dict) else ():
        names |= set(capabilities.role_flags(per_role, role))
    return names


def _changed(values: dict[str, bool], name: str, state: bool | None) -> dict[str, bool]:
    values = dict(values)
    if state is None:
        values.pop(name, None)
    else:
        values[name] = state
    return values


def breakdown(orch: Facade, agent: Agent) -> dict[str, Any]:
    """What the agent's next turn gets (capabilities.Resolution.breakdown); works for an
    agent not stored yet (the orchestrator before its first use)."""
    return capabilities.resolve(orch.store, agent).breakdown()


def on_names(orch: Facade, agent: Agent) -> list[str]:
    """The names the agent's next turn has, for listings."""
    return capabilities.resolve(orch.store, agent).on


def agent_capabilities(orch: Facade, agent_id: str) -> dict[str, Any]:
    return breakdown(orch, orch.agent(agent_id))


def set_agent_capabilities(
    orch: Facade, agent_id: str, changes: Any, granter: Agent | None = None
) -> dict[str, Any]:
    """Change the agent's own setting: {name: True|False|None}, None removes the name
    (inherit), a name not given is kept. `granter`: the agent that asks (None: the owner).
    The orchestrator is created on first use. Applies from its next turn; returns its
    breakdown."""
    agent = orch._agent_or_orchestrator_draft(agent_id)
    if orch.store.get_agent(agent.id) is None:  # the orchestrator's draft
        agent = orch.orchestrator()
    own = granted(orch, agent, changes, granter)
    orch.store.set_agent_capabilities(agent.id, own)
    agent.capabilities = json.dumps(own) if own else None
    return breakdown(orch, agent)


def granted(
    orch: Facade, agent: Agent, changes: Any, granter: Agent | None = None
) -> dict[str, bool]:
    """The agent's own setting after `changes` ({name: True|False|None}), or
    OrchestratorError when one of them may not be made (see the module's doc). `agent` may
    be a draft of one not created yet."""
    if not isinstance(changes, dict):
        raise OrchestratorError("expected an object of capability names and true, false or null")
    own = capabilities.flags(agent.capabilities)
    role: dict[str, tuple[bool, str]] | None = None
    granter_has: list[str] | None = None
    for name, value in changes.items():
        _flag(value, name)
        if value is None:
            try:
                own.pop(capabilities.check_name(name), None)
            except ValueError as exc:
                raise OrchestratorError(str(exc)) from None
            continue
        _known(name)
        if value:
            if role is None:
                role = capabilities.role_values(orch.store, agent.project, agent.role)
            if name in role and role[name][0] is False:
                where = f" --project {role[name][1]}" if role[name][1] else ""
                raise OrchestratorError(
                    f"{name} is off for role {agent.role}, and an agent's own setting cannot"
                    f" turn it on; change the role's: `{config.CLI_NAME} capabilities on {name}"
                    f" --role {agent.role}{where}`"
                )
            if granter is not None and granter.project is not None:  # not the orchestrator
                if granter_has is None:
                    granter_has = on_names(orch, granter)
                if name not in granter_has:
                    raise OrchestratorError(
                        f"cannot give {name}: {granter.id} does not have it itself, and an"
                        " agent may give only the capabilities its own turns have"
                    )
        own[name] = value
    return own


def unsupported_note(provider: str, own: dict[str, bool]) -> str | None:
    """What a new agent's own setting turns on that its provider does not support."""
    names = [
        name
        for name, on in own.items()
        if on
        and name in capabilities.REGISTRY
        and provider not in capabilities.REGISTRY[name].providers
    ]
    if not names:
        return None
    return (
        f"{provider} does not support {', '.join(names)}: kept on the agent and skipped"
        " in its turns"
    )
