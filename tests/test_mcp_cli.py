"""`spartito mcp`: the list, show, add and remove of MCP servers added in Spartito, and on,
off and inherit as the capability commands for `mcp:<name>`; the MCP section of
`spartito capabilities` and `spawn --capabilities mcp:…`. Turns are queued, never run."""

from __future__ import annotations

import json
import sys

import click
import click.testing
import pytest
from typer.testing import CliRunner

from orchestrator import cli, config, desktop
from orchestrator.service import ORCHESTRATOR_ID, Orchestrator

DB_URL = "${DOCS_DB_URL}"
URL = "https://mcp.example.com/mcp"
ADD_DOCS_DB = [
    "mcp", "add", "docs-db", "--command", "docs-db-mcp", "--arg", "--read-only", "--arg=-v",
    "--env", f"DOCS_DB_URL={DB_URL}", "--reaches", "the docs database, read-only",
]  # fmt: skip
ADD_REMOTE = ["mcp", "add", "remote-docs", "--url", URL, "--header", "Authorization: ${DOCS_TOKEN}"]


@pytest.fixture(autouse=True)
def no_launch(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    launched: list[int] = []
    monkeypatch.setattr(Orchestrator, "_launch", lambda self, run: launched.append(run.id))
    return launched


def invoke(*args: str, agent: str | None = None) -> click.testing.Result:
    env = {"ORCH_AGENT_ID": agent} if agent else {}
    return CliRunner().invoke(cli.app, list(args), env=env)


def run(*args: str) -> list[str]:
    result = invoke(*args)
    assert result.exit_code == 0, result.output
    return click.unstyle(result.output).splitlines()


def refused(*args: str, agent: str | None = None) -> str:
    result = invoke(*args, agent=agent)
    assert result.exit_code == 1, result.output
    return " ".join(click.unstyle(result.output).split())


def own(orch: Orchestrator, agent_id: str) -> dict | None:
    stored = orch.agent(agent_id).capabilities
    return json.loads(stored) if stored else None


# -- list, show, add, remove -----------------------------------------------------------------


def test_with_no_servers_the_list_says_how_to_add_one(orch: Orchestrator) -> None:
    assert run("mcp") == [
        f"no MCP servers added; add one with `{config.CLI_NAME} mcp add NAME --command CMD`"
        f" (a local command) or `{config.CLI_NAME} mcp add NAME --url URL` (a remote server)"
    ]
    assert json.loads(invoke("mcp", "--json").output) == []


def test_adding_a_command_server_says_it_is_off_and_how_to_turn_it_on(orch: Orchestrator) -> None:
    lines = run(*ADD_DOCS_DB)

    entry = orch.mcp_show("docs-db")
    assert entry["definition"] == {
        "command": "docs-db-mcp",
        "args": ["--read-only", "-v"],  # values starting with '-', both forms
        "env": {"DOCS_DB_URL": DB_URL},
    }
    assert entry["own_reaches"] == "the docs database, read-only"
    assert lines == [
        f"added docs-db (stdio): {entry['reaches']}",
        f"it is off until a level turns it on: `{config.CLI_NAME} mcp on docs-db` (globally;"
        " --project P, --role R or --agent A for less)",
    ]
    assert entry["global"] is None and entry["default"] is False


def test_adding_a_url_server_keeps_its_headers(orch: Orchestrator) -> None:
    lines = run(*ADD_REMOTE)

    entry = orch.mcp_show("remote-docs")
    assert entry["transport"] == "http"
    assert entry["definition"] == {
        "url": "https://mcp.example.com",
        "headers": {"Authorization": "${DOCS_TOKEN}"},
    }
    assert lines[0] == f"added remote-docs (http): {entry['reaches']}"


def test_the_list_shows_each_server_and_its_levels(orch: Orchestrator, project: str) -> None:
    run(*ADD_DOCS_DB)
    run(*ADD_REMOTE)
    orch.set_capability("mcp:docs-db", True, project)
    orch.set_capability("mcp:docs-db", False, role="reviewer")
    orch.set_capability("mcp:remote-docs", True, project, "tester")
    agent = orch.create_agent(project, worktree=False, provider="claude")
    record = {"on": ["mcp:docs-db"], "skipped": {}, "mcp": {"docs-db": "connected"}}
    orch.store.set_run_capabilities(orch.store.add_run(agent.id, "go").id, record)

    lines = run("mcp")
    assert [line.split() for line in lines] == [
        ["NAME", "TRANSPORT", "TARGET", "GLOBAL", "LAST", "STATUS"],
        ["docs-db", "stdio", "docs-db-mcp", "off", "(default)", "connected"],
        ["remote-docs", "http", "https://mcp.example.com", "off", "(default)", "-"],
        ["role", "reviewer:", "mcp:docs-db", "off", "(final)"],
    ]
    lines = run("mcp", "--project", project)
    assert lines[0].split()[:5] == ["NAME", "TRANSPORT", "TARGET", "GLOBAL", "PROJECT"]
    assert lines[1].split()[3:6] == ["off", "(default)", "on"]
    assert lines[2].split()[3:6] == ["off", "(default)", "inherit"]
    assert lines[-1] == f"role tester ({project}): mcp:remote-docs on"

    assert json.loads(invoke("mcp", "--json").output) == orch.mcp_list()
    assert json.loads(invoke("mcp", "-p", project, "--json").output) == orch.mcp_list(project)


def test_show_gives_the_definition_reach_levels_and_agents(
    orch: Orchestrator, project: str
) -> None:
    run(*ADD_DOCS_DB)
    run(*ADD_REMOTE)
    orch.set_capability("mcp:docs-db", True, project)
    orch.set_capability("mcp:docs-db", False, role="reviewer")
    orch.set_capability("mcp:docs-db", True, project, "tester")
    agent = orch.create_agent(project, worktree=False, provider="claude")
    orch.set_agent_capabilities(agent.id, {"mcp:docs-db": True})

    entry = orch.mcp_show("docs-db")
    assert run("mcp", "show", "docs-db", "--project", project) == [
        "name         docs-db  (capability mcp:docs-db)",
        "transport    stdio",
        "command      docs-db-mcp",
        "args         --read-only -v",
        f"env          DOCS_DB_URL={DB_URL}",
        f"reaches      {entry['reaches']}",
        "global       off (default)",
        "project      on",
        "last status  -",
        f"roles        reviewer off (final), tester ({project}) on",
        f"agents       {agent.id} on",
    ]
    assert run("mcp", "show", "mcp:remote-docs") == [
        "name         remote-docs  (capability mcp:remote-docs)",
        "transport    http",
        "url          https://mcp.example.com (origin only)",
        "headers      Authorization: ${DOCS_TOKEN}",
        f"reaches      {orch.mcp_show('remote-docs')['reaches']}",
        "global       off (default)",
        "last status  -",
        "roles        none",
        "agents       none",
    ]
    shown = json.loads(invoke("mcp", "show", "docs-db", "--json").output)
    assert shown == entry and shown["agents"] == {agent.id: True}
    assert URL not in invoke("mcp", "show", "remote-docs", "--json").output


def test_remove_says_where_settings_were_removed(orch: Orchestrator, project: str) -> None:
    run(*ADD_DOCS_DB)
    run(*ADD_REMOTE)
    orch.set_capability("mcp:docs-db", True)
    orch.set_capability("mcp:docs-db", False, project, "reviewer")
    agent = orch.create_agent(project, worktree=False)
    orch.set_agent_capabilities(agent.id, {"mcp:docs-db": True})

    assert run("mcp", "remove", "docs-db") == [
        "removed docs-db; its settings were removed from: global, role reviewer of project"
        f" {project}, agent {agent.id}"
    ]
    assert run("mcp", "remove", "mcp:remote-docs") == ["removed remote-docs; it had no setting"]
    assert orch.mcp_list() == [] and own(orch, agent.id) is None


@pytest.mark.parametrize(
    ("args", "message"),
    [
        (["add", "a b", "--command", "x"], "invalid MCP server name 'a b'"),
        (["add", "orch", "--command", "x"], "Spartito's own MCP server"),
        (["add", "x", "--command", "x", "--env", "DOCS_DB_URL=postgres://docs"], "refuses literal"),
        (["add", "x", "--command", "x", "--env", "DOCS_DB_URL=${DOCS_DB_URL:-x}"], "not a ${VAR}"),
        (["add", "x", "--url", URL, "--header", "Authorization: Bearer ${T}"], "refuses literal"),
        (["add", "x", "--url", "https://user:pw@mcp.example.com"], "user name or password"),
        (["add", "x", "--command", "x", "--url", URL], "either a command (stdio) or a URL"),
        (["add", "x"], "either a command (stdio) or a URL"),
        (["add", "x", "--url", URL, "--arg", "-v"], "args and env are for a command"),
        (["add", "x", "--command", "x", "--header", "A: ${T}"], "headers are for a URL"),
        (["add", "docs-db", "--command", "x"], "'docs-db' exists already"),
        (["show", "nope"], "unknown MCP server 'nope' (added: docs-db)"),
        (["remove", "nope"], "unknown MCP server 'nope' (added: docs-db)"),
        (["on", "nope"], "unknown MCP server 'nope' (added: docs-db)"),
        (["on", "a b"], "invalid capability name 'mcp:a b'"),
        (["on", "docs-db", "--agent", "x", "--project", "p"], "--agent cannot be combined"),
        (["--project", "nope"], "unknown project"),
    ],
)
def test_the_command_refuses(orch: Orchestrator, args: list[str], message: str) -> None:
    run(*ADD_DOCS_DB)

    assert message in refused("mcp", *args)
    assert [entry["server"] for entry in orch.mcp_list()] == ["docs-db"]


@pytest.mark.parametrize("caller", ["lead", ORCHESTRATOR_ID, "worker"])
def test_adding_and_removing_in_an_agents_turn_are_refused(
    orch: Orchestrator, project: str, caller: str
) -> None:
    run(*ADD_DOCS_DB)
    agent = {
        "lead": orch.lead(project),
        ORCHESTRATOR_ID: orch.orchestrator(),
        "worker": orch.create_agent(project, worktree=False),
    }[caller]

    added = refused("mcp", "add", "other", "--command", "x", agent=agent.id)
    removed = refused("mcp", "remove", "docs-db", agent=agent.id)

    assert f"{agent.id} cannot add MCP servers: adding and removing them is the owner's alone" in (
        added
    )
    assert f"{agent.id} cannot remove MCP servers" in removed
    assert [entry["server"] for entry in orch.mcp_list()] == ["docs-db"]


# -- on, off, inherit: the capability commands for mcp:<name> ---------------------------------


def levels(orch: Orchestrator, project: str, agent_id: str) -> tuple:
    return orch.settings(), orch.settings(project), own(orch, agent_id)


SCOPES = [[], ["--project", "{project}"], ["--role", "reviewer"], ["-p", "{project}", "-r", "t"]]


@pytest.mark.parametrize("scope", [*SCOPES, ["--agent", "{agent}"]])
def test_on_off_inherit_are_the_capability_commands(
    orch: Orchestrator, project: str, scope: list[str]
) -> None:
    run(*ADD_DOCS_DB)
    agent = orch.create_agent(project, worktree=False, provider="claude")
    filled = [arg.format(project=project, agent=agent.id) for arg in scope]

    for state in ("on", "off", "inherit"):
        by_mcp = run("mcp", state, "docs-db", *filled)
        after_mcp = levels(orch, project, agent.id)
        run("capabilities", "inherit", "mcp:docs-db", *filled)  # back to before
        by_capabilities = run("capabilities", state, "mcp:docs-db", *filled)
        assert by_mcp == by_capabilities
        assert levels(orch, project, agent.id) == after_mcp
        assert run("mcp", state, "mcp:docs-db", *filled) == by_mcp  # the long name works too
    assert levels(orch, project, agent.id)[2] is None


def test_an_agents_turn_turns_a_server_on_under_the_capability_rules(
    orch: Orchestrator, project: str
) -> None:
    run(*ADD_DOCS_DB)
    me = orch.create_agent(project, worktree=False, provider="claude")
    other = orch.create_agent(project, worktree=False, provider="claude")

    for args in (["on", "mcp:docs-db", "--agent", other.id], ["on", "mcp:docs-db", "-p", project]):
        message = refused("capabilities", *args, agent=me.id)
        assert refused("mcp", *args, agent=me.id) == message
    assert "does not have it itself" in refused("mcp", "on", "docs-db", "-a", other.id, agent=me.id)
    orch.set_agent_capabilities(me.id, {"mcp:docs-db": True})
    given = invoke("mcp", "on", "docs-db", "--agent", other.id, agent=me.id)
    assert given.exit_code == 0, given.output
    assert own(orch, other.id) == {"mcp:docs-db": True}
    assert invoke("mcp", "on", "docs-db", "--role", "worker", agent=ORCHESTRATOR_ID).exit_code == 1
    orch.orchestrator()
    by_orchestrator = invoke("mcp", "on", "docs-db", "--role", "worker", agent=ORCHESTRATOR_ID)
    assert by_orchestrator.exit_code == 0, by_orchestrator.output


def test_a_roles_off_is_final_for_the_agents_own_on(orch: Orchestrator, project: str) -> None:
    run(*ADD_DOCS_DB)
    agent = orch.create_agent(project, worktree=False, provider="claude")
    run("mcp", "off", "docs-db", "--role", agent.role)

    message = refused("mcp", "on", "docs-db", "--agent", agent.id)

    assert f"mcp:docs-db is off for role {agent.role}" in message
    assert message == refused("capabilities", "on", "mcp:docs-db", "--agent", agent.id)
    assert own(orch, agent.id) is None


def test_capabilities_show_gives_an_mcp_servers_last_status(
    orch: Orchestrator, project: str
) -> None:
    run(*ADD_DOCS_DB)
    agent = orch.create_agent(project, worktree=False, provider="claude")
    orch.set_agent_capabilities(agent.id, {"mcp:docs-db": True})
    assert run("capabilities", "show", agent.id)[1].split() == ["mcp:docs-db", "on", "agent"]

    record = {"on": ["mcp:docs-db"], "skipped": {}, "mcp": {"docs-db": "needs-auth"}}
    orch.store.set_run_capabilities(orch.store.add_run(agent.id, "go").id, record)

    lines = run("capabilities", "show", agent.id)
    assert lines[1].split() == ["mcp:docs-db", "on", "agent", "last", "status:", "needs-auth"]
    assert lines[0].split()[0] == "chrome" and "last status" not in lines[0]


# -- the capabilities listing and spawn ------------------------------------------------------


def test_the_capabilities_list_has_a_section_for_mcp_servers(
    orch: Orchestrator, project: str
) -> None:
    run(*ADD_DOCS_DB)
    run(*ADD_REMOTE)
    orch.set_capability("mcp:docs-db", False, role="reviewer")

    lines = run("capabilities", "--project", project)

    at = lines.index(f"MCP servers ({config.CLI_NAME} mcp):")
    assert lines[at - 1] == "" and lines[0].split()[0] == "NAME"
    assert [line.split()[0] for line in lines[1:at] if line] == ["chrome"]
    assert lines[at + 1].split() == [
        "NAME", "TRANSPORT", "TARGET", "GLOBAL", "PROJECT", "LAST", "STATUS"
    ]  # fmt: skip
    assert lines[at + 2].split()[:3] == ["mcp:docs-db", "stdio", "docs-db-mcp"]
    assert lines[at + 3].split()[:3] == ["mcp:remote-docs", "http", "https://mcp.example.com"]
    assert lines[at + 4 :] == ["role reviewer: mcp:docs-db off (final)"]
    listed = json.loads(invoke("capabilities", "--json").output)
    assert [entry["name"] for entry in listed] == ["chrome", "mcp:docs-db", "mcp:remote-docs"]


def test_without_servers_the_capabilities_list_has_no_mcp_section(orch: Orchestrator) -> None:
    assert not any("MCP servers" in line for line in run("capabilities"))


def test_spawn_takes_an_mcp_server(orch: Orchestrator, project: str) -> None:
    run(*ADD_DOCS_DB)
    spawn = ["spawn", "x", "-p", project, "--no-worktree", "--provider", "claude"]

    agent_id = run(*spawn, "--capabilities", "mcp:docs-db,-chrome")[0].split()[0]

    assert own(orch, agent_id) == {"mcp:docs-db": True, "chrome": False}
    assert "capabilities mcp:docs-db" in run("status", agent_id)
    assert "unknown MCP server 'nope'" in refused(*spawn, "--capabilities", "mcp:nope")


# -- the orch server and the process's part ---------------------------------------------------


def test_serve_runs_spartitos_own_server(monkeypatch: pytest.MonkeyPatch) -> None:
    from orchestrator import mcp_server

    served: list[bool] = []
    monkeypatch.setattr(mcp_server, "main", lambda: served.append(True))

    assert invoke("mcp", "serve").exit_code == 0 and served == [True]


@pytest.mark.parametrize(
    ("argv", "part"),
    [(["mcp", "serve"], "mcp"), (["mcp"], "cli"), (["mcp", "add", "x"], "cli"), (["web"], "web")],
)
def test_only_serve_counts_as_the_mcp_server_process(
    monkeypatch: pytest.MonkeyPatch, argv: list[str], part: str
) -> None:
    monkeypatch.setattr(desktop, "ROLE", None)
    monkeypatch.setattr(sys, "argv", [config.CLI_NAME, *argv])

    assert desktop.role() == part
