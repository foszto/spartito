"""Agent capabilities for the interfaces (capabilities.py has the registry and the
resolution): the listing with each one's status, setting them globally, per project and per
role, an agent's own setting, and who may give which.

Who may turn a capability on for an agent (its own setting, at spawn_agent or send_message,
or the command run in an agent's turn): the owner's surfaces (CLI, dashboard, the interactive
session: no granting agent) and the orchestrator agent (the one agent without a project) may
give any; any other agent only one its own next turn has, and not to an agent whose own
setting turned it off. Turning one off is always allowed, and so is removing a setting
(inherit) unless that setting is an off: removing an off is turning on as well. Nobody turns
on what the agent's role turns off. Only the owner and the orchestrator agent turn one on, or
remove an off, globally, for a project or for a role.

MCP servers (`mcp:<name>`, core/mcp.py) follow the same rules; adding and removing one is the
owner's alone.

A guard rail against agents doing it by accident or on a page's instruction, not a sandbox:
an agent with a shell can still write the home's database or call the dashboard's API.
"""

from __future__ import annotations

import json
from typing import Any, Protocol

from .. import capabilities, config, settings
from .. import mcp as mcp_servers
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


def _known(name: Any, store: Store) -> capabilities.Capability:
    try:
        return capabilities.known(capabilities.check_name(name), store)
    except ValueError as exc:
        raise OrchestratorError(str(exc)) from None


def capability_list(
    orch: Facade, project: str | None = None, wait: bool = False
) -> list[dict[str, Any]]:
    """Every registered capability: what it is, the providers that support it, its status
    for this process (capabilities.status; from what the provider check knows, starting
    none; with `wait` it checks the versions of what it does not know yet), the default, the global setting (`global`: true, false,
    null: inherit) and the global role settings (`roles`: {role: bool}). With `project` also
    `project` and `project_roles`, that project's. `kind`: "builtin", or "mcp" for an MCP
    server's, which also has `server`, `origin`, `transport`, `definition` (as shown:
    mcp.Server.shown), `own_reaches` (the owner's line, or null), `tools` (null: all) and
    `last_status` (what the latest turn that had it reported, or null)."""
    name = orch.project(project).name if project else None
    globally = orch.store.get_settings(settings.GLOBAL)
    scoped = orch.store.get_settings(name) if name else {}
    found = availability.listing(models=False) if wait else availability.checked()
    servers = {server.capability: server for server in mcp_servers.servers(orch.store)}
    statuses = _latest_statuses(orch.store) if servers else {}
    entries = []
    for capability in capabilities.registry(orch.store).values():
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
            "kind": capability.kind,
        }
        if server := servers.get(capability.name):
            entry |= {
                "server": server.name,
                "origin": server.origin,
                "transport": server.transport,
                "definition": server.shown(),
                "own_reaches": server.own_reaches,
                "tools": None,
                "last_status": statuses.get(server.name),
            }
        if name:
            own = capabilities.flags(scoped.get(settings.CAPABILITIES))
            entry["project"] = own.get(capability.name)
            entry["project_roles"] = _roles(scoped, capability.name)
        entries.append(entry)
    return entries


def _latest_statuses(store: Store) -> dict[str, str]:
    """Per MCP server, the status the latest turn that reported one for it recorded."""
    found: dict[str, str] = {}
    for stored in store.recent_run_capabilities('"mcp"'):
        try:
            record = json.loads(stored)
        except ValueError:
            continue
        for server, status in mcp_servers.last_statuses(record).items():
            found.setdefault(server, status)
    return found


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
    granter: Agent | None = None,
) -> list[dict[str, Any]]:
    """Turn a capability on (True) or off (False) globally or for `project`, with `role`
    for that role in that scope; None removes the setting (inherit). Applies from each
    agent's next turn. `granter`: the agent that asks (None: the owner); only the
    orchestrator agent may turn one on. Unreadable entries of the scope's capability
    settings are dropped (as reading ignores them). Returns capability_list(project)."""
    _flag(state, name)
    if state is not None or name not in _stored_names(orch, project):
        _known(name, orch.store)
    scope = orch.project(project).name if project else settings.GLOBAL
    if role is not None and (not isinstance(role, str) or not role.strip()):
        raise OrchestratorError("role must be a role name")
    where = f"for role {role.strip()}" if role else "for this project" if project else "globally"
    if state and _agent(granter):
        raise OrchestratorError(
            f"{granter.id} cannot turn {name} on {where}: only the owner or the orchestrator"
            " turns a capability on globally, for a project or for a role"
        )
    data = capabilities.readable(orch.store.get_settings(scope))
    if role:
        per_role = data.get(settings.ROLE_CAPABILITIES)
        per_role = dict(per_role) if isinstance(per_role, dict) else {}
        values = capabilities.role_flags(per_role, role.strip())
    else:
        values = capabilities.flags(data.get(settings.CAPABILITIES))
    if state is None and values.get(name) is False and _agent(granter):
        raise OrchestratorError(
            f"{granter.id} cannot lift the off of {name} {where}: only the owner or the"
            " orchestrator removes an off globally, for a project or for a role"
        )
    if role:
        per_role[role.strip()] = _changed(values, name, state)
        data[settings.ROLE_CAPABILITIES] = per_role
    else:
        data[settings.CAPABILITIES] = _changed(values, name, state)
    orch.update_settings(data, project)
    return orch.capability_list(project)


def _agent(granter: Agent | None) -> bool:
    """True for a granting agent other than the orchestrator (the one without a project)."""
    return granter is not None and granter.project is not None


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
    """What the agent's next turn gets (capabilities.Resolution.breakdown), with the MCP
    servers' statuses at its last turn; works for an agent not stored yet (the orchestrator
    before its first use)."""
    return capabilities.resolve(orch.store, agent).breakdown(last_statuses(orch.store, agent))


def last_statuses(store: Store, agent: Agent) -> dict[str, str]:
    """The MCP servers' statuses the agent's last recorded turn reported."""
    meta = store.meta_of(agent.id)
    return capabilities.last_statuses(meta[run_id].capabilities for run_id in sorted(meta))


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
    before = dict(own)
    role: dict[str, tuple[bool, str]] | None = None
    granter_has: list[str] | None = None
    for name, value in changes.items():
        _flag(value, name)
        if value is None:
            try:
                capabilities.check_name(name)
            except ValueError as exc:
                raise OrchestratorError(str(exc)) from None
            if _agent(granter) and before.get(name) is False:
                raise OrchestratorError(
                    f"cannot lift the off of {name} for {agent.id}: it was turned off for it,"
                    " and only the owner or the orchestrator can lift it"
                )
            own.pop(name, None)
            continue
        _known(name, orch.store)
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
            if _agent(granter) and before.get(name) is False:
                raise OrchestratorError(
                    f"cannot give {name}: it was turned off for {agent.id}, and only the owner"
                    " or the orchestrator can turn it on again"
                )
            if _agent(granter):
                if granter_has is None:
                    granter_has = on_names(orch, granter)
                if name not in granter_has:
                    raise OrchestratorError(
                        f"cannot give {name}: {granter.id} does not have it itself, and an"
                        " agent may give only the capabilities its own turns have"
                    )
        own[name] = value
    return own


def unsupported_note(provider: str, own: dict[str, bool], store: Store | None = None) -> str | None:
    """What a new agent's own setting turns on that its provider does not support (with a
    store, its MCP servers count too)."""
    entries = capabilities.registry(store)
    names = [
        name
        for name, on in own.items()
        if on and name in entries and provider not in entries[name].providers
    ]
    if not names:
        return None
    return (
        f"{provider} does not support {', '.join(names)}: kept on the agent and skipped"
        " in its turns"
    )
