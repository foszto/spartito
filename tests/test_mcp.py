"""MCP servers added in Spartito: the entries and their checks (mcp.py), the table, the
servers as capabilities `mcp:<name>` (capabilities.registry), who may add, remove and give
them (core/mcp.py, core/grants.py), and what lists and agents see. No CLI is run."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from orchestrator import capabilities, mcp, settings
from orchestrator.errors import OrchestratorError
from orchestrator.models import Agent
from orchestrator.providers import get as provider
from orchestrator.providers.base import RunContext, Support, Wiring
from orchestrator.service import Orchestrator
from orchestrator.store import Store

DB_URL = "${DOCS_DB_URL}"
URL = "https://mcp.example.com/mcp"
STDIO_ENTRY = {"command": "docs-db-mcp", "args": ["--read-only"], "env": {"DOCS_DB_URL": DB_URL}}
HTTP_ENTRY = {"type": "http", "url": URL, "headers": {"Authorization": "${DOCS_TOKEN}"}}


@pytest.fixture(autouse=True)
def no_launch(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """Turns are queued, never run: these tests look at what is stored."""
    launched: list[int] = []
    monkeypatch.setattr(Orchestrator, "_launch", lambda self, run: launched.append(run.id))
    return launched


@pytest.fixture
def store(tmp_path: Path) -> Store:
    return Store(tmp_path / "orch.db")


def docs_db(**changes) -> mcp.Server:
    fields = {"command": "docs-db-mcp", "args": ["--read-only"], "env": {"DOCS_DB_URL": DB_URL}}
    return mcp.server("docs-db", **{**fields, **changes})


def remote(**changes) -> mcp.Server:
    fields = {"url": URL, "headers": {"Authorization": "${DOCS_TOKEN}"}}
    return mcp.server("remote-docs", **{**fields, **changes})


def add_docs_db(orch: Orchestrator, **changes) -> dict:
    fields = {"command": "docs-db-mcp", "args": ["--read-only"], "env": {"DOCS_DB_URL": DB_URL}}
    return orch.mcp_add("docs-db", **{**fields, **changes})


def agent(provider_name: str = "claude", own: dict | None = None, role: str = "worker") -> Agent:
    return Agent(
        id="fix-login-3f2a",
        project="my-app",
        name="fix-login",
        role=role,
        provider=provider_name,
        cwd="/w",
        capabilities=json.dumps(own) if own else None,
    )


# -- entries ------------------------------------------------------------------------------


def test_a_command_server_is_an_entry_with_its_reach() -> None:
    server = docs_db()

    assert (server.name, server.origin, server.transport) == ("docs-db", "spartito", "stdio")
    assert server.capability == "mcp:docs-db"
    assert server.entry() == STDIO_ENTRY
    assert server.shown() == STDIO_ENTRY
    assert server.reaches == (
        "runs docs-db-mcp on this machine as you, with your files, your network and the"
        " environment it is given"
    )


def test_a_url_server_shows_its_origin_only() -> None:
    server = remote(url="https://mcp.example.com:8443/mcp/v1?team=docs")

    assert server.transport == "http"
    assert server.entry() == {**HTTP_ENTRY, "url": "https://mcp.example.com:8443/mcp/v1?team=docs"}
    assert server.shown() == {
        "url": "https://mcp.example.com:8443",
        "headers": {"Authorization": "${DOCS_TOKEN}"},
    }
    assert server.reaches == (
        "sends the agent's tool calls, and what they carry, to https://mcp.example.com:8443,"
        " with the credentials configured for it"
    )


def test_the_owners_line_is_kept_after_the_generated_one() -> None:
    server = docs_db(reaches="  the docs database, read-only  ")

    assert server.own_reaches == "the docs database, read-only"
    assert server.reaches.endswith("the environment it is given; the docs database, read-only")
    assert docs_db(reaches="").own_reaches is None


def test_env_and_headers_may_be_given_as_typed() -> None:
    server = docs_db(env=["DOCS_DB_URL=${DOCS_DB_URL}"], args=("--read-only",))
    other = remote(headers=["Authorization: ${DOCS_TOKEN}"])

    assert server.entry() == STDIO_ENTRY and other.entry() == HTTP_ENTRY
    assert mcp.server("plain", command="docs-db-mcp").entry() == {
        "command": "docs-db-mcp",
        "args": [],
        "env": {},
    }
    assert mcp.server("plain", url=URL).entry() == {"type": "http", "url": URL, "headers": {}}


@pytest.mark.parametrize("name", ["", "a" * 65, "with space", "a.b", "mcp:docs", "naïve", None])
def test_invalid_names_are_refused(name: object) -> None:
    with pytest.raises(ValueError, match="invalid MCP server name"):
        mcp.server(name, command="x")


@pytest.mark.parametrize("name", ["orch", "ORCH"])
def test_the_name_of_spartitos_own_server_is_refused(name: str) -> None:
    with pytest.raises(ValueError, match="Spartito's own MCP server"):
        mcp.server(name, command="x")


@pytest.mark.parametrize("name", mcp.RESERVED)
def test_names_claude_code_reserves_are_refused(name: str) -> None:
    with pytest.raises(ValueError, match="a name Claude Code reserves"):
        mcp.server(name, command="x")


def test_names_with_letters_digits_underscores_and_dashes_are_accepted() -> None:
    assert mcp.server("Docs_DB-2", command="x").capability == "mcp:Docs_DB-2"
    assert mcp.server("a" * 64, command="x").name == "a" * 64


@pytest.mark.parametrize(
    "value", ["postgres://docs", "", "$DOCS_DB_URL", "${DOCS_DB_URL:-x}", "x${DOCS_DB_URL}", 5]
)
def test_a_literal_env_value_is_refused(value: object) -> None:
    with pytest.raises(ValueError) as caught:
        docs_db(env={"DOCS_DB_URL": value})
    assert "not a ${VAR} reference" in str(caught.value)
    assert str(caught.value).endswith(
        "put the value in an environment variable of the service and write ${VAR}"
    )


@pytest.mark.parametrize("value", ["Bearer ${DOCS_TOKEN}", "secret", "${A}${B}"])
def test_a_literal_header_value_is_refused(value: str) -> None:
    with pytest.raises(ValueError, match=r"header Authorization: .* is not a \$\{VAR\}"):
        remote(headers={"Authorization": value})


@pytest.mark.parametrize(
    ("fields", "message"),
    [
        ({"env": {"1X": DB_URL}}, "invalid variable name"),
        ({"env": ["DOCS_DB_URL"]}, "expected 'name=value'"),
        ({"env": "DOCS_DB_URL=${DOCS_DB_URL}"}, "env must be an object"),
        ({"args": "--read-only"}, "args must be a list of strings"),
        ({"args": [1]}, "args must be a list of strings"),
        ({"command": " "}, "command must not be empty"),
        ({"command": "a\nb"}, "command must be one line"),
        ({"headers": {"X-Key": "${K}"}}, "headers are for a URL"),
        ({"url": URL}, "either a command"),
        ({"reaches": "two\nlines"}, "reaches must be one line"),
        ({"reaches": "x" * 301}, "at most 300"),
    ],
)
def test_a_command_server_that_cannot_be_made_is_refused(fields: dict, message: str) -> None:
    with pytest.raises(ValueError, match=re.escape(message)):
        docs_db(**fields)


@pytest.mark.parametrize(
    ("fields", "message"),
    [
        ({"url": "ftp://mcp.example.com"}, "must start with http:// or https://"),
        ({"url": "https://user:pass@mcp.example.com/mcp"}, "a user name or password"),
        ({"url": "https://token@mcp.example.com/mcp"}, "a user name or password"),
        ({"url": "https:///mcp"}, "has no host"),
        ({"url": "https://mcp.example.com/a b"}, "must not contain spaces"),
        ({"url": "https://mcp.example.com:port/"}, "cannot be read"),
        ({"url": ""}, "http or https URL"),
        ({"headers": {"Bad Name": "${K}"}}, "invalid header name"),
        ({"headers": {"X-Key": "${K}", "x-key": "${L}"}}, "given twice"),
        ({"headers": ["X-Key ${K}"]}, "expected 'name:value'"),
        ({"args": ["x"]}, "args and env are for a command"),
        ({"env": {"K": "${K}"}}, "args and env are for a command"),
    ],
)
def test_a_url_server_that_cannot_be_made_is_refused(fields: dict, message: str) -> None:
    with pytest.raises(ValueError, match=re.escape(message)):
        remote(**fields)


def test_a_server_needs_a_command_or_a_url() -> None:
    with pytest.raises(ValueError, match="either a command"):
        mcp.server("docs-db")


# -- storage ------------------------------------------------------------------------------


def test_servers_are_stored_read_and_removed(store: Store) -> None:
    mcp.add(store, docs_db(reaches="the docs database"))
    mcp.add(store, remote())

    assert [server.name for server in mcp.servers(store)] == ["docs-db", "remote-docs"]
    row = store.get_mcp_server("docs-db")
    assert json.loads(row["definition"]) == STDIO_ENTRY
    assert (row["origin"], row["transport"], row["reaches"]) == (
        "spartito",
        "stdio",
        "the docs database",
    )
    assert row["tools"] is None and row["pin"] is None
    assert row["created_at"] == row["updated_at"]
    assert mcp.find(store, "mcp:docs-db") == mcp.find(store, "docs-db")
    with pytest.raises(ValueError, match="exists already"):
        mcp.add(store, docs_db())
    assert store.remove_mcp_server("docs-db") is True
    assert store.remove_mcp_server("docs-db") is False
    assert [server.name for server in mcp.servers(store)] == ["remote-docs"]


@pytest.mark.parametrize(
    ("origin", "transport", "definition"),
    [
        ("claude-user", "stdio", STDIO_ENTRY),  # a later phase's
        ("spartito", "ws", STDIO_ENTRY),
        ("spartito", "stdio", {**STDIO_ENTRY, "env": {"DOCS_DB_URL": "postgres://docs"}}),
        ("spartito", "http", {"url": "https://user:pw@mcp.example.com", "headers": {}}),
        ("spartito", "stdio", ["not", "an", "object"]),
    ],
)
def test_a_row_that_fails_the_checks_is_no_server(
    store: Store, origin: str, transport: str, definition: object
) -> None:
    """A row written around the checks (by hand, by other code) is never wired: unknown."""
    store.add_mcp_server("docs-db", origin, transport, definition, None)  # type: ignore[arg-type]

    assert mcp.servers(store) == [] and "mcp:docs-db" not in capabilities.registry(store)


# -- names and the registry ---------------------------------------------------------------


@pytest.mark.parametrize("name", ["mcp:docs-db", "mcp:Docs_DB", "mcp:" + "a" * 64, "chrome"])
def test_capability_names_take_mcp_servers(name: str) -> None:
    assert capabilities.check_name(name) == name


@pytest.mark.parametrize("name", ["mcp:", "mcp:a b", "mcp:" + "a" * 65, "MCP:docs", "mcp:a:b"])
def test_malformed_mcp_names_are_refused(name: str) -> None:
    with pytest.raises(ValueError, match="invalid capability name"):
        capabilities.check_name(name)


def test_names_with_a_colon_are_read_parsed_and_cleaned() -> None:
    stored = json.dumps({"mcp:docs-db": True, "chrome": False, "mcp:bad name": True})

    assert capabilities.flags(stored) == {"mcp:docs-db": True, "chrome": False}
    assert capabilities.parse("mcp:docs-db, -chrome,-mcp:other") == {
        "mcp:docs-db": True,
        "chrome": False,
        "mcp:other": False,
    }
    assert settings.clean(
        {
            "capabilities": {"mcp:docs-db": True, "chrome": None},
            "role_capabilities": {"reviewer": {"mcp:docs-db": False}},
        }
    ) == {
        "capabilities": {"mcp:docs-db": True},
        "role_capabilities": {"reviewer": {"mcp:docs-db": False}},
    }


def test_the_released_name_rule_reads_mcp_entries_as_absent() -> None:
    """Older code reads names with this rule (capabilities.NAME before MCP servers), and
    drops what fails it: for it every mcp: entry is off."""
    released = re.compile(r"^[a-z][a-z0-9-]{0,31}$")

    assert released.pattern == capabilities.PLAIN_NAME.pattern
    assert not released.match("mcp:docs-db")


def test_a_plugin_cannot_take_an_mcp_name(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(capabilities, "REGISTRY", dict(capabilities.REGISTRY))

    with pytest.raises(ValueError, match="invalid capability name"):
        capabilities.register(capabilities.Capability("mcp:docs-db", "x", "x", "x", {}))


def test_the_registry_holds_one_entry_per_server(store: Store) -> None:
    assert capabilities.registry(None) == capabilities.REGISTRY == capabilities.registry(store)
    mcp.add(store, docs_db(reaches="the docs database"))

    entries = capabilities.registry(store)
    entry = entries["mcp:docs-db"]

    assert list(entries) == [*capabilities.REGISTRY, "mcp:docs-db"]
    assert (entry.kind, entry.origin, entry.label) == ("mcp", "spartito", "docs-db")
    assert entry.reaches == docs_db(reaches="the docs database").reaches
    assert entry.providers == {
        "claude": Support(on=Wiring(mcp_servers={"docs-db": STDIO_ENTRY}, mcp_tools=True))
    }
    assert entry.providers["claude"].off == Wiring()
    assert capabilities.CHROME.kind == "builtin" and "mcp:docs-db" not in capabilities.REGISTRY
    assert capabilities.known("mcp:docs-db", store) == entry


def test_unknown_names_are_named_in_the_refusal(store: Store) -> None:
    with pytest.raises(ValueError, match=r"unknown MCP server 'docs-db' \(added: none\)"):
        capabilities.known("mcp:docs-db", store)
    mcp.add(store, remote())
    with pytest.raises(ValueError, match=r"unknown MCP server 'docs-db' \(added: remote-docs\)"):
        capabilities.known("mcp:docs-db", store)
    with pytest.raises(ValueError, match=r"unknown capability 'jira' \(known: chrome\)"):
        capabilities.known("jira", store)


# -- resolution ---------------------------------------------------------------------------


def test_a_server_that_is_on_reaches_a_claude_turn(store: Store) -> None:
    mcp.add(store, docs_db())
    store.set_settings(settings.GLOBAL, {"capabilities": {"mcp:docs-db": True}})

    found = capabilities.resolve(store, agent())
    item = next(item for item in found.items if item.name == "mcp:docs-db")

    assert (item.state, item.source, item.kind, item.origin) == ("on", "global", "mcp", "spartito")
    assert Wiring(mcp_servers={"docs-db": STDIO_ENTRY}, mcp_tools=True) in found.wirings
    assert found.record() == {"on": ["mcp:docs-db"], "skipped": {}}


def test_a_codex_agent_skips_it_with_a_note(store: Store) -> None:
    mcp.add(store, docs_db())
    store.set_settings(settings.GLOBAL, {"capabilities": {"mcp:docs-db": True}})

    found = capabilities.resolve(store, agent("codex"))

    assert found.skipped == {"mcp:docs-db": "codex does not support it"}
    assert found.wirings == ()


def test_a_server_that_is_off_adds_nothing(store: Store) -> None:
    mcp.add(store, docs_db())

    found = capabilities.resolve(store, agent())
    item = next(item for item in found.items if item.name == "mcp:docs-db")

    assert (item.state, item.source) == ("off", "default")
    assert all(not wiring.mcp_servers for wiring in found.wirings)


def test_a_name_without_a_server_is_off_and_not_listed(store: Store) -> None:
    """Never added, or removed: no level makes it a turn's, and it blocks nothing."""
    store.set_settings(
        settings.GLOBAL,
        {"capabilities": {"mcp:gone": True}, "role_capabilities": {"worker": {"mcp:gone": True}}},
    )

    found = capabilities.resolve(store, agent(own={"mcp:gone": True}))

    assert [item.name for item in found.items] == ["chrome"]
    assert found.record() == {"on": [], "skipped": {}}


def test_a_claude_invocation_with_a_server_on_places_it(store: Store, tmp_path: Path) -> None:
    """The Claude provider places the server in the turn's configuration file (in detail:
    test_mcp_wiring.py); the turn gets tool search, as for every capability that brings MCP
    tools."""
    mcp.add(store, docs_db())
    store.set_settings(settings.GLOBAL, {"capabilities": {"mcp:docs-db": True}})
    granted = capabilities.resolve(store, agent())
    ctx = RunContext(
        agent=agent(),
        prompt="do it",
        system_prompt="ROLE",
        scratch=tmp_path,
        wirings=granted.wirings,
        mcp_config=tmp_path / "mcp-1.json",
    )

    invocation = provider("claude").invocation(ctx)

    assert invocation.argv[0]
    (content,) = invocation.private_files.values()
    assert list(json.loads(content)["mcpServers"]) == ["docs-db"]


def test_the_breakdown_carries_the_last_status() -> None:
    item = capabilities.Item("mcp:docs-db", "docs-db", "on", "global", None, False, kind="mcp")
    item = capabilities.Item(**{**item.__dict__, "origin": "spartito"})
    found = capabilities.Resolution((item,))

    assert found.breakdown({"docs-db": "connected"})["items"] == [
        {
            "name": "mcp:docs-db",
            "label": "docs-db",
            "state": "on",
            "source": "global",
            "override": None,
            "final": False,
            "note": None,
            "kind": "mcp",
            "origin": "spartito",
            "tools": None,
            "last_status": "connected",
        }
    ]
    assert found.breakdown()["items"][0]["last_status"] is None


@pytest.mark.parametrize(
    ("records", "expected"),
    [
        ([], {}),
        ([None, "not json", '{"on": []}'], {}),
        (
            ['{"mcp": {"docs-db": "failed"}}', '{"on": [], "mcp": {"docs-db": "connected"}}'],
            {"docs-db": "connected"},
        ),
        (['{"mcp": {"docs-db": "failed"}}', '{"on": []}'], {}),  # the last turn had none
        (['{"mcp": ["docs-db"]}'], {}),
        (['{"mcp": {"docs-db": 1, "other": "pending"}}'], {"other": "pending"}),
    ],
)
def test_the_last_statuses_come_from_the_last_record(records: list, expected: dict) -> None:
    assert capabilities.last_statuses(records) == expected


# -- adding and removing ------------------------------------------------------------------


def test_the_owner_adds_and_lists_a_server(orch: Orchestrator) -> None:
    added = add_docs_db(orch, reaches="the docs database")

    assert orch.mcp_list() == [added]
    assert added == {
        "name": "mcp:docs-db",
        "label": "docs-db",
        "description": "the MCP server docs-db (stdio: a command, added in Spartito) and its tools",
        "reaches": docs_db(reaches="the docs database").reaches,
        "requires": [],
        "providers": ["claude"],
        "status": added["status"],
        "reason": added["reason"],
        "default": False,
        "global": None,
        "roles": {},
        "plugin": False,
        "docs": "docs/mcp.md#what-works-today",
        "kind": "mcp",
        "server": "docs-db",
        "origin": "spartito",
        "transport": "stdio",
        "definition": STDIO_ENTRY,
        "own_reaches": "the docs database",
        "tools": None,
        "last_status": None,
        "agents": {},
    }
    assert orch.mcp_show("mcp:docs-db") == orch.mcp_show("docs-db") == added
    assert [entry["kind"] for entry in orch.capability_list()] == ["builtin", "mcp"]


def test_a_url_server_is_listed_without_its_path(orch: Orchestrator) -> None:
    added = orch.mcp_add("remote-docs", url=URL + "?team=docs", headers=["X-Key: ${DOCS_KEY}"])

    assert added["transport"] == "http"
    assert added["definition"] == {
        "url": "https://mcp.example.com",
        "headers": {"X-Key": "${DOCS_KEY}"},
    }
    assert "example.com/mcp" not in json.dumps(added) and "team=" not in json.dumps(added)


def test_adding_refuses_what_cannot_be_made(orch: Orchestrator) -> None:
    add_docs_db(orch)

    with pytest.raises(OrchestratorError, match=r"'docs-db' exists already; remove it first"):
        add_docs_db(orch)
    with pytest.raises(OrchestratorError, match="write \\$\\{VAR\\}"):
        orch.mcp_add("other", command="x", env={"TOKEN": "secret"})
    with pytest.raises(OrchestratorError, match="Spartito's own MCP server"):
        orch.mcp_add("orch", command="x")
    with pytest.raises(OrchestratorError, match="unknown MCP server 'other'"):
        orch.mcp_show("other")
    with pytest.raises(OrchestratorError, match="unknown project"):
        orch.mcp_list("no-such-project")


def test_only_the_owner_adds_and_removes(orch: Orchestrator, project: str) -> None:
    lead = orch.lead(project)
    boss = orch.orchestrator()
    add_docs_db(orch)

    for granter in (lead, boss):
        with pytest.raises(OrchestratorError, match=f"{granter.id} cannot add MCP servers"):
            orch.mcp_add("other", command="x", granter=granter)
        with pytest.raises(OrchestratorError, match=f"{granter.id} cannot remove MCP servers"):
            orch.mcp_remove("docs-db", granter=granter)
    assert [entry["server"] for entry in orch.mcp_list()] == ["docs-db"]
    assert orch.mcp_remove("docs-db")["server"] == "docs-db"
    assert orch.mcp_list() == []
    with pytest.raises(OrchestratorError, match=r"unknown MCP server 'docs-db' \(added: none\)"):
        orch.mcp_remove("docs-db")


def test_removing_clears_every_level_so_a_new_server_inherits_none(
    orch: Orchestrator, project: str
) -> None:
    add_docs_db(orch)
    worker = orch.create_agent(project, worktree=False)
    orch.set_capability("mcp:docs-db", True)
    orch.set_capability("chrome", False)
    orch.set_capability("mcp:docs-db", False, project)
    orch.set_capability("mcp:docs-db", False, role="reviewer")
    orch.set_capability("mcp:docs-db", True, project, role="worker")
    orch.set_agent_capabilities(worker.id, {"mcp:docs-db": True, "chrome": False})
    assert orch.mcp_show("docs-db", project)["agents"] == {worker.id: True}

    removed = orch.mcp_remove("mcp:docs-db")

    assert removed["cleared"] == [
        "global",
        "role reviewer",
        f"project {project}",
        f"role worker of project {project}",
        f"agent {worker.id}",
    ]
    assert orch.store.get_settings(settings.GLOBAL)["capabilities"] == {"chrome": False}
    assert "role_capabilities" not in orch.store.get_settings(settings.GLOBAL)
    assert orch.store.get_settings(project) == {}
    assert json.loads(orch.agent(worker.id).capabilities) == {"chrome": False}
    readded = add_docs_db(orch)
    assert (readded["global"], readded["roles"], readded["agents"]) == (None, {}, {})
    assert orch.mcp_show("docs-db", project)["project"] is None


def test_adding_clears_levels_left_behind(orch: Orchestrator) -> None:
    """Levels naming a server that does not exist (written by older code, or a removal
    that stopped half way) do not reach a server added later under that name."""
    orch.store.set_settings(
        settings.GLOBAL,
        {
            "capabilities": {"mcp:docs-db": True},
            "role_capabilities": {"worker": {"mcp:docs-db": True}},
        },
    )

    added = add_docs_db(orch)

    assert (added["global"], added["roles"]) == (None, {})
    assert orch.store.get_settings(settings.GLOBAL) == {}


# -- levels: the capability rules ---------------------------------------------------------


def test_a_server_is_turned_on_off_and_inherited_at_each_level(
    orch: Orchestrator, project: str
) -> None:
    with pytest.raises(OrchestratorError, match="unknown MCP server 'docs-db'"):
        orch.set_capability("mcp:docs-db", True)
    add_docs_db(orch)

    orch.set_capability("mcp:docs-db", True)
    orch.set_capability("mcp:docs-db", False, project)
    orch.set_capability("mcp:docs-db", False, role="reviewer")
    entry = orch.mcp_show("docs-db", project)
    assert (entry["global"], entry["project"], entry["roles"]) == (True, False, {"reviewer": False})

    orch.set_capability("mcp:docs-db", None, project)
    assert orch.mcp_show("docs-db", project)["project"] is None


def test_a_gone_server_blocks_no_other_write_and_can_be_inherited(orch: Orchestrator) -> None:
    orch.store.set_settings(settings.GLOBAL, {"capabilities": {"mcp:gone": True}})

    orch.update_settings({**orch.settings(), "archive_after_hours": 5})
    orch.set_capability("chrome", True)
    assert orch.store.get_settings(settings.GLOBAL)["capabilities"] == {
        "mcp:gone": True,
        "chrome": True,
    }
    orch.set_capability("mcp:gone", None)
    assert orch.store.get_settings(settings.GLOBAL)["capabilities"] == {"chrome": True}
    with pytest.raises(OrchestratorError, match="unknown MCP server 'gone'"):
        orch.update_settings({"capabilities": {"mcp:gone": False}})


def test_an_agent_turns_on_no_scope(orch: Orchestrator, project: str) -> None:
    add_docs_db(orch)
    lead = orch.lead(project)

    with pytest.raises(OrchestratorError, match="only the owner or the orchestrator turns"):
        orch.set_capability("mcp:docs-db", True, project, granter=lead)
    orch.set_capability("mcp:docs-db", False, project, granter=lead)  # off: always
    with pytest.raises(OrchestratorError, match="cannot lift the off of mcp:docs-db"):
        orch.set_capability("mcp:docs-db", None, project, granter=lead)
    orch.set_capability("mcp:docs-db", True, role="worker", granter=orch.orchestrator())
    assert orch.mcp_show("docs-db", project)["roles"] == {"worker": True}


def test_an_agent_gives_only_what_its_own_turn_has(orch: Orchestrator, project: str) -> None:
    add_docs_db(orch)
    lead = orch.lead(project, provider="claude")
    worker = orch.create_agent(project, worktree=False, parent_id=lead.id, provider="claude")

    with pytest.raises(OrchestratorError, match=f"cannot give mcp:docs-db: {lead.id} does not"):
        orch.set_agent_capabilities(worker.id, {"mcp:docs-db": True}, lead)
    orch.set_agent_capabilities(lead.id, {"mcp:docs-db": True})
    lead = orch.agent(lead.id)
    orch.set_agent_capabilities(worker.id, {"mcp:docs-db": True}, lead)
    assert orch.agent_capabilities(worker.id)["on"] == ["mcp:docs-db"]

    orch.set_agent_capabilities(worker.id, {"mcp:docs-db": False})  # the owner's off
    with pytest.raises(OrchestratorError, match="cannot give mcp:docs-db: it was turned off"):
        orch.set_agent_capabilities(worker.id, {"mcp:docs-db": True}, lead)
    with pytest.raises(OrchestratorError, match="cannot lift the off of mcp:docs-db"):
        orch.set_agent_capabilities(worker.id, {"mcp:docs-db": None}, lead)


def test_a_roles_off_is_final_for_a_server(orch: Orchestrator, project: str) -> None:
    add_docs_db(orch)
    worker = orch.create_agent(project, worktree=False, provider="claude")
    orch.set_capability("mcp:docs-db", False, project, role="worker")

    with pytest.raises(OrchestratorError, match="mcp:docs-db is off for role worker"):
        orch.set_agent_capabilities(worker.id, {"mcp:docs-db": True})
    orch.store.set_agent_capabilities(worker.id, {"mcp:docs-db": True})  # written around it
    item = next(
        item
        for item in orch.agent_capabilities(worker.id)["items"]
        if item["name"] == "mcp:docs-db"
    )
    assert (item["state"], item["final"]) == ("skipped", True)


def test_spawning_on_a_provider_without_it_notes_it(orch: Orchestrator, project: str) -> None:
    add_docs_db(orch)

    spawned, _ = orch.spawn(project, "x", worktree=False, capabilities={"mcp:docs-db": True})

    assert spawned.provider_note == (
        "fake does not support mcp:docs-db: kept on the agent and skipped in its turns"
    )


# -- what lists and agents see ------------------------------------------------------------


def test_an_agent_sees_the_status_its_last_turn_reported(orch: Orchestrator, project: str) -> None:
    add_docs_db(orch)
    worker = orch.create_agent(project, worktree=False, provider="claude")
    orch.set_agent_capabilities(worker.id, {"mcp:docs-db": True})

    def item() -> dict:
        found = orch.agent_capabilities(worker.id)["items"]
        return next(entry for entry in found if entry["name"] == "mcp:docs-db")

    assert (item()["kind"], item()["origin"], item()["tools"], item()["last_status"]) == (
        "mcp",
        "spartito",
        None,
        None,
    )
    for status in ("pending", "connected"):
        run = orch.store.add_run(worker.id, "go")
        record = {"on": ["mcp:docs-db"], "skipped": {}, "mcp": {"docs-db": status}}
        orch.store.set_run_capabilities(run.id, record)

    assert item()["last_status"] == "connected"
    assert orch.setup(worker.id)["capabilities"] == orch.agent_capabilities(worker.id)
    detail = orch.view(orch.agent(worker.id), detail=True)["capabilities"]
    assert detail == orch.agent_capabilities(worker.id)
    assert orch.view(orch.agent(worker.id))["capabilities"] == ["mcp:docs-db"]
    assert orch.mcp_show("docs-db")["last_status"] == "connected"
    chrome = orch.agent_capabilities(worker.id)["items"][0]
    assert chrome["kind"] == "builtin" and "last_status" not in chrome


def test_the_list_shows_the_latest_status_of_any_turn(orch: Orchestrator, project: str) -> None:
    add_docs_db(orch)
    orch.mcp_add("remote-docs", url=URL)
    first = orch.create_agent(project, worktree=False, provider="claude")
    second = orch.create_agent(project, worktree=False, provider="claude")
    for agent_id, statuses in (
        (first.id, {"docs-db": "failed", "remote-docs": "needs-auth"}),
        (second.id, {"docs-db": "connected"}),
        (first.id, {}),
    ):
        run = orch.store.add_run(agent_id, "go")
        orch.store.set_run_capabilities(run.id, {"on": [], "skipped": {}, "mcp": statuses})

    found = {entry["server"]: entry["last_status"] for entry in orch.mcp_list()}

    assert found == {"docs-db": "connected", "remote-docs": "needs-auth"}
