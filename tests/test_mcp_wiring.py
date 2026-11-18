"""MCP servers added in Spartito, wired into Claude's turns: one private MCP configuration
file per turn with servers (providers/claude.py, Invocation.private_files), written and
removed by the runner; the pre-turn check of their `${NAME}` references; the statuses the
CLI reports, recorded with the turn (runner.py). Fake CLIs only (fakeprovider.FakeClaude)."""

from __future__ import annotations

import json
import os
import time
from dataclasses import replace
from pathlib import Path

import pytest
from fakeprovider import FakeClaude, claude_runs
from test_interfaces import option

from orchestrator import capabilities, config, providers, runner
from orchestrator.loadout import Loadout
from orchestrator.models import Agent, RunStatus
from orchestrator.providers import McpServer, RunContext, get
from orchestrator.providers.base import Wiring, server_references
from orchestrator.providers.claude import CHROME, NO_BACKGROUND_ENV
from orchestrator.service import Orchestrator

URL = "https://mcp.example.com/mcp"
DOCS_DB = {
    "command": "docs-db-mcp",
    "args": ["--read-only"],
    "env": {"DOCS_DB_URL": "${DOCS_DB_URL}"},
}
REMOTE = {"type": "http", "url": URL, "headers": {"Authorization": "${DOCS_TOKEN}"}}
SERVERS = (
    Wiring(mcp_servers={"docs-db": DOCS_DB}, mcp_tools=True),
    Wiring(mcp_servers={"remote-docs": REMOTE}, mcp_tools=True),
)
WORKER = Loadout(
    tools=("Bash", "Read", "Edit", "Write", "Glob", "Grep"),
    strict_mcp=True,
    setting_sources=("project", "local"),
    slash_commands=False,
    codex_user_config=False,
)
LEAD = replace(WORKER, tools=("Bash", "Read", "Glob", "Grep"))
OWN = McpServer("py", ["-m", "orchestrator.mcp_server"], {"ORCH_AGENT_ID": "a-1"})
OWN_ENTRY = {"command": "py", "args": ["-m", "orchestrator.mcp_server"], "env": OWN.env}


def turn(
    tmp_path: Path,
    wirings: tuple[Wiring, ...] = SERVERS,
    lead: bool = False,
    loadout: Loadout | None = None,
    path: Path | None = None,
):
    agent = Agent(
        id="fix-login-3f2a",
        project="my-app",
        name="fix-login",
        role="lead" if lead else "worker",
        provider="claude",
        cwd="/w",
        session_id="sid",  # resumed: no random session id on the command line
    )
    ctx = RunContext(
        agent=agent,
        prompt="do it",
        system_prompt="ROLE",
        scratch=tmp_path,
        mcp=OWN if lead else None,
        lead=lead,
        loadout=loadout or (LEAD if lead else WORKER),
        wirings=wirings,
        mcp_config=path or tmp_path / "private" / "mcp-7.json",
    )
    return get("claude").invocation(ctx)


def written(invocation) -> dict:
    """The configuration the turn's file gets, parsed."""
    (content,) = invocation.private_files.values()
    return json.loads(content)["mcpServers"]


# -- the configuration (provider) ----------------------------------------------------------


def test_a_worker_with_a_stdio_and_an_http_server(tmp_path: Path) -> None:
    path = tmp_path / "private" / "mcp-7.json"
    invocation = turn(tmp_path, path=path)

    assert invocation.argv == [
        "claude", "-p", "--output-format", "stream-json", "--verbose",
        "--resume", "sid", "--permission-mode", "auto",
        "--tools", "Bash,Read,Edit,Write,Glob,Grep,ToolSearch",
        "--strict-mcp-config", "--setting-sources", "project,local",
        "--disable-slash-commands", "--append-system-prompt", "ROLE",
        "--mcp-config", str(path),
    ]  # fmt: skip
    assert list(invocation.private_files) == [path]
    # As given, ${NAME} unexpanded: Claude Code expands them from the turn's environment.
    assert written(invocation) == {"docs-db": DOCS_DB, "remote-docs": REMOTE}
    assert invocation.env == {}
    assert not path.exists()  # the provider writes nothing: the runner does


def test_a_lead_gets_its_own_server_in_the_same_file(tmp_path: Path) -> None:
    path = tmp_path / "private" / "mcp-7.json"
    invocation = turn(tmp_path, lead=True, path=path)

    assert invocation.argv == [
        "claude", "-p", "--output-format", "stream-json", "--verbose",
        "--resume", "sid", "--permission-mode", "auto",
        "--tools", "Bash,Read,Glob,Grep,ToolSearch",
        "--strict-mcp-config", "--setting-sources", "project,local",
        "--disable-slash-commands", "--append-system-prompt", "ROLE",
        "--mcp-config", str(path), "--allowedTools", "mcp__orch",
    ]  # fmt: skip
    assert written(invocation) == {
        "orch": {**OWN_ENTRY, "alwaysLoad": True},
        "docs-db": DOCS_DB,
        "remote-docs": REMOTE,
    }
    assert invocation.env == NO_BACKGROUND_ENV
    # Nothing on the command line holds a server's entry.
    assert not [arg for arg in invocation.argv if "mcpServers" in arg]


@pytest.mark.parametrize(
    ("mode", "worker_tools", "lead_tools", "always_load"),
    [
        ("auto", "Bash,Read,Edit,Write,Glob,Grep,ToolSearch", "Bash,Read,Glob,Grep,ToolSearch", True),
        ("always", "Bash,Read,Edit,Write,Glob,Grep,ToolSearch", "Bash,Read,Glob,Grep,ToolSearch", False),
        ("never", "Bash,Read,Edit,Write,Glob,Grep", "Bash,Read,Glob,Grep", False),
    ],
)  # fmt: skip
def test_servers_bring_tool_search_by_the_loadout(
    tmp_path: Path, mode: str, worker_tools: str, lead_tools: str, always_load: bool
) -> None:
    worker = turn(tmp_path, loadout=replace(WORKER, tool_search=mode))
    lead = turn(tmp_path, lead=True, loadout=replace(LEAD, tool_search=mode, memory=False))

    assert option(worker.argv, "--tools") == worker_tools
    assert option(lead.argv, "--tools") == lead_tools
    assert ("alwaysLoad" in written(lead)["orch"]) is always_load


def test_empty_args_env_and_headers_are_left_out(tmp_path: Path) -> None:
    bare = (
        Wiring(mcp_servers={"docs-db": {"command": "docs-db-mcp", "args": [], "env": {}}}),
        Wiring(mcp_servers={"remote-docs": {"type": "http", "url": URL, "headers": {}}}),
    )
    assert written(turn(tmp_path, bare)) == {
        "docs-db": {"command": "docs-db-mcp"},
        "remote-docs": {"type": "http", "url": URL},
    }


def test_two_wirings_naming_one_server_give_it_once_as_the_last_says(tmp_path: Path) -> None:
    other = {**DOCS_DB, "args": ["--read-write"]}
    twice = (*SERVERS, Wiring(mcp_servers={"docs-db": other}, mcp_tools=True))

    assert written(turn(tmp_path, twice)) == {"docs-db": other, "remote-docs": REMOTE}


def test_a_server_named_orch_never_replaces_spartitos_own(tmp_path: Path) -> None:
    named = (Wiring(mcp_servers={"orch": DOCS_DB, "docs-db": DOCS_DB}),)

    assert written(turn(tmp_path, named, lead=True)) == {"orch": OWN_ENTRY, "docs-db": DOCS_DB}
    assert written(turn(tmp_path, named)) == {"docs-db": DOCS_DB}


def test_without_servers_the_command_line_is_the_one_of_before(tmp_path: Path) -> None:
    for lead in (False, True):
        plain = turn(tmp_path, (CHROME.off,), lead=lead)
        empty = turn(tmp_path, (CHROME.off, Wiring(mcp_servers={})), lead=lead)
        assert empty.argv == plain.argv and empty.env == plain.env
        assert empty.private_files == plain.private_files == {}
    alone = turn(tmp_path, (), lead=True)
    assert option(alone.argv, "--mcp-config") == json.dumps({"mcpServers": {"orch": OWN_ENTRY}})


def test_servers_need_a_path_for_the_file(tmp_path: Path) -> None:
    agent = Agent(id="a", project=None, name="a", role="worker", provider="claude", cwd="/w")
    ctx = RunContext(agent=agent, prompt="p", system_prompt="", scratch=tmp_path, wirings=SERVERS)
    with pytest.raises(ValueError, match="needs a path"):
        get("claude").invocation(ctx)


def test_the_init_line_gives_the_servers_statuses() -> None:
    line = {
        "type": "system",
        "subtype": "init",
        "session_id": "s-1",
        "model": "m",
        "mcp_servers": [
            {"name": "orch", "status": "connected"},
            {"name": "docs-db", "status": "failed"},
            {"name": "broken"},
            "junk",
        ],
    }
    (event,) = get("claude").parse(json.dumps(line))
    (older,) = get("claude").parse(json.dumps({**line, "mcp_servers": None}))

    assert (event.kind, event.session_id) == ("session", "s-1")
    assert event.mcp == {"orch": "connected", "docs-db": "failed"}
    assert older.mcp is None


# -- the pre-turn check --------------------------------------------------------------------


def test_the_references_a_server_needs() -> None:
    entry = {
        "command": "${TOOLS_DIR}/docs-db-mcp",
        "args": ["--db", "${DOCS_DB_URL}", "--mode=${MODE:-read}", "--plain"],
        "env": {"DOCS_DB_URL": "${DOCS_DB_URL}", "LOG": "${LOG_LEVEL}"},
    }
    remote = {"type": "http", "url": "https://${DOCS_HOST}/mcp", "headers": {"X-Key": "${KEY}"}}

    assert server_references(entry) == ["TOOLS_DIR", "DOCS_DB_URL", "LOG_LEVEL"]
    assert server_references(remote) == ["DOCS_HOST", "KEY"]
    assert server_references({"command": "plain"}) == []


def resolution(*wirings: Wiring, chrome: bool = False) -> capabilities.Resolution:
    names = [name for wiring in wirings for name in wiring.mcp_servers]
    items = [
        capabilities.Item(f"mcp:{name}", name, "on", "global", None, False, kind="mcp")
        for name in names
    ]
    if chrome:
        items.append(capabilities.Item("chrome", "Claude in Chrome", "on", "global", None, False))
    return capabilities.Resolution(tuple(items), (*wirings, *((CHROME.on,) if chrome else ())))


@pytest.mark.parametrize(
    ("entry", "env", "missing"),
    [
        (DOCS_DB, {"DOCS_DB_URL": "postgres://db"}, None),
        (DOCS_DB, {"DOCS_DB_URL": ""}, None),  # set, if empty
        (DOCS_DB, {}, "DOCS_DB_URL is not set"),
        ({"command": "${TOOLS_DIR}/docs-db-mcp"}, {}, "TOOLS_DIR is not set"),
        ({"command": "docs-db-mcp", "args": ["${A}", "${B:-x}", "${C}"]}, {}, "A, C are not set"),
        ({"type": "http", "url": "https://${DOCS_HOST}/mcp"}, {}, "DOCS_HOST is not set"),
        (REMOTE, {}, "DOCS_TOKEN is not set"),
    ],
)
def test_a_server_whose_variable_is_not_set_is_left_out(
    entry: dict, env: dict, missing: str | None
) -> None:
    granted = resolution(Wiring(mcp_servers={"docs-db": entry}, mcp_tools=True))

    found, unset = runner.without_unset_servers(granted, env)

    if missing is None:
        assert found == granted and unset == {}
        return
    assert unset == {"docs-db": missing}
    assert found.wirings == ()  # nothing left to bring: no MCP tools, no ToolSearch
    assert found.on == [] and found.skipped == {"mcp:docs-db": missing}
    assert capabilities.describe_record(found.record()) == (
        f"capabilities for this turn: none; skipped: mcp:docs-db ({missing})"
    )


def test_only_the_server_with_an_unset_variable_is_left_out() -> None:
    granted = resolution(*SERVERS, chrome=True)

    found, unset = runner.without_unset_servers(granted, {"DOCS_TOKEN": "t"})

    assert unset == {"docs-db": "DOCS_DB_URL is not set"}
    assert found.wirings == (SERVERS[1], CHROME.on)
    assert found.on == ["mcp:remote-docs", "chrome"]


@pytest.mark.parametrize("chrome", [False, True])
def test_all_servers_left_out_leaves_the_command_line_of_a_turn_without_them(
    tmp_path: Path, chrome: bool
) -> None:
    found, _ = runner.without_unset_servers(resolution(*SERVERS, chrome=chrome), {})
    plain = resolution(chrome=chrome)

    for mode in ("auto", "always", "never"):
        for lead in (False, True):
            loadout = replace(LEAD if lead else WORKER, tool_search=mode, memory=False)
            left = turn(tmp_path, found.wirings, lead=lead, loadout=loadout)
            without = turn(tmp_path, plain.wirings, lead=lead, loadout=loadout)
            assert left.argv == without.argv and left.private_files == {}, (mode, lead)
            # ToolSearch then comes only from what else brings MCP tools (chrome), or for a
            # lead's own server under `always`.
            search = mode != "never" and (chrome or (lead and mode == "always"))
            assert ("ToolSearch" in option(left.argv, "--tools")) is search, (mode, lead)


# -- the private file (runner) -------------------------------------------------------------


def test_the_file_is_private_replaces_a_leftover_and_is_removed(
    orch: Orchestrator, project: str
) -> None:
    path = config.mcp_config_file(41)
    path.parent.mkdir(parents=True)
    path.parent.chmod(0o755)
    path.write_text("a leftover")
    path.chmod(0o644)
    gone = config.mcp_config_file(42)  # of a run that does not exist (any more)
    gone.write_text("{}")
    queued = orch.store.add_run(orch.create_agent(project).id, "later")
    kept = config.mcp_config_file(queued.id)  # of an active run
    kept.write_text("{}")

    with runner.PrivateFiles(orch.store, 41, {path: '{"mcpServers": {}}'}):
        assert path.read_text() == '{"mcpServers": {}}'
        assert path.stat().st_mode & 0o777 == 0o600
        assert path.parent.stat().st_mode & 0o777 == 0o700
        assert not gone.exists() and kept.exists()
    assert not path.exists() and kept.exists()


def test_no_file_no_directory_without_servers(orch: Orchestrator) -> None:
    with runner.PrivateFiles(orch.store, 41, {}):
        pass
    assert not config.mcp_config_file(41).parent.exists()


# -- turns of the fake claude --------------------------------------------------------------


@pytest.fixture
def claude_turns(orch: Orchestrator, project: str, monkeypatch: pytest.MonkeyPatch) -> str:
    """FakeClaude as `claude`, here and in runners; the servers docs-db (stdio) and
    remote-docs (http) added and on globally, their variables set. Returns the project."""
    monkeypatch.setenv(
        "ORCH_PROVIDERS", "fake=fakeprovider:FakeProvider,claude=fakeprovider:FakeClaude"
    )
    monkeypatch.setitem(providers.PROVIDERS, "claude", FakeClaude())
    monkeypatch.setenv("DOCS_DB_URL", "postgres://db.example.com/docs")
    monkeypatch.setenv("DOCS_TOKEN", "Bearer not-a-secret")
    monkeypatch.setenv("FAKE_CLAUDE_ENV", "DOCS_DB_URL,DOCS_TOKEN")
    orch.mcp_add("docs-db", **{key: DOCS_DB[key] for key in ("command", "args", "env")})
    orch.mcp_add("remote-docs", url=URL, headers=REMOTE["headers"])
    orch.set_capability("mcp:docs-db", True)
    orch.set_capability("mcp:remote-docs", True)
    return project


def finished(orch: Orchestrator, agent_id: str) -> dict:
    idle, (view,) = orch.wait([agent_id], timeout=60)
    assert idle
    return view


def record(orch: Orchestrator, run_id: int) -> dict:
    return json.loads(orch.store.meta(run_id).capabilities)


def last_statuses(view: dict) -> dict:
    return {
        item["name"]: item["last_status"]
        for item in view["capabilities"]["items"]
        if item["kind"] == "mcp"
    }


def test_a_worker_turn_gets_the_file_and_records_the_statuses(
    orch: Orchestrator, claude_turns: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    status = {"remote-docs": "needs-auth", "claude-in-chrome": "connected"}
    monkeypatch.setenv("FAKE_MCP_STATUS", json.dumps(status))

    agent, run = orch.spawn(claude_turns, "hello", provider="claude", worktree=False)
    view = finished(orch, agent.id)

    assert orch.store.get_run(run.id).status is RunStatus.DONE
    (seen,) = claude_runs(config.run_dir(run.id))
    path = str(config.mcp_config_file(run.id))
    assert "--strict-mcp-config" in seen["argv"]
    assert seen["argv"][-2:] == ["--mcp-config", path]
    assert seen["config_path"] == path
    assert seen["config"] == {"mcpServers": {"docs-db": DOCS_DB, "remote-docs": REMOTE}}
    assert (seen["mode"], seen["dir_mode"]) == ("0o600", "0o700")
    assert "ToolSearch" in option(seen["argv"], "--tools").split(",")
    assert "--allowedTools" not in seen["argv"]  # the permission mode decides each call
    # The file never held a value; the CLI got them in its environment.
    assert "postgres" not in json.dumps(seen["config"]) and seen["env"]["DOCS_DB_URL"]
    assert not Path(path).exists()
    assert record(orch, run.id) == {
        "on": ["mcp:docs-db", "mcp:remote-docs"],
        "skipped": {},
        "mcp": {"docs-db": "connected", "remote-docs": "needs-auth"},  # not claude-in-chrome
    }
    assert last_statuses(view) == {"mcp:docs-db": "connected", "mcp:remote-docs": "needs-auth"}


def test_a_lead_turn_merges_its_own_server_into_the_file(
    orch: Orchestrator, claude_turns: str
) -> None:
    agent, run = orch.spawn(claude_turns, "plan", provider="claude", role="lead", worktree=False)
    finished(orch, agent.id)

    (seen,) = claude_runs(config.run_dir(run.id))
    servers = seen["config"]["mcpServers"]
    assert list(servers) == ["orch", "docs-db", "remote-docs"]
    assert servers["orch"]["alwaysLoad"] is True
    assert servers["orch"]["env"]["ORCH_AGENT_ID"] == agent.id
    assert option(seen["argv"], "--mcp-config") == str(config.mcp_config_file(run.id))
    assert option(seen["argv"], "--allowedTools") == "mcp__orch"
    assert seen["mode"] == "0o600"
    assert seen["prompt"] == "plan"  # through the memory canary, as before
    assert record(orch, run.id)["mcp"] == {"docs-db": "connected", "remote-docs": "connected"}
    assert not config.mcp_config_file(run.id).exists()


@pytest.mark.parametrize("how", ["FAIL", "missing"])
def test_the_file_is_gone_after_a_failing_turn(
    orch: Orchestrator, claude_turns: str, monkeypatch: pytest.MonkeyPatch, how: str
) -> None:
    if how == "missing":  # the CLI cannot start: an exception in the runner
        monkeypatch.setenv("FAKE_CLAUDE_MISSING", "1")
    agent, run = orch.spawn(claude_turns, how, provider="claude", worktree=False)
    finished(orch, agent.id)

    assert orch.store.get_run(run.id).status is RunStatus.FAILED
    assert len(claude_runs(config.run_dir(run.id))) == (how == "FAIL")
    assert not config.mcp_config_file(run.id).exists()
    assert list(config.private_dir().iterdir()) == []


def test_the_file_is_gone_after_a_stopped_turn(orch: Orchestrator, claude_turns: str) -> None:
    agent, run = orch.spawn(claude_turns, "SLEEP 30", provider="claude", worktree=False)
    deadline = time.monotonic() + 30
    while not claude_runs(config.run_dir(run.id)):
        assert time.monotonic() < deadline, "the turn did not start"
        time.sleep(0.05)
    assert config.mcp_config_file(run.id).exists()

    orch.stop(agent.id)
    finished(orch, agent.id)

    assert orch.store.get_run(run.id).status is RunStatus.STOPPED
    assert not config.mcp_config_file(run.id).exists()


def test_a_server_whose_variable_is_not_set_is_skipped_with_a_note(
    orch: Orchestrator, claude_turns: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("DOCS_DB_URL")

    agent, run = orch.spawn(claude_turns, "hello", provider="claude", worktree=False)
    view = finished(orch, agent.id)

    (seen,) = claude_runs(config.run_dir(run.id))
    assert seen["config"] == {"mcpServers": {"remote-docs": REMOTE}}
    assert record(orch, run.id) == {
        "on": ["mcp:remote-docs"],
        "skipped": {"mcp:docs-db": "DOCS_DB_URL is not set"},
        "mcp": {"docs-db": "skipped: DOCS_DB_URL is not set", "remote-docs": "connected"},
    }
    notes = [event.text for event in orch.store.events(agent.id) if event.kind == "info"]
    assert (
        "capabilities for this turn: mcp:remote-docs; skipped: mcp:docs-db (DOCS_DB_URL is not set)"
    ) in notes
    assert last_statuses(view)["mcp:docs-db"] == "skipped: DOCS_DB_URL is not set"


def test_all_servers_skipped_runs_the_turn_without_them(
    orch: Orchestrator, claude_turns: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("DOCS_DB_URL")
    monkeypatch.delenv("DOCS_TOKEN")

    agent, run = orch.spawn(claude_turns, "hello", provider="claude", worktree=False)
    finished(orch, agent.id)

    assert orch.store.get_run(run.id).status is RunStatus.DONE
    (seen,) = claude_runs(config.run_dir(run.id))
    assert "--mcp-config" not in seen["argv"] and "config_path" not in seen
    assert option(seen["argv"], "--tools") == "Bash,Read,Edit,Write,Glob,Grep"  # no ToolSearch
    assert record(orch, run.id)["mcp"] == {
        "docs-db": "skipped: DOCS_DB_URL is not set",
        "remote-docs": "skipped: DOCS_TOKEN is not set",
    }


def test_a_server_the_cli_does_not_report_gets_no_status(
    orch: Orchestrator, claude_turns: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_MCP_STATUS", json.dumps({"remote-docs": None}))
    agent, run = orch.spawn(claude_turns, "hello", provider="claude", worktree=False)
    view = finished(orch, agent.id)

    assert record(orch, run.id)["mcp"] == {"docs-db": "connected"}
    assert last_statuses(view) == {"mcp:docs-db": "connected", "mcp:remote-docs": None}


def test_a_turn_without_servers_records_as_before(
    orch: Orchestrator, claude_turns: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    orch.set_capability("mcp:docs-db", None)
    orch.set_capability("mcp:remote-docs", None)
    agent, run = orch.spawn(claude_turns, "hello", provider="claude", worktree=False)
    finished(orch, agent.id)

    (seen,) = claude_runs(config.run_dir(run.id))
    assert "--mcp-config" not in seen["argv"]
    assert record(orch, run.id) == {"on": [], "skipped": {}}
    assert not config.private_dir().exists() or list(config.private_dir().iterdir()) == []


def test_a_compaction_turn_gets_the_servers_too(orch: Orchestrator, claude_turns: str) -> None:
    agent, _ = orch.spawn(claude_turns, "hello", provider="claude", worktree=False)
    finished(orch, agent.id)
    compaction = orch.compact(agent.id)
    finished(orch, agent.id)

    assert orch.store.get_run(compaction.id).status is RunStatus.DONE
    (seen,) = claude_runs(config.run_dir(compaction.id))
    assert seen["prompt"].startswith("/compact")
    assert "--resume" in seen["argv"] and "--disable-slash-commands" not in seen["argv"]
    assert list(seen["config"]["mcpServers"]) == ["docs-db", "remote-docs"]
    assert not config.mcp_config_file(compaction.id).exists()


def test_a_server_off_or_removed_is_gone_from_the_next_turn(
    orch: Orchestrator, claude_turns: str
) -> None:
    agent, first = orch.spawn(claude_turns, "one", provider="claude", worktree=False)
    finished(orch, agent.id)
    orch.set_capability("mcp:remote-docs", False)
    second = orch.send(agent.id, "two")
    finished(orch, agent.id)
    orch.mcp_remove("docs-db")
    third = orch.send(agent.id, "three")
    view = finished(orch, agent.id)

    runs = [claude_runs(config.run_dir(run.id))[0] for run in (first, second, third)]
    assert list(runs[0]["config"]["mcpServers"]) == ["docs-db", "remote-docs"]
    assert runs[1]["config"] == {"mcpServers": {"docs-db": DOCS_DB}}
    assert "--resume" in runs[2]["argv"] and "--mcp-config" not in runs[2]["argv"]
    assert "ToolSearch" not in option(runs[2]["argv"], "--tools")
    assert record(orch, third.id) == {"on": [], "skipped": {}}
    assert last_statuses(view) == {"mcp:remote-docs": None}  # off; docs-db no longer exists
    assert list(config.private_dir().iterdir()) == []


def test_a_codex_turn_skips_the_servers_with_a_note(
    orch: Orchestrator, claude_turns: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The fake provider as `codex`, as test_attribution runs it.
    monkeypatch.setenv(
        "ORCH_PROVIDERS", "fake=fakeprovider:FakeProvider,codex=fakeprovider:FakeProvider"
    )
    monkeypatch.setitem(providers.PROVIDERS, "codex", providers.get("fake"))

    agent, run = orch.spawn(claude_turns, "hello", provider="codex", worktree=False)
    finished(orch, agent.id)

    assert orch.store.get_run(run.id).status is RunStatus.DONE
    seen = json.loads((config.run_dir(run.id) / "ctx.json").read_text())
    assert seen["wirings"] == []
    skipped = {
        "mcp:docs-db": "codex does not support it",
        "mcp:remote-docs": "codex does not support it",
    }
    assert record(orch, run.id) == {"on": [], "skipped": skipped}
    notes = [event.text for event in orch.store.events(agent.id) if event.kind == "info"]
    assert any("skipped: mcp:docs-db (codex does not support it)" in note for note in notes)
    assert not os.path.exists(config.mcp_config_file(run.id))
