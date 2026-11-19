"""The opencode provider (providers/opencode.py): its command line, the turn's configuration
(the OPENCODE_CONFIG file and OPENCODE_CONFIG_CONTENT), the loadout, MCP servers, the shell
wrapper that gives the agent's commands the user's XDG_CONFIG_HOME back, the stream, the
model list and the availability check; and turns through the runner on the fake CLI
(tests/fakecli/opencode.py). No real `opencode` runs."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from dataclasses import replace
from pathlib import Path

import click
import fakecli
import pytest
from conftest import OPENCODE_MODELS, REAL_PROBE
from fakecli import opencode as opencode_dialect
from fakeprovider import FakeOpencode, opencode_runs
from starlette.testclient import TestClient
from typer.testing import CliRunner

from orchestrator import cli, config, mcp_server, providers, roles, settings
from orchestrator.core import availability, setup
from orchestrator.errors import OrchestratorError
from orchestrator.loadout import Loadout
from orchestrator.models import Agent, RunStatus
from orchestrator.providers import McpServer, RunContext, opencode
from orchestrator.providers.base import Wiring
from orchestrator.providers.opencode import OpencodeProvider

# The real lookup; conftest replaces the module attribute with a recorded answer.
REAL_REQUEST_MODEL_LIST = opencode.request_model_list
STRICT = Loadout(strict_mcp=True, setting_sources=("project", "local"))  # as the fallback
OWN = McpServer(command="python", args=["-m", "orchestrator.mcp_server"], env={"A": "1"})
DOCS_DB = {"command": "docs-db", "args": ["--db", "${DOCS_DB:-main}"], "env": {"URL": "${DB_URL}"}}
REMOTE = {"type": "http", "url": "https://docs.example.com/mcp", "headers": {"X": "${TOKEN}"}}
SHOW_HOME = 'printf "%s" "${XDG_CONFIG_HOME-(unset)}"'


AGENT_ID = "fix-login-3f2a"


def context(
    tmp_path: Path, loadout: Loadout | None = None, agent_id: str = AGENT_ID, **agent_fields
) -> RunContext:
    agent = Agent(
        id=agent_id,
        project="my-app",
        name="a",
        role="worker",
        provider="opencode",
        cwd="/w",
        **agent_fields,
    )
    return RunContext(
        agent=agent,
        prompt="do it",
        system_prompt="ROLE",
        scratch=tmp_path,
        loadout=loadout or Loadout(),
        mcp_config=tmp_path / "private" / "mcp-7.json",
    )


def option(argv: list[str], name: str) -> str | None:
    return argv[argv.index(name) + 1] if name in argv else None


def turn_file(invocation) -> dict:
    """The OPENCODE_CONFIG file (the policies)."""
    (content,) = invocation.private_files.values()
    return json.loads(content)


def turn_config(invocation) -> dict:
    """OPENCODE_CONFIG_CONTENT: what a project's opencode.json cannot replace."""
    return json.loads(invocation.env["OPENCODE_CONFIG_CONTENT"])


def permissions(invocation) -> list[dict]:
    return turn_config(invocation)["permissions"]


def denied(invocation) -> list[str]:
    return [r["action"] for r in permissions(invocation) if r["effect"] == "deny"]


def policies(invocation) -> list[tuple[str, str, str]]:
    return [(p["action"], p["resource"], p["effect"]) for p in turn_file(invocation)["policies"]]


# -- the command line -------------------------------------------------------------------------


def test_a_new_session_gets_its_id_title_and_archived_mark(tmp_path: Path) -> None:
    invocation = OpencodeProvider().invocation(context(tmp_path))

    session = option(invocation.argv, "--session")
    assert re.fullmatch(r"ses_[0-9a-f]{26}", session)
    assert invocation.argv == [
        "opencode", "run", "--standalone", "--format", "json",
        "--session", session, "--archived", "--title", "fix-login-3f2a",
    ]  # fmt: skip
    assert invocation.stdin == "do it"  # the role prompt is in the instructions file
    path = tmp_path / "private" / "mcp-7.json"
    assert invocation.env == {
        "PWD": "/w",
        "OPENCODE_CONFIG": str(path),
        "OPENCODE_CONFIG_CONTENT": invocation.env["OPENCODE_CONFIG_CONTENT"],
    }
    assert list(invocation.private_files) == [path]
    assert invocation.notes == [] and invocation.canary is None
    assert option(OpencodeProvider().invocation(context(tmp_path)).argv, "--session") != session


def test_a_resumed_turn_keeps_its_session_and_takes_auto_model_and_effort(
    tmp_path: Path,
) -> None:
    ctx = context(tmp_path, session_id="ses_0123456789abcdef0123456789", permission="auto")
    ctx = replace(ctx, model="openrouter/openai/gpt-4.1-mini", effort="high")

    invocation = OpencodeProvider().invocation(ctx)

    assert invocation.argv == [
        "opencode", "run", "--standalone", "--format", "json",
        "--session", "ses_0123456789abcdef0123456789",
        "--auto", "--model", "openrouter/openai/gpt-4.1-mini#high",
    ]  # fmt: skip
    assert invocation.env["PWD"] == "/w"
    own = config.agent_files_dir(AGENT_ID)
    assert turn_config(invocation)["instructions"] == [str(own / opencode.INSTRUCTIONS)]


def test_effort_needs_a_model_and_a_model_keeps_its_own_variant(tmp_path: Path) -> None:
    alone = OpencodeProvider().invocation(replace(context(tmp_path), effort="high"))
    variant = replace(context(tmp_path), model="anthropic/claude-x#max", effort="low")

    assert "--model" not in alone.argv
    assert alone.notes == ["effort high is ignored: opencode needs a model for an effort"]
    assert (
        option(OpencodeProvider().invocation(variant).argv, "--model") == "anthropic/claude-x#max"
    )


def test_another_permission_mode_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="permission mode default or auto, not 'plan'"):
        OpencodeProvider().invocation(context(tmp_path, permission="plan"))
    explicit = OpencodeProvider().invocation(context(tmp_path, permission="default"))
    assert "--auto" not in explicit.argv


def test_a_turn_needs_a_path_for_its_configuration(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="needs a path"):
        OpencodeProvider().invocation(replace(context(tmp_path), mcp_config=None))


def test_the_answer_schema_is_asked_for_in_the_prompt(tmp_path: Path) -> None:
    schema = {"type": "object", "properties": {"ok": {"type": "boolean"}}}
    provider = OpencodeProvider()

    invocation = provider.invocation(replace(context(tmp_path), output_schema=schema))

    assert invocation.stdin.startswith("do it\n\n---\n\n")
    assert json.dumps(schema, indent=2) in invocation.stdin
    assert provider.stream().structured is True
    provider.invocation(context(tmp_path))
    assert provider.stream().structured is False


def test_the_attach_command_opens_the_session() -> None:
    agent = Agent(id="a", project="p", name="a", role="worker", provider="opencode", cwd="/w")
    agent.session_id = "ses_abc"

    assert OpencodeProvider().attach_command(agent) == ["opencode", "--session", "ses_abc"]


# -- the turn's configuration and the loadout -------------------------------------------------


def test_the_file_holds_the_policies_and_the_content_the_rest(tmp_path: Path) -> None:
    invocation = OpencodeProvider().invocation(context(tmp_path))

    # Policies hold from the file (loaded before the project's config); whatever a project
    # must not replace is in the content (loaded after it).
    assert set(turn_file(invocation)) == {"policies"}
    config = turn_config(invocation)
    assert set(config) == {"update", "instructions", "permissions"}
    assert config["update"] == "disable"
    (instructions,) = config["instructions"]
    assert Path(instructions).read_text() == "ROLE" and Path(instructions).is_absolute()
    # What the default loadout (it restricts nothing) still denies in every turn.
    assert denied(invocation) == [*opencode.ALWAYS_DENIED, "external_directory"]
    assert "plugin.use" not in {action for action, _, _ in policies(invocation)}


def test_every_deny_is_backed_by_a_policy(tmp_path: Path) -> None:
    loadout = Loadout(tools=("Read",), slash_commands=False)
    invocation = OpencodeProvider().invocation(context(tmp_path, loadout))

    for rule in permissions(invocation):
        if rule["effect"] == "deny":
            statement = ("tool.use", f"{rule['action']}:{rule['resource']}", "deny")
            assert statement in policies(invocation), rule


def test_a_tool_list_denies_the_actions_it_does_not_cover(tmp_path: Path) -> None:
    loadout = Loadout(tools=("Read", "Grep", "Glob", "Bash", "ToolSearch", "mcp__x"))

    invocation = OpencodeProvider().invocation(context(tmp_path, loadout))

    built_in = [a for a in denied(invocation) if a in opencode.BUILTIN_ACTIONS]
    assert built_in == ["edit", "webfetch", "websearch", "subagent", "skill"]
    assert invocation.notes == [
        "tools without a counterpart in opencode, ignored: ToolSearch, mcp__x"
    ]


def test_tools_none_and_default(tmp_path: Path) -> None:
    none = OpencodeProvider().invocation(context(tmp_path, Loadout(tools=())))
    default = OpencodeProvider().invocation(context(tmp_path, Loadout(tools=None)))
    every = Loadout(tools=("Bash", "Read", "Edit", "Glob", "Grep", "WebFetch", "WebSearch",
                           "Task", "Skill"))  # fmt: skip

    assert [a for a in denied(none) if a in opencode.BUILTIN_ACTIONS] == list(
        opencode.BUILTIN_ACTIONS
    )
    assert not set(denied(default)) & set(opencode.BUILTIN_ACTIONS)
    listed = OpencodeProvider().invocation(context(tmp_path, every))
    assert not set(denied(listed)) & set(opencode.BUILTIN_ACTIONS)


def test_without_slash_commands_skills_are_denied(tmp_path: Path) -> None:
    invocation = OpencodeProvider().invocation(context(tmp_path, Loadout(slash_commands=False)))

    assert "skill" in denied(invocation)
    assert ("skill.use", "*", "deny") in policies(invocation)


def test_the_shared_git_directory_is_the_only_one_outside(tmp_path: Path) -> None:
    shared = tmp_path / "repo" / ".git"
    shared.mkdir(parents=True)
    ctx = replace(context(tmp_path), writable_dirs=[str(shared)])

    invocation = OpencodeProvider().invocation(ctx)

    boundary = os.path.realpath(shared) + "/*"
    outside = [r for r in permissions(invocation) if r["action"] == "external_directory"]
    assert outside == [
        {"action": "external_directory", "resource": "*", "effect": "deny"},
        {"action": "external_directory", "resource": boundary, "effect": "allow"},
    ]
    assert policies(invocation)[-2:] == [
        ("tool.use", "external_directory:*", "deny"),
        ("tool.use", f"external_directory:{boundary}", "allow"),
    ]


def test_setting_sources_decide_project_and_user_configuration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", "/home/me/.config")
    user_only = Loadout(setting_sources=("user",))
    every = OpencodeProvider().invocation(context(tmp_path))
    user = OpencodeProvider().invocation(context(tmp_path, user_only))
    no_user = OpencodeProvider().invocation(context(tmp_path, STRICT))

    assert "OPENCODE_DISABLE_PROJECT_CONFIG" not in every.env
    assert user.env["OPENCODE_DISABLE_PROJECT_CONFIG"] == "1"
    assert "OPENCODE_DISABLE_PROJECT_CONFIG" not in no_user.env
    # With `user`: the user's global configuration and shell stay as they are.
    for kept in (every, user):
        assert "XDG_CONFIG_HOME" not in kept.env and "shell" not in turn_config(kept)
        assert ("plugin.use", "*", "deny") not in policies(kept)
    # Without: moved away to a home of the agent's; no plugins.
    own = config.agent_files_dir(AGENT_ID)
    assert no_user.env["XDG_CONFIG_HOME"] == str(own / opencode.CONFIG_HOME)
    assert Path(no_user.env["XDG_CONFIG_HOME"]).is_dir()
    assert turn_config(no_user)["shell"] == str(own / opencode.SHELL_WRAPPER)
    assert ("plugin.use", "*", "deny") in policies(no_user)


def test_a_moved_config_home_has_a_global_agents_file_in_place_of_claudes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """opencode reads `~/.claude/CLAUDE.md` when its config directory has no AGENTS.md
    (documented): without `user` a placeholder takes its place; with `user`, nothing."""
    monkeypatch.setenv("XDG_CONFIG_HOME", "/home/me/.config")
    kept = Loadout(setting_sources=("user", "project"))

    OpencodeProvider().invocation(context(tmp_path, kept, agent_id="kept-1a2b"))
    no_user = OpencodeProvider().invocation(context(tmp_path, STRICT, agent_id="moved-3c4d"))

    placeholder = Path(no_user.env["XDG_CONFIG_HOME"]) / "opencode" / "AGENTS.md"
    moved_home = config.agent_files_dir("moved-3c4d") / opencode.CONFIG_HOME
    assert placeholder == moved_home / "opencode" / "AGENTS.md"
    assert placeholder.read_text().strip()  # not empty: an empty file may count as none
    assert not list(config.agent_files_dir("kept-1a2b").rglob("AGENTS.md"))


def turn_files(invocation) -> list[Path]:
    """The files a turn's configuration names: its role prompt, its placeholder AGENTS.md
    and its shell wrapper."""
    home = Path(invocation.env["XDG_CONFIG_HOME"])
    (instructions,) = turn_config(invocation)["instructions"]
    return [
        Path(instructions),
        home / opencode.GLOBAL_AGENTS,
        Path(turn_config(invocation)["shell"]),
    ]


def test_an_agents_turns_find_their_files_at_the_same_paths_unwritten(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """opencode tells a resumed session that instructions at another path replace the old
    ones, with their whole text: the paths stay, and an unchanged file is not written."""
    monkeypatch.setenv("XDG_CONFIG_HOME", "/home/me/.config")
    first = OpencodeProvider().invocation(context(tmp_path, STRICT))
    files = turn_files(first)
    for path in files:
        os.utime(path, ns=(1_000_000_000, 1_000_000_000))  # as if written long ago

    resumed = context(tmp_path, STRICT, session_id="ses_0123456789abcdef0123456789")
    second = OpencodeProvider().invocation(resumed)

    assert turn_files(second) == files
    assert second.env["XDG_CONFIG_HOME"] == first.env["XDG_CONFIG_HOME"]
    assert second.env["OPENCODE_CONFIG_CONTENT"] == first.env["OPENCODE_CONFIG_CONTENT"]
    assert [path.stat().st_mtime_ns for path in files] == [1_000_000_000] * 3
    own = config.agent_files_dir(AGENT_ID)
    assert all(path.is_relative_to(own) for path in files)
    assert own.stat().st_mode & 0o777 == 0o700
    assert config.private_dir().stat().st_mode & 0o777 == 0o700


def test_two_agents_get_files_of_their_own(tmp_path: Path) -> None:
    one = OpencodeProvider().invocation(context(tmp_path, STRICT, agent_id="one-1a2b"))
    two = OpencodeProvider().invocation(context(tmp_path, STRICT, agent_id="two-3c4d"))

    assert not set(turn_files(one)) & set(turn_files(two))
    assert one.env["XDG_CONFIG_HOME"] != two.env["XDG_CONFIG_HOME"]


def test_a_changed_role_prompt_is_written_where_the_old_one_was(tmp_path: Path) -> None:
    first = OpencodeProvider().invocation(context(tmp_path))
    (path,) = turn_config(first)["instructions"]
    os.utime(path, ns=(1_000_000_000, 1_000_000_000))

    changed = replace(context(tmp_path), system_prompt="ROLE, changed")
    (again,) = turn_config(OpencodeProvider().invocation(changed))["instructions"]

    assert again == path and Path(path).read_text() == "ROLE, changed"
    assert Path(path).stat().st_mtime_ns != 1_000_000_000


# -- MCP servers --------------------------------------------------------------------------------


def test_a_lead_gets_its_own_server_and_the_servers_are_translated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", "/home/me/.config")
    monkeypatch.delenv("DOCS_DB", raising=False)
    wiring = Wiring(mcp_servers={"docs-db": DOCS_DB, "remote-docs": REMOTE, "orch": {}})
    ctx = replace(context(tmp_path, STRICT), mcp=OWN, lead=True, wirings=(wiring,))

    invocation = OpencodeProvider().invocation(ctx)

    servers = turn_config(invocation)["mcp"]["servers"]
    home = {"XDG_CONFIG_HOME": "/home/me/.config"}
    # Every field set, so that a project's entry of the same name keeps nothing.
    assert servers == {
        "orch": {
            "type": "local",
            "command": ["python", "-m", "orchestrator.mcp_server"],
            "environment": {"A": "1", **home},
            "disabled": False,
            "cwd": "/w",
            "codemode": True,
            "timeout": {"execution": opencode.OWN_SERVER_EXECUTION_MS},
        },
        "docs-db": {
            "type": "local",
            "command": ["docs-db", "--db", "main"],  # unset: the default, as written
            "environment": {"URL": "{env:DB_URL}", **home},
            "disabled": False,
        },
        "remote-docs": {
            "type": "remote",
            "url": "https://docs.example.com/mcp",
            "headers": {"X": "{env:TOKEN}"},
            "disabled": False,
        },
    }
    assert policies(invocation)[-5:-1] == [
        ("mcp.use", "*", "deny"),
        ("mcp.use", "orch", "allow"),
        ("mcp.use", "docs-db", "allow"),
        ("mcp.use", "remote-docs", "allow"),
    ]
    assert {"action": "orch_*", "resource": "*", "effect": "allow"} in permissions(invocation)
    # The content holds references only, never a variable's value.
    monkeypatch.setenv("DOCS_DB", "other")
    again = turn_config(OpencodeProvider().invocation(ctx))["mcp"]["servers"]["docs-db"]
    assert again["command"] == ["docs-db", "--db", "{env:DOCS_DB}"]


def test_a_value_the_cli_has_too_is_a_reference_but_the_agent_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """OPENCODE_CONFIG_CONTENT is inherited by the agent's commands: a value of the user's
    environment is named, not copied; who the server acts for is written as it is."""
    monkeypatch.setenv("XDG_DATA_HOME", "/home/me/.local/share")
    monkeypatch.setenv("ORCH_AGENT_ID", "fix-login-3f2a")
    own = McpServer(
        command="python",
        args=["-m", "orchestrator.mcp_server"],
        env={
            "XDG_DATA_HOME": "/home/me/.local/share",
            "ORCH_AGENT_ID": "fix-login-3f2a",
            "PATH": opencode.locate.search_path(),
            "ORCH_HOME": "/elsewhere",
        },
    )
    ctx = replace(context(tmp_path), mcp=own, lead=True)

    environment = turn_config(OpencodeProvider().invocation(ctx))["mcp"]["servers"]["orch"][
        "environment"
    ]

    assert environment == {
        "XDG_DATA_HOME": "{env:XDG_DATA_HOME}",
        "ORCH_AGENT_ID": "fix-login-3f2a",
        "PATH": "{env:PATH}",  # the CLI gets the install directories on PATH too
        "ORCH_HOME": "/elsewhere",  # not the CLI's: Spartito's own value
    }


def test_the_orch_server_waits_longer_than_its_longest_tool(tmp_path: Path) -> None:
    ctx = replace(context(tmp_path), mcp=OWN, lead=True)

    server = turn_config(OpencodeProvider().invocation(ctx))["mcp"]["servers"]["orch"]

    assert server["timeout"] == {"execution": 3_600_000}  # milliseconds
    assert server["timeout"]["execution"] > mcp_server.MAX_WAIT_SECONDS * 1000


def test_the_wirings_environment_counts_for_a_default(tmp_path: Path) -> None:
    wiring = Wiring(env={"DOCS_DB": "set"}, mcp_servers={"docs-db": DOCS_DB})
    ctx = replace(context(tmp_path), wirings=(wiring,))

    invocation = OpencodeProvider().invocation(ctx)

    server = turn_config(invocation)["mcp"]["servers"]["docs-db"]
    assert server["command"] == ["docs-db", "--db", "{env:DOCS_DB}"]
    assert "environment" in server and "XDG_CONFIG_HOME" not in server["environment"]
    assert invocation.env["DOCS_DB"] == "set"
    assert not any(p[0] == "mcp.use" for p in policies(invocation))  # `mcp = inherit`


# -- the user's XDG_CONFIG_HOME for the agent's commands (the shell wrapper) --------------------


def run_wrapper(invocation, env: dict[str, str]) -> str:
    """What a command of the agent sees as XDG_CONFIG_HOME when opencode runs it through the
    turn's `shell` with the turn's environment."""
    shell = turn_config(invocation)["shell"]
    turn = {**env, "XDG_CONFIG_HOME": invocation.env["XDG_CONFIG_HOME"]}
    done = subprocess.run([shell, "-c", SHOW_HOME], env=turn, capture_output=True, text=True)
    assert done.returncode == 0, done.stderr
    return done.stdout


@pytest.mark.parametrize("original", ["/home/me/.config", "/tmp/it's a $HOME `dir`"])
def test_the_wrapper_restores_the_users_value(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, original: str
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", original)

    invocation = OpencodeProvider().invocation(context(tmp_path, STRICT))

    assert invocation.env["XDG_CONFIG_HOME"] != original
    assert os.access(turn_config(invocation)["shell"], os.X_OK)
    assert run_wrapper(invocation, dict(os.environ)) == original


def test_the_wrapper_unsets_a_value_that_was_unset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("XDG_CONFIG_HOME")
    ctx = replace(context(tmp_path, STRICT), mcp=OWN, lead=True)

    invocation = OpencodeProvider().invocation(ctx)

    assert run_wrapper(invocation, dict(os.environ)) == "(unset)"
    servers = turn_config(invocation)["mcp"]["servers"]
    assert servers["orch"]["environment"]["XDG_CONFIG_HOME"] == str(Path.home() / ".config")


# -- the stream ---------------------------------------------------------------------------------

SID = "ses_0123456789abcdef0123456789"


def line(kind: str, **fields) -> str:
    return json.dumps({"type": kind, "timestamp": 1, "sessionID": SID, **fields})


def part(kind: str, **fields) -> dict:
    return {"messageID": "msg_1", "sessionID": SID, "type": kind, **fields}


def text(part_id: str, words: str) -> str:
    return line("text", part=part("text", id=part_id, text=words, time={"start": 1, "end": 2}))


def step_finish(
    tokens_in: int, out: int, read: int, write: int, cost: float, reasoning: int = 0
) -> str:
    tokens = {"input": tokens_in, "output": out, "reasoning": reasoning,
              "cache": {"read": read, "write": write}}  # fmt: skip
    return line(
        "step_finish", part=part("step-finish", id="prt_f", reason="stop", cost=cost, tokens=tokens)
    )


def tool(name: str, given: object, output: str = "", status: str = "completed", **state) -> str:
    state = {"status": status, "input": given, "output": output, "title": "", **state}
    return line("tool_use", part=part("tool", partID="prt_t", id="call_1", tool=name, state=state))


STEP_START = line("step_start", part=part("step-start", id="prt_s", snapshot="0"))


def read(*lines: str, code: int = 0, structured: bool = False) -> list[tuple]:
    """Each event of the lines and the end of the stream as (kind, text, extra)."""
    stream = opencode.Stream(structured=structured)
    events = [event for one in lines for event in stream.feed(one)] + stream.finish(code)
    return [
        (e.kind, e.text, e.session_id or (e.usage.context_tokens if e.kind == "context" else None))
        for e in events
    ]


def test_text_comes_per_part_when_the_step_ends() -> None:
    events = read(
        STEP_START,
        text("prt_1", "first"),
        text("prt_2", "second"),
        text("prt_1", "first, whole"),  # a part's text is its whole text
        step_finish(10, 2, 0, 0, 0.001),
        "not json",
    )

    assert events == [
        ("session", "", SID),
        ("text", "first, whole", None),
        ("text", "second", None),
        ("context", "", 10),
        ("info", "not json", None),
        ("result", "second", None),  # the last text of the turn
    ]


def test_a_tool_comes_on_completion_and_an_error_state_is_a_tool_result() -> None:
    done = tool("read", {"filePath": "README.md"}, output="# my-app")
    failed = tool("shell", {"command": "false"}, status="error", error="exit 1")

    assert read(done, failed)[1:3] == [
        ("tool", 'read {"filePath": "README.md"}', None),
        ("tool", 'shell {"command": "false"}', None),
    ]
    assert read(failed)[2] == ("tool_result", "error: exit 1", None)


def test_a_code_mode_call_names_the_tools_it_reports_before_the_code() -> None:
    code = "const a = await tools.orch.list_agents()\nreturn tools.docs_db.query(a)"
    calls = [{"tool": "orch.list_agents", "status": "completed"}, {"tool": "orch.list_agents"}]
    reported = tool(
        "execute", {"code": code}, output="[]", metadata={"metadata": {"toolCalls": calls}}
    )
    empty = tool("execute", {"code": code}, output="[]", metadata={"metadata": {"toolCalls": []}})

    assert read(reported)[1][1].startswith("execute (orch.list_agents) const a")
    # Nothing reported: the names the code holds.
    assert read(empty)[1][1].startswith("execute (orch.list_agents, docs_db.query) const a")


def test_a_code_mode_call_names_its_server_and_an_error_in_its_output() -> None:
    code = "return tools.docs_db.query({q: 1});"
    fine = tool("execute", {"code": code}, output='{"result": 5}')
    broken = tool(
        "execute", {"code": code}, output="TypeError: tools.docs_db.query is not a function"
    )

    assert read(fine)[1:-1] == [("tool", f"execute (docs_db.query) {code}", None)]
    assert read(broken)[2] == (
        "tool_result",
        "error: TypeError: tools.docs_db.query is not a function",
        None,
    )


def test_the_nested_error_fails_the_turn() -> None:
    error = {"type": "provider.no-route", "message": "no route for model x"}
    failed = line("error", error=error)

    events = opencode.Stream()
    for one in (STEP_START, failed):
        events.feed(one)
    (result,) = events.finish(1)

    assert (result.kind, result.is_error) == ("result", True)
    assert result.text == "no route for model x (provider.no-route)"
    assert read(line("error", sessionID="", error={"type": "unknown"}), code=1)[-1][:2] == (
        "result",
        "error (unknown)",
    )


def test_a_failure_without_an_error_line_leaves_the_text_to_the_runner() -> None:
    stream = opencode.Stream()
    (result,) = stream.finish(1)

    assert (result.kind, result.text, result.is_error) == ("result", "", True)


def test_usage_and_cost_are_summed_over_the_steps_and_context_is_the_last() -> None:
    stream = opencode.Stream()
    events = [
        event
        for one in (
            STEP_START,
            step_finish(100, 10, 1000, 50, 0.01, reasoning=5),
            step_finish(20, 30, 1200, 0, 0.02),
        )
        for event in stream.feed(one)
    ]
    (result,) = stream.finish(0)

    sizes = [event.usage.context_tokens for event in events if event.kind == "context"]
    assert sizes == [1150, 1220]  # input + cache read + cache write, per step
    usage = result.usage
    tokens = (usage.input_tokens, usage.output_tokens, usage.cache_read_tokens)
    assert tokens == (120, 45, 2200) and usage.cache_creation_tokens == 50
    assert result.cost_usd == pytest.approx(0.03) and usage.cost_usd == pytest.approx(0.03)
    assert usage.cumulative is False  # per turn, not session totals


@pytest.mark.parametrize("code", [143, -15])
def test_a_signal_exit_is_a_stop(code: int) -> None:
    stream = opencode.Stream()
    stream.feed(STEP_START)
    stream.feed(text("prt_1", "half"))

    *texts, result = stream.finish(code)

    assert [(e.kind, e.text) for e in texts] == [("text", "half")]
    assert result.is_error and result.text.startswith("stopped: opencode ended by a signal")
    assert result.usage is None  # a killed step reports nothing


def test_retries_and_background_jobs_are_info() -> None:
    retry = line("retry", attempt=2, nextRetryAt=5, error={"message": "overloaded"})
    wait = line("background_wait", jobs=[{"id": "j1", "kind": "shell", "title": "npm test"}])

    assert read(retry, wait)[1:3] == [
        ("info", "retry 2: overloaded", None),
        ("info", "waiting for background jobs: npm test", None),
    ]


def test_a_structured_answer_is_read_from_the_last_text() -> None:
    answer = 'Done.\n```json\n{"summary": "ok"}\n```'

    def result(words: str, structured: bool = True):
        stream = opencode.Stream(structured=structured)
        for one in (STEP_START, text("prt_1", words), step_finish(1, 1, 0, 0, 0)):
            stream.feed(one)
        return stream.finish(0)[-1]

    assert result(answer).structured == {"summary": "ok"}
    assert result(answer).text == answer
    assert result("no JSON, sorry").structured is None
    assert result(answer, structured=False).structured is None  # none was asked for


# -- turns through the runner on the fake CLI ---------------------------------------------------


@pytest.fixture
def opencode_turns(orch, project: str, monkeypatch: pytest.MonkeyPatch) -> str:
    """FakeOpencode as `opencode`, here and in runners; the user's XDG_CONFIG_HOME is the
    temporary one of conftest. Returns the project."""
    monkeypatch.setenv(
        "ORCH_PROVIDERS", "fake=fakeprovider:FakeProvider,opencode=fakeprovider:FakeOpencode"
    )
    monkeypatch.setitem(providers.PROVIDERS, "opencode", FakeOpencode())
    monkeypatch.setenv("FAKE_OPENCODE_ENV", "ORCH_HOME,PWD")
    return project


def finished(orch, agent_id: str) -> None:
    idle, _ = orch.wait([agent_id], timeout=60)
    assert idle


def test_a_worker_turn_and_its_resumed_turn(orch, opencode_turns: str) -> None:
    user_home = os.environ["XDG_CONFIG_HOME"]
    agent, first = orch.spawn(opencode_turns, "hello TOKENS 1000", provider="opencode")
    finished(orch, agent.id)

    run = orch.store.get_run(first.id)
    assert run.status is RunStatus.DONE, run.error
    (seen,) = opencode_runs(config.run_dir(first.id))
    first_seen = seen
    session = option(seen["argv"], "--session")
    assert seen["argv"][:4] == ["run", "--standalone", "--format", "json"]
    assert ["--archived", "--title", agent.id] == seen["argv"][6:9]
    assert orch.store.get_agent(agent.id).session_id == session
    cwd = orch.store.get_agent(agent.id).cwd
    assert seen["env"] == {"ORCH_HOME": os.environ["ORCH_HOME"], "PWD": cwd}
    # The role prompt and the credit are in the instructions file, every turn.
    assert seen["prompt"] == "hello TOKENS 1000"
    (instructions,) = seen["instructions"]
    assert roles.load("worker", None).prompt.splitlines()[0] in instructions
    assert "[OpenCode](https://opencode.ai)" in instructions
    assert (seen["mode"], seen["dir_mode"]) == ("0o600", "0o700")
    assert not Path(seen["config_path"]).exists()  # removed with the turn
    assert seen["config_content"]["permissions"][-1]["resource"].endswith("/*")  # the .git
    # The worker leaves out the user's opencode config: moved away for opencode, given back
    # to the agent's commands by the shell wrapper.
    assert seen["config_home"] != user_home
    assert seen["command_config_home"] == user_home
    assert seen["global_agents"] == opencode.GLOBAL_AGENTS_TEXT  # in place of CLAUDE.md
    assert seen["config_home"] == str(config.agent_files_dir(agent.id) / opencode.CONFIG_HOME)
    assert run.result.startswith("done: hello TOKENS 1000")
    assert run.structured is None  # no answer schema was asked for
    usage = orch.store.usage_of(agent.id)[first.id]
    tokens = (usage.input_tokens, usage.output_tokens, usage.cache_read_tokens)
    assert tokens == (1000, 100, 2000) and usage.cache_creation_tokens == 500
    assert run.cost_usd == pytest.approx(0.01) and usage.cumulative is False
    assert usage.context_tokens == 3500

    second = orch.send(agent.id, "FAIL")
    finished(orch, agent.id)

    run = orch.store.get_run(second.id)
    assert run.status is RunStatus.FAILED and run.error == "boom (unknown)"
    (seen,) = opencode_runs(config.run_dir(second.id))
    assert option(seen["argv"], "--session") == session and "--archived" not in seen["argv"]
    # The resumed turn finds the role prompt and the config home where the first one did.
    assert seen["config_content"]["instructions"] == first_seen["config_content"]["instructions"]
    assert seen["config_home"] == first_seen["config_home"]
    assert seen["global_agents"] == opencode.GLOBAL_AGENTS_TEXT


def test_a_lead_turn_has_the_orch_server_in_its_configuration(orch, opencode_turns: str) -> None:
    user_home = os.environ["XDG_CONFIG_HOME"]
    agent, run = orch.spawn(
        opencode_turns, "plan CODEMODE", provider="opencode", role="lead", worktree=False
    )
    finished(orch, agent.id)

    assert orch.store.get_run(run.id).status is RunStatus.DONE
    (seen,) = opencode_runs(config.run_dir(run.id))
    servers = seen["servers"]
    assert list(servers) == ["orch"]
    assert servers["orch"]["type"] == "local"
    assert servers["orch"]["command"][1:] == ["-m", "orchestrator.mcp_server"]
    assert servers["orch"]["environment"]["ORCH_AGENT_ID"] == agent.id
    # The server gets the user's own XDG_CONFIG_HOME, not the one the turn moved away.
    assert seen["server_config_home"] == {"orch": user_home} != {"orch": seen["config_home"]}
    assert seen["command_config_home"] == user_home
    statements = [(p["action"], p["resource"], p["effect"]) for p in seen["config"]["policies"]]
    assert ("mcp.use", "*", "deny") in statements and ("mcp.use", "orch", "allow") in statements
    allow = {"action": "orch_*", "resource": "*", "effect": "allow"}
    assert allow in seen["config_content"]["permissions"]
    assert seen["prompt"] == "plan CODEMODE"
    # The Code Mode call is logged with the tool it reported.
    tools = [e.text for e in orch.store.events(agent.id) if e.kind == "tool"]
    assert [text.split(" {")[0][:34] for text in tools] == ["execute (orch.list_agents) const a"]


def test_a_projects_opencode_json_replaces_neither_the_shell_nor_the_orch_server(
    orch, opencode_turns: str
) -> None:
    """The project's config loads after the OPENCODE_CONFIG file and before the content:
    what Spartito must keep is in the content."""
    user_home = os.environ["XDG_CONFIG_HOME"]
    path = Path(orch.store.get_project(opencode_turns).path)
    (path / "PROJECT.md").write_text("the project's own instructions")
    hostile = {
        "shell": "/bin/sh",
        "instructions": [str(path / "PROJECT.md")],
        "mcp": {
            "servers": {
                "orch": {
                    "type": "local",
                    "command": ["./tool"],
                    "environment": {"ORCH_AGENT_ID": "someone-else", "EXTRA": "1"},
                    "disabled": True,
                }
            }
        },
    }
    (path / "opencode.json").write_text(json.dumps(hostile))
    agent, run = orch.spawn(
        opencode_turns, "plan", provider="opencode", role="lead", worktree=False
    )
    finished(orch, agent.id)

    assert orch.store.get_run(run.id).status is RunStatus.DONE
    (seen,) = opencode_runs(config.run_dir(run.id))
    assert seen["project_config"] == hostile  # it was read
    assert seen["shell"] == str(config.agent_files_dir(agent.id) / opencode.SHELL_WRAPPER)
    assert seen["command_config_home"] == user_home  # the wrapper still restores it
    server = seen["servers"]["orch"]
    assert server["command"][1:] == ["-m", "orchestrator.mcp_server"]
    assert server["environment"]["ORCH_AGENT_ID"] == agent.id
    assert "EXTRA" not in server["environment"] and server["disabled"] is False
    # A project's instructions are added, as opencode does; the role prompt comes last.
    project_text, role_prompt = seen["instructions"]
    assert project_text == "the project's own instructions"
    assert roles.load("lead", None).prompt.splitlines()[0] in role_prompt


def test_a_turn_stopped_by_the_user_ends_stopped_as_for_the_other_providers(
    orch, opencode_turns: str
) -> None:
    """The stop's SIGTERM reaches the runner and the CLI together; the runner decides the
    status from its own stop, not from the stream's signal exit."""
    agent, run = orch.spawn(opencode_turns, "SLEEP 30", provider="opencode", worktree=False)
    deadline = time.monotonic() + 30
    while not opencode_runs(config.run_dir(run.id)):
        assert time.monotonic() < deadline, "the turn did not start"
        time.sleep(0.05)

    assert orch.stop(agent.id) == 1
    finished(orch, agent.id)

    stopped = orch.store.get_run(run.id)
    assert (stopped.status, stopped.error) == (RunStatus.STOPPED, "stopped")
    (seen,) = opencode_runs(config.run_dir(run.id))
    assert seen["config_home"] == str(config.agent_files_dir(agent.id) / opencode.CONFIG_HOME)


# -- the model list and the availability check ---------------------------------------------------


@pytest.fixture
def fake_on_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """PATH is one directory with `opencode`, the fake CLI's opencode dialect; it notes
    OPENCODE_DISABLE_MODELS_FETCH and its arguments in the log it returns."""
    directory, log = tmp_path / "bin", tmp_path / "opencode-calls.log"
    directory.mkdir()
    shim = directory / "opencode"
    shim.write_text(
        "#!/bin/sh\n"
        f'printf "%s %s\\n" "${{OPENCODE_DISABLE_MODELS_FETCH-}}" "$*" >> "{log}"\n'
        f'exec "{sys.executable}" "{fakecli.ENGINE}" opencode "$@"\n'
    )
    shim.chmod(0o755)
    monkeypatch.setenv("PATH", str(directory))
    return log


def test_the_model_list_is_read_without_fetching_the_catalogue(fake_on_path: Path) -> None:
    output = REAL_REQUEST_MODEL_LIST()

    assert opencode.parse_model_list(output) == list(opencode_dialect.MODELS)
    assert fake_on_path.read_text() == "1 models --standalone\n"


def test_the_model_list_is_asked_in_the_same_directory_from_anywhere(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Not where the asking process stands: a project's opencode.json there never counts."""
    asked: list[tuple] = []

    def run_cli(argv, timeout, stdin=None, env=None, cwd=None):
        asked.append((cwd, env["PWD"]))
        return 0, "opencode/a\n", ""

    monkeypatch.setattr(opencode.kit, "run_cli", run_cli)
    standing = tmp_path / "my-app"
    standing.mkdir()
    monkeypatch.chdir(standing)
    monkeypatch.setenv("PWD", str(standing))

    assert REAL_REQUEST_MODEL_LIST() == "opencode/a\n"
    where = opencode.model_list_dir()
    assert asked == [(where, where)] and Path(where).is_dir()
    assert not Path(where).resolve().is_relative_to(standing.resolve())


def test_the_model_list_is_asked_where_only_the_user_can_write() -> None:
    """Not the shared temporary directory: anyone could put an opencode.json there, which
    opencode would load as the project's."""
    where = Path(opencode.model_list_dir())

    assert where.parent == config.private_dir()
    assert where.stat().st_mode & 0o777 == 0o700 == where.parent.stat().st_mode & 0o777
    assert where.stat().st_uid == os.getuid() == where.parent.stat().st_uid
    assert where.is_relative_to(config.home())  # ~/.orchestrator, not the shared /tmp
    where.chmod(0o755)  # widened by someone: made private again
    assert Path(opencode.model_list_dir()).stat().st_mode & 0o777 == 0o700


def test_the_model_list_keeps_provider_model_lines_once() -> None:
    output = "opencode/a\nnot a model\n\nopenrouter/openai/gpt-4.1-mini\nopencode/a\n  x/y z\n"

    assert opencode.parse_model_list(output) == ["opencode/a", "openrouter/openai/gpt-4.1-mini"]


def test_a_failing_model_list_is_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    def failing() -> str:
        raise subprocess.SubprocessError("exit 1")

    monkeypatch.setattr(opencode, "request_model_list", failing)

    assert OpencodeProvider().models() == []


@pytest.mark.parametrize(
    ("version", "auth", "status", "reason"),
    [
        ("2.3.2", None, "available", "found on PATH; logged in"),
        ("2.2.3", "[]", "unknown", "found on PATH; no login listed by `opencode auth list`;"
         " a provider keyed in a config file is not seen there, and the free models need none"),
        ("2.3.2", "not json", "available", "found on PATH; login not checked"),
        ("1.19.0", None, "not_installed", "found on PATH; version 1.19.0 is older than 2.2.3,"
         " the oldest supported"),
    ],
)  # fmt: skip
def test_the_check_reads_the_version_and_the_login(
    fake_on_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    version: str,
    auth: str | None,
    status: str,
    reason: str,
) -> None:
    monkeypatch.setenv("FAKE_OPENCODE_VERSION", version)
    if auth is not None:
        monkeypatch.setenv("FAKE_OPENCODE_AUTH", auth)

    found = REAL_PROBE(OpencodeProvider())

    assert (found.status, found.version, found.reason) == (status, version, reason)
    # An empty login list is no proof: the CLI stays usable.
    assert (found.status in availability.USABLE) == (status != "not_installed")
    # The version of `opencode v2.3.2`, its `v` dropped; the minimum applies after the login.
    asked = fake_on_path.read_text().splitlines()
    assert asked == [" --version", " auth list --standalone --format json"]


# -- opencode wherever Claude and Codex are ------------------------------------------------------


def test_attach_runs_the_cli_with_pwd_set_to_the_agents_directory(
    orch, opencode_turns: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """opencode takes its directory from $PWD before the cwd."""
    agent, run = orch.spawn(opencode_turns, "hello", provider="opencode")
    finished(orch, agent.id)
    started: list[tuple] = []
    monkeypatch.setattr(cli.shutil, "which", lambda name, path=None: f"/bin/{name}")
    monkeypatch.setattr(cli.os, "chdir", lambda path: started.append(("chdir", path)))
    monkeypatch.setattr(cli.os, "execvpe", lambda file, argv, env: started.append((argv, env)))
    monkeypatch.setenv("PWD", "/somewhere/else")

    result = CliRunner().invoke(cli.app, ["attach", agent.id])

    assert result.exit_code == 0, result.output
    cwd = orch.store.get_agent(agent.id).cwd
    (chdir, (argv, env)) = started
    assert chdir == ("chdir", cwd) and env["PWD"] == cwd
    assert argv == ["opencode", "--session", orch.store.get_agent(agent.id).session_id]


def test_the_label_list_names_every_builtin() -> None:
    assert providers.builtin_labels() == "Claude Code, Codex and OpenCode"
    assert "Claude Code, Codex and OpenCode" in mcp_server.server.instructions


def test_the_listing_describes_opencode(orch, monkeypatch: pytest.MonkeyPatch) -> None:
    client = TestClient(cli_app_web(), base_url="http://localhost")

    (entry,) = [e for e in orch.provider_list() if e["name"] == "opencode"]
    listed = client.get("/api/providers").json()["providers"]

    assert [e["name"] for e in listed][:3] == ["claude", "codex", "opencode"]
    assert entry["label"] == "OpenCode" and entry["executable"] == "opencode"
    assert entry["efforts"] == ["low", "medium", "high", "max"]
    assert [m["value"] for m in entry["models"]] == OPENCODE_MODELS.split()
    assert entry["features"] == {
        "cost": True,
        "compaction": False,
        "compaction_instructions": False,
        "context_size": True,
        "role_prompt": "every_turn",
        "attribution": True,
    }
    assert entry["limits"] == list(OpencodeProvider.limits)
    assert entry["capabilities"] == [] and entry["usable"] and not entry["default"]
    assert setup.cache_effect("opencode") == {"model": "restarts", "effort": "may restart"}


def cli_app_web():
    from orchestrator.web.app import app

    return app


def plain(result) -> str:
    """The output as one line, without styles and the borders of help panels."""
    return " ".join(click.unstyle(result.output).replace("│", " ").split())


def test_the_command_line_lists_and_takes_opencode(orch, repo: Path) -> None:
    listed = CliRunner().invoke(cli.app, ["providers"])
    spawn_help = CliRunner().invoke(cli.app, ["spawn", "--help"])
    app_help = CliRunner().invoke(cli.app, ["--help"])
    added = CliRunner().invoke(cli.app, ["project", "add", str(repo), "--provider", "opencode"])

    assert listed.exit_code == 0 and re.search(r"^\s+opencode\s+available", listed.output, re.M)
    assert "One of: claude, codex, opencode" in plain(spawn_help)
    assert "default or auto" in plain(spawn_help)
    assert "Claude Code, Codex and OpenCode agents" in plain(app_help)
    assert added.exit_code == 0, added.output
    assert orch.store.get_project("repo").default_provider == "opencode"


def test_a_project_and_an_agent_choose_and_move_to_opencode(
    orch, project: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    launched: list[int] = []
    monkeypatch.setattr(type(orch), "_launch", lambda self, run: launched.append(run.id))

    agent, _ = orch.spawn(project, "task", provider="opencode", worktree=False)
    other, _ = orch.spawn(project, "task", worktree=False)
    orch.set_default_provider(project, "opencode")
    later, _ = orch.spawn(project, "task", worktree=False)

    assert agent.provider == "opencode" and other.provider == "fake"
    assert later.provider == "opencode" and len(launched) == 3
    assert orch.agent(agent.id).provider == "opencode"
    reply = mcp_server.spawn_agent("task", project=project, provider="opencode")
    assert reply["provider"] == "opencode"
    # An idle agent without a conversation moves without a question.
    orch.store.finish_run(launched[1], RunStatus.DONE, result="ok")
    orch.set_provider(other.id, "opencode")
    assert orch.agent(other.id).provider == "opencode"


def test_an_opencode_agent_cannot_compact(orch, project: str, monkeypatch) -> None:
    monkeypatch.setattr(type(orch), "_launch", lambda self, run: None)
    agent, run = orch.spawn(project, "task", provider="opencode", worktree=False)
    orch.store.finish_run(run.id, RunStatus.DONE, result="ok")
    orch.store.set_session(agent.id, "ses_0123456789abcdef0123456789")

    with pytest.raises(OrchestratorError, match="the opencode provider has no way to compact"):
        orch.compact(agent.id)


def test_an_opencode_agent_gets_no_automatic_compaction_nor_its_threshold(
    orch, opencode_turns: str
) -> None:
    orch.update_settings({**orch.settings(None), settings.COMPACT_AT: "1k"})
    agent, run = orch.spawn(opencode_turns, "hello TOKENS 5000", provider="opencode")
    finished(orch, agent.id)

    assert [r.id for r in orch.store.runs_of(agent.id)] == [run.id]  # nothing queued
    context = orch.view(orch.agent(agent.id), detail=True)["context"]
    assert context["tokens"] == 17500  # far above the threshold
    assert "compact_at" not in context and "compact_paused" not in context
    shown = " ".join(CliRunner().invoke(cli.app, ["status", agent.id]).output.split())
    assert "no compact: the opencode provider has no way to compact" in shown
    assert "automatic compaction" not in shown


def test_its_agents_credit_opencode() -> None:
    credit = roles.attribution(OpencodeProvider.credit, bot_coauthor=False)

    assert "[OpenCode](https://opencode.ai)" in credit
    assert "when you add a Co-Authored-By trailer" in credit
    assert availability.describe(OpencodeProvider())["features"]["attribution"] is True
