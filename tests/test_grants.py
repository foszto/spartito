"""Capabilities at the surfaces (core/grants.py): the listing, the settings per scope and
role, an agent's own setting, who may give which, and the CLI, HTTP and MCP interfaces."""

from __future__ import annotations

import functools
import json

import anyio
import click
import click.testing
import pytest
from fakecapability import FAKE_TOOL
from starlette.testclient import TestClient
from typer.testing import CliRunner

from orchestrator import capabilities, cli, config, mcp_server
from orchestrator.core import availability
from orchestrator.models import RunStatus
from orchestrator.providers import locate
from orchestrator.service import ORCHESTRATOR_ID, Orchestrator, OrchestratorError
from orchestrator.web.app import app

ORIGIN = {"origin": "http://localhost"}
UNSEEN = capabilities.CHROME_UNSEEN


@pytest.fixture(autouse=True)
def no_launch(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """Turns are queued, never run: these tests look at what is stored."""
    launched: list[int] = []
    monkeypatch.setattr(Orchestrator, "_launch", lambda self, run: launched.append(run.id))
    return launched


@pytest.fixture(autouse=True)
def fake_tool(monkeypatch: pytest.MonkeyPatch) -> None:
    """`fake-tool` (carried by the fake provider) registered after chrome; no login
    variables, so chrome's status is `unknown`."""
    registry = dict(capabilities.REGISTRY)
    registry["fake-tool"] = FAKE_TOOL
    monkeypatch.setattr(capabilities, "REGISTRY", registry)
    monkeypatch.setattr(capabilities, "PLUGINS", {"fake-tool"})
    for name in capabilities.NOT_CLAUDE_AI:
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def client(orch: Orchestrator) -> TestClient:
    return TestClient(app, base_url="http://localhost")


def own(orch: Orchestrator, agent_id: str) -> dict | None:
    stored = orch.agent(agent_id).capabilities
    return json.loads(stored) if stored else None


def as_caller(monkeypatch: pytest.MonkeyPatch, agent_id: str | None) -> None:
    if agent_id:
        monkeypatch.setenv("ORCH_AGENT_ID", agent_id)
    else:
        monkeypatch.delenv("ORCH_AGENT_ID", raising=False)


# -- the listing and the settings ---------------------------------------------------------


def test_the_listing_describes_each_capability(orch: Orchestrator) -> None:
    chrome, tool = orch.capability_list()

    assert chrome == {
        "name": "chrome",
        "label": "Claude in Chrome",
        "description": capabilities.CHROME.description,
        "reaches": capabilities.CHROME.reaches,
        "requires": list(capabilities.CHROME.requires),
        "providers": ["claude"],
        "status": "unknown",
        "reason": UNSEEN,
        "default": False,
        "global": None,
        "roles": {},
        "plugin": False,
        "docs": "docs/capabilities.md#chrome-claude-in-chrome",
        "kind": "builtin",
    }
    assert (tool["name"], tool["providers"], tool["plugin"]) == ("fake-tool", ["fake"], True)


def test_the_listing_starts_no_provider_check_unless_asked_to_wait(
    orch: Orchestrator, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []

    def probe(provider) -> locate.Probe:
        calls.append(provider.name)
        return locate.Probe(locate.NOT_INSTALLED, None, None, "not installed")

    monkeypatch.setattr(availability, "probe", probe)
    availability.reset()

    assert orch.capability_list()[0]["status"] == "unknown" and calls == []
    chrome = orch.capability_list(wait=True)[0]
    assert (chrome["status"], chrome["reason"]) == (
        "unavailable",
        "needs Claude Code: not installed",
    )
    assert "claude" in calls
    calls.clear()
    assert orch.capability_list()[0]["status"] == "unavailable" and calls == []  # the cache


def test_the_listing_follows_the_default(orch: Orchestrator, monkeypatch) -> None:
    monkeypatch.setattr(capabilities, "DEFAULT_ON", frozenset({"chrome"}))

    assert orch.capability_list()[0]["default"] is True


def test_capabilities_are_set_per_scope_and_role(orch: Orchestrator, project: str) -> None:
    orch.update_settings({"worker": {"fake": {"model": "fake-small"}}})

    orch.set_capability("chrome", True)
    orch.set_capability("chrome", False, role="reviewer")
    orch.set_capability("chrome", False, project)
    listed = orch.set_capability("chrome", True, project, "tester")

    (chrome, _) = listed
    assert (chrome["global"], chrome["roles"]) == (True, {"reviewer": False})
    assert (chrome["project"], chrome["project_roles"]) == (False, {"tester": True})
    assert orch.settings()["worker"] == {"fake": {"model": "fake-small"}}  # kept
    assert orch.settings(project) == {
        "capabilities": {"chrome": False},
        "role_capabilities": {"tester": {"chrome": True}},
    }

    orch.set_capability("chrome", None, project)
    orch.set_capability("chrome", None, role="reviewer")
    (chrome, _) = orch.capability_list(project)
    assert (chrome["project"], chrome["roles"], chrome["global"]) == (None, {}, True)


@pytest.mark.parametrize(
    ("args", "message"),
    [
        (("jira", True), r"unknown capability 'jira' \(known: chrome, fake-tool\)"),
        (("chrome", "on"), "expected true, false or null"),
        (("chrome", True, "nowhere"), "unknown project 'nowhere'"),
        (("chrome", True, None, "  "), "role must be a role name"),
    ],
)
def test_a_setting_that_cannot_be_made_is_refused(
    orch: Orchestrator, args: tuple, message: str
) -> None:
    with pytest.raises(OrchestratorError, match=message):
        orch.set_capability(*args)


def test_a_name_whose_plugin_is_gone_can_still_be_removed(orch: Orchestrator) -> None:
    orch.store.set_settings("", {"capabilities": {"gone": True}})  # stored while it was there

    orch.set_capability("gone", None)

    assert "capabilities" not in orch.settings()


@pytest.mark.parametrize(
    "data",
    [{"capabilities": {"jira": True}}, {"role_capabilities": {"tester": {"jira": False}}}],
)
def test_the_settings_refuse_a_capability_that_is_not_registered(
    orch: Orchestrator, client: TestClient, project: str, data: dict
) -> None:
    message = "unknown capability 'jira' (known: chrome, fake-tool)"
    with pytest.raises(OrchestratorError, match=r"unknown capability 'jira' \(known"):
        orch.update_settings(data, project)
    response = client.post("/api/settings", json={"data": data}, headers=ORIGIN)
    assert response.status_code == 400 and response.json() == {"error": message}
    assert orch.settings() == {} and orch.settings(project) == {}


def test_the_settings_keep_and_remove_a_name_whose_plugin_is_gone(orch: Orchestrator) -> None:
    stored = {"capabilities": {"gone": True}, "role_capabilities": {"tester": {"gone": False}}}
    orch.store.set_settings("", stored)

    orch.update_settings({"capabilities": {"gone": True, "chrome": True}})  # unchanged: kept
    orch.update_settings({"archive_after_hours": 2})  # not named: kept as stored
    assert orch.settings()["capabilities"] == {"gone": True, "chrome": True}
    assert orch.settings()["role_capabilities"] == {"tester": {"gone": False}}
    with pytest.raises(OrchestratorError, match="unknown capability 'gone'"):
        orch.update_settings({"role_capabilities": {"tester": {"gone": True}}})

    orch.update_settings({"capabilities": {"gone": None, "chrome": True}, "role_capabilities": {}})
    assert orch.settings()["capabilities"] == {"chrome": True}
    assert "role_capabilities" not in orch.settings()


def test_unreadable_stored_values_do_not_block_a_change(orch: Orchestrator) -> None:
    """Reading ignores them; a change drops them, which repairs the scope."""
    orch.store.set_settings(
        "", {"capabilities": "garbage", "role_capabilities": {"tester": {"chrome": "yes"}}}
    )

    orch.set_capability("chrome", True)
    orch.set_capability("chrome", False, role="reviewer")

    assert orch.settings()["capabilities"] == {"chrome": True}
    assert orch.settings()["role_capabilities"] == {"reviewer": {"chrome": False}}
    orch.store.set_settings("", {"role_capabilities": {"tester": "garbage", " ": {}}})
    orch.set_capability("chrome", None, role="tester")
    assert "role_capabilities" not in orch.settings()


# -- an agent's own setting ---------------------------------------------------------------


def test_an_agents_own_setting_is_set_and_removed(orch: Orchestrator, project: str) -> None:
    agent = orch.create_agent(project, worktree=False)

    found = orch.set_agent_capabilities(agent.id, {"fake-tool": True, "chrome": False})

    assert own(orch, agent.id) == {"fake-tool": True, "chrome": False}
    assert found["on"] == ["fake-tool"]
    assert [(i["name"], i["state"], i["source"]) for i in found["items"]] == [
        ("chrome", "off", "agent"),
        ("fake-tool", "on", "agent"),
    ]
    found = orch.set_agent_capabilities(agent.id, {"fake-tool": None, "chrome": None})
    assert own(orch, agent.id) is None and found["on"] == []
    assert orch.agent_capabilities(agent.id) == found


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"jira": True}, "unknown capability 'jira'"),
        ({"chrome": "yes"}, "expected true, false or null"),
        (["chrome"], "expected an object"),
        ({"Bad": None}, "invalid capability name"),
    ],
)
def test_an_agents_setting_that_cannot_be_made_is_refused(
    orch: Orchestrator, project: str, changes: object, message: str
) -> None:
    agent = orch.create_agent(project, worktree=False)

    with pytest.raises(OrchestratorError, match=message):
        orch.set_agent_capabilities(agent.id, changes)
    assert own(orch, agent.id) is None


@pytest.mark.parametrize("scope", ["global", "project"])
def test_a_roles_restriction_cannot_be_lifted_by_an_agents_own_setting(
    orch: Orchestrator, project: str, scope: str
) -> None:
    orch.set_capability("fake-tool", False, project if scope == "project" else None, "reviewer")
    agent = orch.create_agent(project, role="reviewer", worktree=False)
    fix = f"`{config.CLI_NAME} capabilities on fake-tool --role reviewer"
    fix += f" --project {project}`" if scope == "project" else "`"

    with pytest.raises(OrchestratorError, match="off for role reviewer") as refused:
        orch.set_agent_capabilities(agent.id, {"fake-tool": True})
    assert fix in str(refused.value)
    with pytest.raises(OrchestratorError, match="off for role reviewer"):
        orch.spawn(project, "x", role="reviewer", worktree=False, capabilities={"fake-tool": True})
    assert [a.id for a in orch.agents(project)] == [agent.id]
    orch.set_agent_capabilities(agent.id, {"fake-tool": False})  # off is always allowed
    assert own(orch, agent.id) == {"fake-tool": False}


def test_the_orchestrators_setting_creates_it_on_first_use(orch: Orchestrator) -> None:
    found = orch.set_agent_capabilities(ORCHESTRATOR_ID, {"chrome": True})

    assert own(orch, ORCHESTRATOR_ID) == {"chrome": True}
    assert found["items"][0]["source"] == "agent"


# -- spawn and the provider note ----------------------------------------------------------


def test_spawn_stores_the_agents_own_setting(orch: Orchestrator, project: str) -> None:
    agent, _ = orch.spawn(project, "x", worktree=False, capabilities={"fake-tool": True})

    assert own(orch, agent.id) == {"fake-tool": True} and agent.provider_note is None
    plain, _ = orch.spawn(project, "x", worktree=False)
    assert own(orch, plain.id) is None


def test_spawn_notes_a_capability_the_provider_does_not_support(
    orch: Orchestrator, project: str
) -> None:
    agent, _ = orch.spawn(
        project, "x", worktree=False, provider="codex", capabilities={"chrome": True}
    )

    assert own(orch, agent.id) == {"chrome": True}  # kept, skipped at its turns
    assert agent.provider_note == (
        "codex does not support chrome: kept on the agent and skipped in its turns"
    )
    assert orch.agent_capabilities(agent.id)["items"][0]["state"] == "skipped"


# -- who may give which: MCP ---------------------------------------------------------------


def test_a_lead_gives_only_what_it_has(
    orch: Orchestrator, project: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    lead = orch.lead(project)
    as_caller(monkeypatch, lead.id)

    with pytest.raises(OrchestratorError, match=f"cannot give fake-tool: {lead.id} does not"):
        mcp_server.spawn_agent("x", worktree=False, capabilities="fake-tool")
    assert [a.id for a in orch.agents(project)] == [lead.id]  # nothing spawned

    reply = mcp_server.spawn_agent("x", worktree=False, capabilities="-fake-tool")  # dropping
    assert own(orch, reply["id"]) == {"fake-tool": False}

    orch.set_agent_capabilities(lead.id, {"fake-tool": True})
    reply = mcp_server.spawn_agent("x", worktree=False, capabilities="fake-tool")
    assert own(orch, reply["id"]) == {"fake-tool": True}


def test_send_message_changes_the_setting_under_the_same_rule(
    orch: Orchestrator, project: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    lead = orch.lead(project)
    worker = orch.create_agent(project, worktree=False, parent_id=lead.id)
    as_caller(monkeypatch, lead.id)

    with pytest.raises(OrchestratorError, match="cannot give fake-tool"):
        mcp_server.send_message(worker.id, "go", capabilities="fake-tool")
    assert orch.store.runs_of(worker.id) == [] and own(orch, worker.id) is None

    orch.set_agent_capabilities(lead.id, {"fake-tool": True})
    mcp_server.send_message(worker.id, "go", capabilities="fake-tool")
    assert own(orch, worker.id) == {"fake-tool": True}
    mcp_server.send_message(worker.id, "again", capabilities="-fake-tool")
    assert own(orch, worker.id) == {"fake-tool": False}
    mcp_server.send_message(worker.id, "and again")  # empty: no change
    assert own(orch, worker.id) == {"fake-tool": False}
    assert len(orch.store.runs_of(worker.id)) == 3


def test_an_agent_cannot_give_back_what_was_turned_off_for_an_agent(
    orch: Orchestrator, project: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The owner turned it off for the lead: a child of the lead that has it cannot turn it
    back on; a lead that dropped it for its worker cannot either; the orchestrator can."""
    lead = orch.lead(project)
    child = orch.create_agent(project, worktree=False, parent_id=lead.id)
    worker = orch.create_agent(project, worktree=False, parent_id=lead.id)
    orch.set_agent_capabilities(child.id, {"fake-tool": True})
    orch.set_agent_capabilities(lead.id, {"fake-tool": False})

    as_caller(monkeypatch, child.id)
    refused = f"cannot give fake-tool: it was turned off for {lead.id}, and only the owner"
    with pytest.raises(OrchestratorError, match=refused):
        mcp_server.send_message(lead.id, "go", capabilities="fake-tool")
    assert own(orch, lead.id) == {"fake-tool": False} and orch.store.runs_of(lead.id) == []

    orch.set_agent_capabilities(lead.id, {"fake-tool": True})
    as_caller(monkeypatch, lead.id)
    mcp_server.send_message(worker.id, "go", capabilities="-fake-tool")
    with pytest.raises(OrchestratorError, match=f"it was turned off for {worker.id}"):
        mcp_server.send_message(worker.id, "go", capabilities="fake-tool")
    assert own(orch, worker.id) == {"fake-tool": False}

    orch.orchestrator()
    as_caller(monkeypatch, ORCHESTRATOR_ID)
    mcp_server.send_message(worker.id, "go", capabilities="fake-tool")
    mcp_server.send_message(lead.id, "go", capabilities="fake-tool")
    assert own(orch, worker.id) == own(orch, lead.id) == {"fake-tool": True}


@pytest.mark.parametrize("caller", [ORCHESTRATOR_ID, None], ids=["maestro", "chat"])
def test_the_orchestrator_and_the_interactive_session_give_any(
    orch: Orchestrator, project: str, monkeypatch: pytest.MonkeyPatch, caller: str | None
) -> None:
    if caller:
        orch.orchestrator()
    as_caller(monkeypatch, caller)

    reply = mcp_server.spawn_agent("x", project, worktree=False, capabilities="fake-tool")
    assert own(orch, reply["id"]) == {"fake-tool": True}
    mcp_server.send_message(reply["id"], "go", capabilities="chrome")
    assert own(orch, reply["id"]) == {"fake-tool": True, "chrome": True}

    orch.set_capability("fake-tool", False, role="tester")
    with pytest.raises(OrchestratorError, match="off for role tester"):
        mcp_server.spawn_agent(
            "x", project, role="tester", worktree=False, capabilities="fake-tool"
        )


@pytest.mark.parametrize("text", ["jira", "Chrome", "-", "fake-tool,,jira"])
def test_mcp_refuses_unknown_or_malformed_names(
    orch: Orchestrator, project: str, monkeypatch: pytest.MonkeyPatch, text: str
) -> None:
    as_caller(monkeypatch, None)

    with pytest.raises(OrchestratorError, match="unknown capability|invalid capability name"):
        mcp_server.spawn_agent("x", project, worktree=False, capabilities=text)
    assert orch.agents(project) == []


def test_parse_reads_names_and_drops() -> None:
    assert capabilities.parse(" chrome, -fake-tool ,") == {"chrome": True, "fake-tool": False}
    assert capabilities.parse("") == {}


# -- views --------------------------------------------------------------------------------


def test_the_views_carry_the_capabilities(
    orch: Orchestrator, project: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    agent, _ = orch.spawn(project, "x", worktree=False, capabilities={"fake-tool": True})
    as_caller(monkeypatch, None)

    detail = mcp_server.get_agent(agent.id)["capabilities"]
    listed = next(a for a in mcp_server.list_agents(project) if a["id"] == agent.id)

    assert detail == orch.agent_capabilities(agent.id)
    assert detail["on"] == ["fake-tool"] and detail["error"] is None
    assert listed["capabilities"] == ["fake-tool"]
    assert orch.setup(agent.id)["capabilities"] == detail


def test_wait_and_dispatch_replies_carry_no_breakdown(
    orch: Orchestrator, project: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The breakdown is for get_agent and the dashboard's agent detail only."""
    as_caller(monkeypatch, None)
    reply = mcp_server.spawn_agent("x", project, worktree=False, capabilities="fake-tool")
    assert "capabilities" not in reply
    assert "capabilities" not in mcp_server.send_message(reply["id"], "go")

    def wait() -> dict:
        call = functools.partial(mcp_server.wait_for_agents, [reply["id"]], timeout_seconds=1)
        return anyio.run(call)

    waiting = wait()  # queued, never run: statuses only
    assert waiting["finished"] is False and "items" not in json.dumps(waiting)
    for queued in orch.store.runs_of(reply["id"]):
        orch.store.finish_run(queued.id, RunStatus.DONE, result="ok")
    done = wait()
    assert done["finished"] is True and "items" not in json.dumps(done)
    assert "capabilities" not in json.dumps(done)


# -- CLI ----------------------------------------------------------------------------------


def run(*args: str) -> list[str]:
    result = CliRunner().invoke(cli.app, list(args))
    assert result.exit_code == 0, result.output
    return click.unstyle(result.output).splitlines()


def test_the_cli_lists_the_capabilities(orch: Orchestrator, project: str) -> None:
    orch.set_capability("chrome", False, role="reviewer")
    orch.set_capability("fake-tool", True, project, "tester")

    lines = run("capabilities")
    assert lines[0].split() == ["NAME", "PROVIDERS", "GLOBAL", "STATUS", "REASON"]
    assert lines[1].split()[:5] == ["chrome", "claude", "off", "(default)", "unknown"]
    assert lines[1].endswith(UNSEEN)
    assert lines[2].split()[:5] == ["fake-tool", "fake", "off", "(default)", "unknown"]
    assert lines[3:] == ["role reviewer: chrome off (final)"]

    lines = run("capabilities", "--project", project)
    assert lines[0].split()[:4] == ["NAME", "PROVIDERS", "GLOBAL", "PROJECT"]
    assert lines[1].split()[4] == "inherit"
    assert lines[-1] == f"role tester ({project}): fake-tool on"

    listed = json.loads(CliRunner().invoke(cli.app, ["capabilities", "--json"]).output)
    assert listed == orch.capability_list()


def test_the_cli_turns_them_on_and_off_at_each_level(orch: Orchestrator, project: str) -> None:
    agent = orch.create_agent(project, worktree=False)

    run("capabilities", "on", "chrome")
    run("capabilities", "off", "chrome", "--project", project)
    run("capabilities", "off", "fake-tool", "--role", "reviewer")
    run("capabilities", "on", "fake-tool", "--role", "tester", "--project", project)
    lines = run("capabilities", "on", "fake-tool", "--agent", agent.id)

    assert orch.settings()["capabilities"] == {"chrome": True}
    assert orch.settings()["role_capabilities"] == {"reviewer": {"fake-tool": False}}
    assert orch.settings(project)["capabilities"] == {"chrome": False}
    assert orch.settings(project)["role_capabilities"] == {"tester": {"fake-tool": True}}
    assert own(orch, agent.id) == {"fake-tool": True}
    assert lines == ["chrome     off      project", "fake-tool  on       agent"]
    assert run("capabilities", "show", agent.id) == lines

    run("capabilities", "inherit", "fake-tool", "--agent", agent.id)
    run("capabilities", "inherit", "chrome")
    assert own(orch, agent.id) is None and "capabilities" not in orch.settings()


@pytest.mark.parametrize(
    ("args", "message"),
    [
        (["on", "chrome", "--agent", "x", "--role", "r"], "--agent cannot be combined"),
        (["on", "jira"], "unknown capability 'jira'"),
        (["show", "nobody"], "unknown agent 'nobody'"),
    ],
)
def test_the_cli_refuses(orch: Orchestrator, args: list[str], message: str) -> None:
    result = CliRunner().invoke(cli.app, ["capabilities", *args])

    assert result.exit_code == 1 and message in result.output


def test_spawn_takes_capabilities_on_the_command_line(orch: Orchestrator, project: str) -> None:
    lines = run("spawn", "x", "-p", project, "--no-worktree", "--capabilities", "fake-tool,-chrome")

    agent_id = lines[0].split()[0]
    assert own(orch, agent_id) == {"fake-tool": True, "chrome": False}
    status = run("status", agent_id)
    assert "capabilities fake-tool" in status
    bad = CliRunner().invoke(cli.app, ["spawn", "x", "-p", project, "--capabilities", "Bad"])
    assert bad.exit_code == 1 and "invalid capability name" in bad.output


# The command run in an agent's turn (ORCH_AGENT_ID, set by the runner) gives as that agent.


def as_agent(agent_id: str | None, *args: str) -> click.testing.Result:
    env = {"ORCH_AGENT_ID": agent_id} if agent_id else {}
    return CliRunner().invoke(cli.app, list(args), env=env)


SPAWN = ["spawn", "x", "-p", "{project}", "--no-worktree", "--capabilities"]


@pytest.mark.parametrize(
    ("args", "dropping"),
    [
        (["capabilities", "on", "chrome", "--agent", "{me}"], ["capabilities", "off"]),
        (["capabilities", "on", "chrome", "--agent", "{other}"], ["capabilities", "inherit"]),
        ([*SPAWN, "chrome"], [*SPAWN, "-chrome"]),
    ],
    ids=["itself", "another", "spawn"],
)
def test_the_cli_in_an_agents_turn_gives_only_what_that_agent_has(
    orch: Orchestrator, project: str, args: list[str], dropping: list[str]
) -> None:
    me = orch.create_agent(project, worktree=False)
    other = orch.create_agent(project, worktree=False)
    filled = [arg.format(me=me.id, other=other.id, project=project) for arg in args]

    result = as_agent(me.id, *filled)

    assert result.exit_code == 1
    assert f"cannot give chrome: {me.id} does not have it itself" in result.output
    assert own(orch, me.id) is None and own(orch, other.id) is None
    assert len(orch.agents(project)) == 2  # nothing spawned
    if dropping[0] == "capabilities":
        dropping = [*dropping, *filled[2:]]
    allowed = as_agent(me.id, *[arg.format(project=project) for arg in dropping])
    assert allowed.exit_code == 0, allowed.output  # dropping is always allowed


@pytest.mark.parametrize(
    ("args", "where"),
    [
        ([], "globally"),
        (["--project", "{project}"], "for this project"),
        (["--role", "worker"], "for role worker"),
    ],
    ids=["global", "project", "role"],
)
def test_the_cli_in_an_agents_turn_turns_on_for_no_scope(
    orch: Orchestrator, project: str, args: list[str], where: str
) -> None:
    me = orch.create_agent(project, worktree=False)
    orch.set_agent_capabilities(me.id, {"chrome": True})  # having it is not enough
    scope = [arg.format(project=project) for arg in args]

    result = as_agent(me.id, "capabilities", "on", "chrome", *scope)

    assert result.exit_code == 1
    assert (
        f"{me.id} cannot turn chrome on {where}: only the owner or the orchestrator turns a"
        " capability on globally, for a project or for a role"
    ) in result.output
    assert not {"capabilities", "role_capabilities"} & (
        orch.settings().keys() | orch.settings(project).keys()
    )
    for state in ("inherit", "off"):  # removing nothing and turning off stay allowed
        allowed = as_agent(me.id, "capabilities", state, "chrome", *scope)
        assert allowed.exit_code == 0, allowed.output
    lifting = as_agent(me.id, "capabilities", "inherit", "chrome", *scope)  # removes an off
    assert lifting.exit_code == 1 and f"{me.id} cannot lift the off of chrome" in lifting.output


def test_the_cli_in_the_orchestrators_turn_and_the_owners_give_any(
    orch: Orchestrator, project: str
) -> None:
    orch.orchestrator()
    worker = orch.create_agent(project, worktree=False)
    for caller in (ORCHESTRATOR_ID, None):
        for scope in ([], ["--project", project], ["--role", "worker"], ["--agent", worker.id]):
            result = as_agent(caller, "capabilities", "on", "chrome", *scope)
            assert result.exit_code == 0, result.output
        spawned = as_agent(
            caller, "spawn", "x", "-p", project, "--no-worktree", "--capabilities", "chrome"
        )
        assert spawned.exit_code == 0, spawned.output
        assert own(orch, spawned.output.split()[0]) == {"chrome": True}
        orch.update_settings({"capabilities": {}, "role_capabilities": {}})
        orch.update_settings({"capabilities": {}}, project)
    assert own(orch, worker.id) == {"chrome": True}


# Removing an off (inherit) turns on what a level above has on: the same as turning on.

LIFTS = {  # scope -> (its options, where the message says, the off it removes)
    "global": ([], "globally", {}),
    "project": (["--project", "{project}"], "for this project", {"project": "{project}"}),
    "role": (["--role", "worker"], "for role worker", {"role": "worker"}),
    "project-role": (
        ["--project", "{project}", "--role", "worker"],
        "for role worker",
        {"project": "{project}", "role": "worker"},
    ),
}


@pytest.mark.parametrize("scope", list(LIFTS))
def test_an_agent_cannot_lift_an_off_of_a_scope(
    orch: Orchestrator, project: str, monkeypatch: pytest.MonkeyPatch, scope: str
) -> None:
    """fake-tool is on by default here: each off decides it, and removing it would turn it on."""
    monkeypatch.setattr(capabilities, "DEFAULT_ON", frozenset({"fake-tool"}))
    args, where, level = LIFTS[scope]
    options = [arg.format(project=project) for arg in args]
    level = {key: value.format(project=project) for key, value in level.items()}
    orch.orchestrator()
    me = orch.create_agent(project, worktree=False)
    orch.set_capability("fake-tool", False, **level)
    before = (orch.agent_capabilities(me.id), orch.settings(), orch.settings(project))
    assert "fake-tool" not in before[0]["on"]
    message = (
        f"{me.id} cannot lift the off of fake-tool {where}: only the owner or the orchestrator"
        " removes an off globally, for a project or for a role"
    )

    with pytest.raises(OrchestratorError, match=message):
        orch.set_capability("fake-tool", None, **level, granter=orch.agent(me.id))
    refused = as_agent(me.id, "capabilities", "inherit", "fake-tool", *options)
    assert refused.exit_code == 1 and message in refused.output
    assert (orch.agent_capabilities(me.id), orch.settings(), orch.settings(project)) == before

    for caller in (ORCHESTRATOR_ID, None):  # the orchestrator and the owner can
        lifted = as_agent(caller, "capabilities", "inherit", "fake-tool", *options)
        assert lifted.exit_code == 0, lifted.output
        assert "fake-tool" in orch.agent_capabilities(me.id)["on"]
        orch.set_capability("fake-tool", False, **level)
    orch.set_capability("fake-tool", True, **level)
    for _ in range(2):  # removing an on, then removing nothing: allowed
        removed = as_agent(me.id, "capabilities", "inherit", "fake-tool", *options)
        assert removed.exit_code == 0, removed.output


def test_an_agent_cannot_lift_an_off_of_an_agents_own_setting(
    orch: Orchestrator, project: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(capabilities, "DEFAULT_ON", frozenset({"fake-tool"}))
    orch.orchestrator()
    me = orch.create_agent(project, worktree=False)
    other = orch.create_agent(project, worktree=False)
    for agent in (me, other):
        orch.set_agent_capabilities(agent.id, {"fake-tool": False})
    before = {agent.id: orch.agent_capabilities(agent.id) for agent in (me, other)}

    for target in (me, other):
        message = (
            f"cannot lift the off of fake-tool for {target.id}: it was turned off for it, and only"
            " the owner or the orchestrator can lift it"
        )
        with pytest.raises(OrchestratorError, match=message):
            orch.set_agent_capabilities(target.id, {"fake-tool": None}, orch.agent(me.id))
        refused = as_agent(me.id, "capabilities", "inherit", "fake-tool", "--agent", target.id)
        assert refused.exit_code == 1 and message in refused.output
    assert {agent.id: orch.agent_capabilities(agent.id) for agent in (me, other)} == before
    assert own(orch, me.id) == own(orch, other.id) == {"fake-tool": False}

    for caller, target in ((ORCHESTRATOR_ID, me), (None, other)):
        lifted = as_agent(caller, "capabilities", "inherit", "fake-tool", "--agent", target.id)
        assert lifted.exit_code == 0, lifted.output
        assert (
            own(orch, target.id) is None and "fake-tool" in orch.agent_capabilities(target.id)["on"]
        )
    orch.set_agent_capabilities(other.id, {"fake-tool": True})
    for _ in range(2):  # removing an on, then removing nothing: allowed
        removed = as_agent(me.id, "capabilities", "inherit", "fake-tool", "--agent", other.id)
        assert removed.exit_code == 0, removed.output
    assert own(orch, other.id) is None


def test_a_command_of_another_homes_turn_acts_as_the_owner(
    orch: Orchestrator, project: str, tmp_path
) -> None:
    """Tried in a temporary home from a turn: its ORCH_AGENT_ID is of the turn's home."""
    worker = orch.create_agent(project, worktree=False)
    other_home = tmp_path / "turn-home"
    other_home.mkdir()
    env = {"ORCH_AGENT_ID": "fix-login-3f2a", config.TURN_HOME_ENV: str(other_home)}

    result = CliRunner().invoke(
        cli.app, ["capabilities", "on", "chrome", "--agent", worker.id], env=env
    )
    assert result.exit_code == 0, result.output
    spawn = ["spawn", "x", "-p", project, "--no-worktree", "--capabilities", "chrome"]
    spawned = CliRunner().invoke(cli.app, spawn, env=env)
    assert spawned.exit_code == 0, spawned.output
    assert own(orch, worker.id) == own(orch, spawned.output.split()[0]) == {"chrome": True}

    link = tmp_path / "same-home"
    link.symlink_to(config.home())
    for same in (str(config.home()), str(link), f"{config.home()}/../{config.home().name}"):
        env[config.TURN_HOME_ENV] = same  # this home: an id it does not know fails closed
        refused = CliRunner().invoke(cli.app, ["capabilities", "off", "chrome"], env=env)
        assert refused.exit_code == 1 and "unknown agent 'fix-login-3f2a'" in refused.output
    assert "capabilities" not in orch.settings()


def test_unreadable_stored_values_block_no_other_settings_write(orch: Orchestrator) -> None:
    garbage = {
        "capabilities": "garbage",
        "role_capabilities": {"tester": {"chrome": "yes"}, " ": {}, "reviewer": {"chrome": False}},
    }
    orch.store.set_settings("", garbage)

    data = orch.settings()  # read the scope, change one key, write all of it back
    data["archive_after_hours"] = 5
    orch.update_settings(data)
    assert orch.settings() == {
        "archive_after_hours": 5,
        "role_capabilities": {"reviewer": {"chrome": False}},
    }

    orch.store.set_settings("", garbage)
    orch.update_settings({"archive_after_hours": 6})  # not named: kept, without the garbage
    assert orch.settings()["role_capabilities"] == {"reviewer": {"chrome": False}}
    assert "capabilities" not in orch.settings()

    orch.store.set_settings("", garbage)
    run("config", "keep-awake", "off")
    assert orch.settings()["keep_awake"] is False and "capabilities" not in orch.settings()

    orch.store.set_settings("", garbage)  # one role changed, the others sent back as read
    roles = {"tester": {"chrome": "yes"}, " ": {}, "reviewer": {"chrome": True}}
    orch.update_settings({"role_capabilities": roles})
    assert orch.settings()["role_capabilities"] == {"reviewer": {"chrome": True}}


@pytest.mark.parametrize(
    "data",
    [
        {"capabilities": "other garbage"},
        {"role_capabilities": {"tester": {"chrome": "no"}}},
        {"role_capabilities": "other garbage"},
    ],
)
def test_a_malformed_value_the_request_sends_is_still_refused(
    orch: Orchestrator, data: dict
) -> None:
    stored = {"capabilities": "garbage", "role_capabilities": {"tester": {"chrome": "yes"}}}
    orch.store.set_settings("", stored)

    with pytest.raises(OrchestratorError, match="capabilit"):
        orch.update_settings(data)
    assert orch.settings() == stored


def test_status_names_what_is_skipped(orch: Orchestrator, project: str) -> None:
    agent = orch.create_agent(project, worktree=False, capabilities={"chrome": True})

    status = run("status", agent.id)

    assert (
        f"capabilities none  (skipped: chrome; {config.CLI_NAME} capabilities show {agent.id})"
        in status
    )


def test_providers_json_lists_what_each_provider_carries(orch: Orchestrator) -> None:
    found = json.loads(CliRunner().invoke(cli.app, ["providers", "--json"]).output)

    carried = {entry["name"]: entry["capabilities"] for entry in found}
    assert carried["claude"] == ["chrome"] and carried["codex"] == []
    assert carried["fake"] == ["fake-tool"]


# -- HTTP ---------------------------------------------------------------------------------


def test_http_lists_and_sets_the_capabilities(
    orch: Orchestrator, project: str, client: TestClient
) -> None:
    assert client.get("/api/capabilities").json() == {"capabilities": orch.capability_list()}
    assert "project" in client.get(f"/api/capabilities?project={project}").json()["capabilities"][0]

    body = {"name": "chrome", "state": False, "project": project, "role": "reviewer"}
    reply = client.post("/api/capabilities", json=body, headers=ORIGIN)

    assert reply.status_code == 200
    assert reply.json()["capabilities"][0]["project_roles"] == {"reviewer": False}
    reply = client.post("/api/capabilities", json={"name": "chrome", "state": True}, headers=ORIGIN)
    assert reply.json()["capabilities"][0]["global"] is True
    assert client.get("/api/state").json()["capabilities"] == orch.capability_list()


def test_http_sets_an_agents_own_and_shows_it(
    orch: Orchestrator, project: str, client: TestClient
) -> None:
    agent = orch.create_agent(project, worktree=False)

    reply = client.post(
        f"/api/agents/{agent.id}/capabilities", json={"fake-tool": True}, headers=ORIGIN
    )

    assert reply.status_code == 200
    assert reply.json() == {"capabilities": orch.agent_capabilities(agent.id), "running": False}
    detail = client.get(f"/api/agents/{agent.id}/events").json()["agent"]
    assert detail["capabilities"]["on"] == ["fake-tool"]
    state = client.get("/api/state").json()
    (listed,) = [a for p in state["projects"] for a in p["agents"] if a["id"] == agent.id]
    assert listed["capabilities"] == ["fake-tool"]
    assert state["orchestrator_setup"]["capabilities"]["on"] == []


def test_http_spawns_with_capabilities(
    orch: Orchestrator, project: str, client: TestClient
) -> None:
    body = {"project": project, "task": "x", "worktree": False, "capabilities": {"chrome": True}}

    reply = client.post("/api/agents", json=body, headers=ORIGIN).json()

    assert own(orch, reply["agent_id"]) == {"chrome": True}
    assert "fake does not support chrome" in reply["note"]


@pytest.mark.parametrize(
    ("path", "body", "message"),
    [
        ("/api/capabilities", {"name": "jira", "state": True}, "unknown capability 'jira'"),
        ("/api/capabilities", {"state": True}, "'name' is required"),
        ("/api/capabilities", {"name": "chrome", "state": 1}, "expected true, false or null"),
        ("/api/capabilities", {"name": "chrome", "state": True, "project": "x"}, "unknown project"),
        ("/api/agents/nobody/capabilities", {"chrome": True}, "unknown agent 'nobody'"),
        ("/api/agents", {"project": "demo", "task": "x", "capabilities": {"jira": True}}, "jira"),
    ],
)
def test_http_refusals(
    orch: Orchestrator, project: str, client: TestClient, path: str, body: dict, message: str
) -> None:
    reply = client.post(path, json=body, headers=ORIGIN)

    assert reply.status_code == 400 and message in reply.json()["error"]
    assert orch.agents(project) == []


def test_a_plugin_that_is_gone_is_not_listed(orch: Orchestrator) -> None:
    capabilities.REGISTRY.pop("fake-tool")  # the fixture's copy

    assert [entry["name"] for entry in orch.capability_list()] == ["chrome"]
