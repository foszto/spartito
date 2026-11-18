"""MCP servers added in Spartito for the interfaces (mcp.py has the entries and their checks,
capabilities.py turns each into the capability `mcp:<name>`): the list with each one's
levels, one server, adding and removing.

Adding and removing are the owner's alone: refused for every agent, the orchestrator agent
included, since they change what code runs and with whose credentials. Turning a server on,
off or back to inherit goes through the capability operations (core/grants.py) with the name
`mcp:<name>`, under their rules.

Removing a server also removes every level that names it (global, each project, their roles,
each agent's own setting), so that a later server of the same name starts with none; adding
one removes any such level left behind as well (a level written by older code, or a removal
that stopped half way).
"""

from __future__ import annotations

import json
from typing import Any, Protocol

from .. import capabilities, config, settings
from .. import mcp as mcp_servers
from ..errors import OrchestratorError
from ..models import Agent, Project
from ..store import Store


class Facade(Protocol):
    """What these functions use of the facade (they are its methods)."""

    store: Store

    def project(self, name: str) -> Project: ...

    def capability_list(self, project: str | None = None) -> list[dict[str, Any]]: ...

    def mcp_show(self, name: str, project: str | None = None) -> dict[str, Any]: ...


def listing(orch: Facade, project: str | None = None) -> list[dict[str, Any]]:
    """Every server, by name: its entry of capability_list(project) (kind "mcp": what it is,
    reaches, the definition as shown, the global, project and role levels, status) with
    `agents`: the agents whose own setting names it, {agent id: true|false}."""
    entries = [
        entry
        for entry in orch.capability_list(project)
        if entry.get("kind") == capabilities.MCP_KIND
    ]
    if not entries:
        return []
    own = _agent_levels(orch.store)
    for entry in entries:
        entry["agents"] = {
            agent_id: values[entry["name"]]
            for agent_id, values in own.items()
            if entry["name"] in values
        }
    return sorted(entries, key=lambda entry: entry["server"])


def show(orch: Facade, name: str, project: str | None = None) -> dict[str, Any]:
    """One server as `listing` gives it; `name`: `docs-db` or `mcp:docs-db`."""
    wanted = mcp_servers.server_name(name) if isinstance(name, str) else name
    found = listing(orch, project)
    for entry in found:
        if entry["server"] == wanted:
            return entry
    added = ", ".join(entry["server"] for entry in found) or "none"
    raise OrchestratorError(f"unknown MCP server {wanted!r} (added: {added})")


def add(
    orch: Facade,
    name: str,
    *,
    command: str | None = None,
    args: Any = (),
    env: Any = None,
    url: str | None = None,
    headers: Any = None,
    reaches: str | None = None,
    granter: Agent | None = None,
) -> dict[str, Any]:
    """Add a server: `command` with `args` and `env` (stdio), or `url` with `headers` (http).
    `env` and `headers`: {name: "${VAR}"}, or strings `NAME=${VAR}` and `Name: ${VAR}`.
    `reaches`: the owner's one line on what it reaches, shown after the generated one.
    `granter`: the agent that asks (None: the owner); any agent is refused. It is off until
    a level turns it on. Returns show(name)."""
    _owner_only(granter, "add")
    try:
        server = mcp_servers.server(
            name, command=command, args=args, env=env, url=url, headers=headers, reaches=reaches
        )
    except ValueError as exc:
        raise OrchestratorError(str(exc)) from None
    if orch.store.get_mcp_server(server.name) is not None:
        raise OrchestratorError(_taken(server.name))
    _clear_levels(orch.store, server.capability)
    try:
        mcp_servers.add(orch.store, server)
    except ValueError:
        raise OrchestratorError(_taken(server.name)) from None
    return orch.mcp_show(server.name)


def remove(orch: Facade, name: str, granter: Agent | None = None) -> dict[str, Any]:
    """Remove a server and every level that names it. `granter`: the agent that asks (None:
    the owner); any agent is refused. Returns the server as show() gave it before, with
    `cleared`: where a level was removed ("global", "project my-app", "role reviewer",
    "role reviewer of project my-app", "agent fix-login-3f2a")."""
    _owner_only(granter, "remove")
    wanted = mcp_servers.server_name(name) if isinstance(name, str) else name
    row = orch.store.get_mcp_server(wanted) if isinstance(wanted, str) else None
    if row is None:
        added = ", ".join(row["name"] for row in orch.store.mcp_servers()) or "none"
        raise OrchestratorError(f"unknown MCP server {wanted!r} (added: {added})")
    try:
        before = orch.mcp_show(wanted)
    except OrchestratorError:  # a row that cannot be read as a server: removed all the same
        before = {"name": mcp_servers.capability_name(wanted), "server": wanted}
    cleared = _clear_levels(orch.store, mcp_servers.capability_name(wanted))
    orch.store.remove_mcp_server(wanted)
    return {**before, "cleared": cleared}


def _owner_only(granter: Agent | None, action: str) -> None:
    if granter is not None:
        raise OrchestratorError(
            f"{granter.id} cannot {action} MCP servers: adding and removing them is the"
            " owner's alone, since they change what code runs and with whose credentials;"
            f" turning one on goes by the capability rules (`{config.CLI_NAME} mcp on NAME`)"
        )


def _taken(name: str) -> str:
    return (
        f"an MCP server named {name!r} exists already; remove it first"
        f" (`{config.CLI_NAME} mcp remove {name}`) or choose another name"
    )


def _agent_levels(store: Store) -> dict[str, dict[str, bool]]:
    """Each agent's own setting, read (unreadable entries count as absent)."""
    found = {agent.id: capabilities.flags(agent.capabilities) for agent in store.list_agents()}
    return {agent_id: values for agent_id, values in found.items() if values}


def _clear_levels(store: Store, name: str) -> list[str]:
    """Remove the capability `name` from the global and every project's settings, their
    role settings and every agent's own setting; returns where it was. Other entries stay as
    stored, readable or not."""
    cleared: list[str] = []
    scopes = [(settings.GLOBAL, None)] + [(p.name, p.name) for p in store.list_projects()]
    for scope, project in scopes:
        data = store.get_settings(scope)
        if not isinstance(data, dict):
            continue
        changed = False
        values = data.get(settings.CAPABILITIES)
        if isinstance(values, dict) and name in values:
            del values[name]
            changed = True
            cleared.append(f"project {project}" if project else "global")
            if not values:
                del data[settings.CAPABILITIES]
        per_role = data.get(settings.ROLE_CAPABILITIES)
        if isinstance(per_role, dict):
            for role, values in list(per_role.items()):
                if isinstance(values, dict) and name in values:
                    del values[name]
                    changed = True
                    cleared.append(
                        f"role {role} of project {project}" if project else f"role {role}"
                    )
                    if not values:
                        del per_role[role]
            if not per_role:
                del data[settings.ROLE_CAPABILITIES]
        if changed:
            store.set_settings(scope, data)
    for agent in store.list_agents():
        try:
            own = json.loads(agent.capabilities) if agent.capabilities else None
        except ValueError:
            continue
        if isinstance(own, dict) and name in own:
            del own[name]
            store.set_agent_capabilities(agent.id, own or None)
            cleared.append(f"agent {agent.id}")
    return cleared
