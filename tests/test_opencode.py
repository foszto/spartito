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
from fakeprovider import FakeOpencode, opencode_runs

from orchestrator import config, providers, roles
from orchestrator.loadout import Loadout
from orchestrator.models import Agent, RunStatus
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


def test_a_lead_turn_has_the_orch_server_in_its_configuration(orch, opencode_turns: str) -> None:
    user_home = os.environ["XDG_CONFIG_HOME"]
    agent, run = orch.spawn(
        opencode_turns, "plan", provider="opencode", role="lead", worktree=False
    )
    finished(orch, agent.id)

    assert orch.store.get_run(run.id).status is RunStatus.DONE
    (seen,) = opencode_runs(config.run_dir(run.id))
    servers = seen["config"]["mcp"]["servers"]
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
    assert seen["prompt"] == "plan"
