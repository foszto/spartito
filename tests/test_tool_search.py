"""Claude Code's tool search (the built-in tool `ToolSearch`) for agents whose turns have MCP
tools: the loadout's `tool_search` (loadout.py) and a capability wiring's `mcp_tools`
decide whether a restricted `--tools` list gets ToolSearch and whether the `orch` server is
marked `alwaysLoad` (providers/claude.py). Fakes only: no CLI runs."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest
from test_interfaces import exec_argv, option  # noqa: F401 (a fixture)
from typer.testing import CliRunner

from orchestrator import cli, config
from orchestrator.capabilities import Capability
from orchestrator.loadout import Loadout
from orchestrator.models import Agent
from orchestrator.providers import McpServer, RunContext, get
from orchestrator.providers.base import Support, Wiring
from orchestrator.providers.claude import CHROME, NO_BACKGROUND_ENV, loadout_flags, tool_search
from orchestrator.service import Orchestrator

WORKER = Loadout(
    tools=("Bash", "Read", "Edit", "Write", "Glob", "Grep"),
    strict_mcp=True,
    setting_sources=("project", "local"),
    slash_commands=False,
    codex_user_config=False,
)
LEAD = Loadout(
    tools=("Bash", "Read", "Glob", "Grep"),
    strict_mcp=True,
    setting_sources=("project", "local"),
    slash_commands=False,
    codex_user_config=False,
)
SERVER = McpServer("py", ["-m", "orchestrator.mcp_server"], {"ORCH_AGENT_ID": "a-1"})
# What the turn's command line was before tool search: the `orch` server without alwaysLoad.
PLAIN_SERVER = {"command": "py", "args": ["-m", "orchestrator.mcp_server"], "env": SERVER.env}


def turn(
    tmp_path: Path,
    loadout: Loadout,
    wirings: tuple[Wiring, ...] = (),
    lead: bool = False,
    provider: str = "claude",
    **fields,
):
    agent = Agent(
        id="fix-login-3f2a",
        project="my-app",
        name="fix-login",
        role="lead" if lead else "worker",
        provider=provider,
        cwd="/w",
        session_id="sid",  # resumed: the command line has no random session id
    )
    ctx = RunContext(
        agent=agent,
        prompt="do it",
        system_prompt="ROLE",
        scratch=tmp_path,
        mcp=SERVER if lead else None,
        lead=lead,
        loadout=loadout,
        wirings=wirings,
        **fields,
    )
    return get(provider).invocation(ctx)


def tools(invocation) -> str | None:
    return option(invocation.argv, "--tools")


def server(invocation) -> dict:
    return json.loads(option(invocation.argv, "--mcp-config"))["mcpServers"]["orch"]


def without(argv: list[str], *drop: str) -> list[str]:
    return [arg for arg in argv if arg not in drop]


# -- the rule ------------------------------------------------------------------------------


def test_a_worker_without_a_capability_runs_exactly_as_before(tmp_path: Path) -> None:
    for mode in ("auto", "always", "never"):
        invocation = turn(tmp_path, replace(WORKER, tool_search=mode), (CHROME.off,))
        assert invocation.argv == [
            "claude", "-p", "--output-format", "stream-json", "--verbose",
            "--resume", "sid", "--permission-mode", "auto",
            "--tools", "Bash,Read,Edit,Write,Glob,Grep",
            "--strict-mcp-config", "--setting-sources", "project,local",
            "--disable-slash-commands", "--append-system-prompt", "ROLE",
        ], mode  # fmt: skip
        assert invocation.env == {"CLAUDE_CODE_ENABLE_CFC": "0"}


def test_a_worker_with_chrome_gets_tool_search(tmp_path: Path) -> None:
    on = turn(tmp_path, WORKER, (CHROME.on,))
    off = turn(tmp_path, WORKER, (CHROME.off,))

    assert tools(on) == "Bash,Read,Edit,Write,Glob,Grep,ToolSearch"
    assert "--mcp-config" not in on.argv
    # The rest of the command line is what it was: the tool list and --chrome only.
    assert without(on.argv, "--chrome", tools(on)) == without(off.argv, tools(off))


def test_a_lead_with_chrome_keeps_its_own_tools_loaded(tmp_path: Path) -> None:
    on = turn(tmp_path, LEAD, (CHROME.on,), lead=True)
    plain = turn(tmp_path, LEAD, lead=True)

    assert tools(on) == "Bash,Read,Glob,Grep,ToolSearch"
    assert server(on) == {**PLAIN_SERVER, "alwaysLoad": True}
    assert option(on.argv, "--allowedTools") == "mcp__orch"
    assert on.env == plain.env == NO_BACKGROUND_ENV


def test_a_lead_without_a_capability_is_unchanged_under_auto(tmp_path: Path) -> None:
    plain = turn(tmp_path, LEAD, lead=True)
    never = turn(tmp_path, replace(LEAD, tool_search="never"), lead=True)

    assert plain.argv == never.argv
    assert tools(plain) == "Bash,Read,Glob,Grep"
    # Byte for byte as before: the same keys in the same order, nothing added.
    assert option(plain.argv, "--mcp-config") == json.dumps({"mcpServers": {"orch": PLAIN_SERVER}})


def test_always_defers_the_orch_tools_too(tmp_path: Path) -> None:
    always = replace(LEAD, tool_search="always")

    lead = turn(tmp_path, always, lead=True)
    with_chrome = turn(tmp_path, always, (CHROME.on,), lead=True)
    worker = turn(tmp_path, replace(WORKER, tool_search="always"))

    assert tools(lead) == tools(with_chrome) == "Bash,Read,Glob,Grep,ToolSearch"
    assert server(lead) == server(with_chrome) == PLAIN_SERVER  # no alwaysLoad
    assert tools(worker) == "Bash,Read,Edit,Write,Glob,Grep"  # no MCP tools at all


def test_never_sends_every_schema_upfront(tmp_path: Path) -> None:
    never = replace(LEAD, tool_search="never")

    lead = turn(tmp_path, never, (CHROME.on,), lead=True)
    worker = turn(tmp_path, replace(WORKER, tool_search="never"), (CHROME.on,))

    assert tools(lead) == "Bash,Read,Glob,Grep" and server(lead) == PLAIN_SERVER
    assert tools(worker) == "Bash,Read,Edit,Write,Glob,Grep"


@pytest.mark.parametrize("mode", ["auto", "always", "never"])
def test_the_clis_default_tool_set_gets_no_tool_list(tmp_path: Path, mode: str) -> None:
    default = Loadout(strict_mcp=True, tool_search=mode)

    invocation = turn(tmp_path, default, (CHROME.on,), lead=True)

    assert "--tools" not in invocation.argv and "ToolSearch" not in invocation.argv
    assert server(invocation) == PLAIN_SERVER


@pytest.mark.parametrize("mode", ["auto", "always"])
def test_a_list_that_names_tool_search_gets_no_duplicate(tmp_path: Path, mode: str) -> None:
    named = replace(LEAD, tools=("Read", "ToolSearch", "Bash"), tool_search=mode)

    invocation = turn(tmp_path, named, (CHROME.on,), lead=True)

    assert tools(invocation) == "Read,ToolSearch,Bash"
    assert server(invocation) == PLAIN_SERVER  # nothing added, so the list's own choice stays


def test_no_built_in_tools_but_tool_search_under_the_same_rule(tmp_path: Path) -> None:
    none = replace(WORKER, tools=())

    assert tools(turn(tmp_path, none)) == ""  # as before
    assert tools(turn(tmp_path, none, (CHROME.on,))) == "ToolSearch"
    assert tools(turn(tmp_path, replace(none, tool_search="never"), (CHROME.on,))) == ""
    assert tools(turn(tmp_path, replace(none, tool_search="always"), lead=True)) == "ToolSearch"


def test_it_comes_after_the_memory_guards_write_and_edit(tmp_path: Path) -> None:
    keeper = replace(LEAD, memory=True)

    on = turn(tmp_path, keeper, (CHROME.on,), lead=True)
    plain = turn(tmp_path, keeper, lead=True)

    assert tools(on) == "Bash,Read,Glob,Grep,Write,Edit,ToolSearch"
    assert tools(plain) == "Bash,Read,Glob,Grep,Write,Edit"
    assert on.canary is not None and "--settings" in on.argv
    assert server(on) == {**PLAIN_SERVER, "alwaysLoad": True}
    # Relaunched without memory (hooks off): the same rule still applies.
    plain_on = turn(tmp_path, LEAD, (CHROME.on,), lead=True)
    assert tools(plain_on) == "Bash,Read,Glob,Grep,ToolSearch"


def test_a_plugin_capability_brings_mcp_tools_only_when_it_says_so(tmp_path: Path) -> None:
    with_field = Capability(
        name="tracker",
        label="Tracker",
        description="an issue tracker's MCP tools",
        reaches="the tracker",
        providers={"claude": Support(on=Wiring(argv=("--tracker",), mcp_tools=True))},
    )
    without_field = Capability(
        name="staging",
        label="Staging API",
        description="points the tools at a staging API",
        reaches="the staging API",
        providers={"claude": Support(on=Wiring(env={"MY_APP_API": "staging"}))},
    )

    brings = turn(tmp_path, LEAD, (with_field.providers["claude"].on,), lead=True)
    plain = turn(tmp_path, LEAD, (without_field.providers["claude"].on,), lead=True)

    assert Wiring().mcp_tools is False
    assert tools(brings) == "Bash,Read,Glob,Grep,ToolSearch" and "--tracker" in brings.argv
    assert server(brings)["alwaysLoad"] is True
    assert plain.argv == turn(tmp_path, LEAD, lead=True).argv
    assert plain.env == {"MY_APP_API": "staging", **NO_BACKGROUND_ENV}


@pytest.mark.parametrize("mode", ["auto", "always", "never"])
def test_codex_is_not_affected(tmp_path: Path, mode: str) -> None:
    loadout = replace(LEAD, tool_search=mode)
    wiring = Wiring(argv=("-c", "tools.example=true"), mcp_tools=True)

    found = turn(tmp_path, loadout, (wiring,), lead=True, provider="codex")
    plain = turn(
        tmp_path, replace(LEAD, tool_search="never"), (wiring,), lead=True, provider="codex"
    )

    assert found.argv == plain.argv
    assert "ToolSearch" not in " ".join(found.argv) and "alwaysLoad" not in " ".join(found.argv)


def test_a_compaction_turn_gets_the_same_flags(tmp_path: Path) -> None:
    compacting = replace(LEAD, slash_commands=True)  # as the runner passes it

    normal = turn(tmp_path, LEAD, (CHROME.on,), lead=True)
    compact = turn(tmp_path, compacting, (CHROME.on,), lead=True)

    assert tools(compact) == tools(normal) == "Bash,Read,Glob,Grep,ToolSearch"
    assert server(compact) == server(normal) == {**PLAIN_SERVER, "alwaysLoad": True}


def test_the_rule_on_its_own() -> None:
    chrome = (CHROME.on,)
    assert tool_search(LEAD, own_server=False) == (False, False)
    assert tool_search(LEAD, own_server=True) == (False, False)
    assert tool_search(LEAD, own_server=False, wirings=chrome) == (True, False)
    assert tool_search(LEAD, own_server=True, wirings=chrome) == (True, True)
    always = replace(LEAD, tool_search="always")
    assert tool_search(always, own_server=True) == (True, False)
    assert tool_search(always, own_server=False) == (False, False)
    assert loadout_flags(LEAD, search=True)[:2] == ["--tools", "Bash,Read,Glob,Grep,ToolSearch"]
    assert loadout_flags(Loadout(), search=True) == []


# -- the runner and `spartito chat` ---------------------------------------------------------


def test_every_kind_of_turn_gets_the_loadouts_tool_search(orch: Orchestrator, project: str) -> None:
    (config.home() / "loadout.toml").write_text('[default]\ntool_search = "always"\n')

    agent, run = orch.spawn(project, "CONTEXT 780", worktree=False)
    done, _ = orch.wait([agent.id], timeout=30)
    compaction = orch.compact(agent.id)
    finished, _ = orch.wait([agent.id], timeout=30)

    assert done and finished
    for each in (run, compaction):
        seen = json.loads((config.run_dir(each.id) / "ctx.json").read_text())
        assert seen["tool_search"] == "always"


def test_chat_adds_tool_search_only_when_the_loadout_says_always(
    orch: Orchestrator,
    exec_argv: list[list[str]],  # noqa: F811
) -> None:
    runner = CliRunner()
    plain = runner.invoke(cli.app, ["chat", "--here"])
    (config.home() / "loadout.toml").write_text('[orchestrator]\ntool_search = "always"\n')
    always = runner.invoke(cli.app, ["chat", "--here"])

    assert plain.exit_code == 0 and always.exit_code == 0
    (auto_argv, always_argv) = exec_argv
    assert option(auto_argv, "--tools") == "Bash,Read,Glob,Grep"  # no capabilities in chat
    assert option(always_argv, "--tools") == "Bash,Read,Glob,Grep,ToolSearch"
    assert "alwaysLoad" not in option(always_argv, "--mcp-config")
