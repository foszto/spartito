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
from dataclasses import replace
from pathlib import Path

import pytest

from orchestrator.loadout import Loadout
from orchestrator.models import Agent
from orchestrator.providers import McpServer, RunContext, opencode
from orchestrator.providers.base import Wiring
from orchestrator.providers.opencode import OpencodeProvider

STRICT = Loadout(strict_mcp=True, setting_sources=("project", "local"))  # as the fallback
OWN = McpServer(command="python", args=["-m", "orchestrator.mcp_server"], env={"A": "1"})
DOCS_DB = {"command": "docs-db", "args": ["--db", "${DOCS_DB:-main}"], "env": {"URL": "${DB_URL}"}}
REMOTE = {"type": "http", "url": "https://docs.example.com/mcp", "headers": {"X": "${TOKEN}"}}
SHOW_HOME = 'printf "%s" "${XDG_CONFIG_HOME-(unset)}"'


def context(tmp_path: Path, loadout: Loadout | None = None, **agent_fields) -> RunContext:
    agent = Agent(
        id="fix-login-3f2a",
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


def turn_config(invocation) -> dict:
    (content,) = invocation.private_files.values()
    return json.loads(content)


def permissions(invocation) -> list[dict]:
    return json.loads(invocation.env["OPENCODE_CONFIG_CONTENT"])["permissions"]


def denied(invocation) -> list[str]:
    return [r["action"] for r in permissions(invocation) if r["effect"] == "deny"]


def policies(invocation) -> list[tuple[str, str, str]]:
    return [(p["action"], p["resource"], p["effect"]) for p in turn_config(invocation)["policies"]]


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
    assert turn_config(invocation)["instructions"] == [str(tmp_path / opencode.INSTRUCTIONS)]


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


def test_the_turn_file_holds_instructions_policies_and_no_updates(tmp_path: Path) -> None:
    invocation = OpencodeProvider().invocation(context(tmp_path))

    config = turn_config(invocation)
    assert set(config) == {"update", "instructions", "policies"}
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
    # Without: moved away to a home of the turn; no plugins.
    assert no_user.env["XDG_CONFIG_HOME"] == str(tmp_path / opencode.CONFIG_HOME)
    assert Path(no_user.env["XDG_CONFIG_HOME"]).is_dir()
    assert turn_config(no_user)["shell"] == str(tmp_path / opencode.SHELL_WRAPPER)
    assert ("plugin.use", "*", "deny") in policies(no_user)


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
    assert servers == {
        "orch": {
            "type": "local",
            "command": ["python", "-m", "orchestrator.mcp_server"],
            "environment": {"A": "1", **home},
        },
        "docs-db": {
            "type": "local",
            "command": ["docs-db", "--db", "main"],  # unset: the default, as written
            "environment": {"URL": "{env:DB_URL}", **home},
        },
        "remote-docs": {
            "type": "remote",
            "url": "https://docs.example.com/mcp",
            "headers": {"X": "{env:TOKEN}"},
        },
    }
    assert policies(invocation)[-5:-1] == [
        ("mcp.use", "*", "deny"),
        ("mcp.use", "orch", "allow"),
        ("mcp.use", "docs-db", "allow"),
        ("mcp.use", "remote-docs", "allow"),
    ]
    assert {"action": "orch_*", "resource": "*", "effect": "allow"} in permissions(invocation)
    # The file holds references only, never a value.
    monkeypatch.setenv("DOCS_DB", "other")
    again = turn_config(OpencodeProvider().invocation(ctx))["mcp"]["servers"]["docs-db"]
    assert again["command"] == ["docs-db", "--db", "{env:DOCS_DB}"]


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
