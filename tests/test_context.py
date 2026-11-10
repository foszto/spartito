"""How full an agent's conversation is, and compacting it (runner, service, CLI, API)."""

from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import replace
from pathlib import Path

import pytest
from starlette.testclient import TestClient
from typer.testing import CliRunner

from orchestrator import cli, config, mcp_server, notify
from orchestrator.loadout import Loadout
from orchestrator.models import Agent, Event, Run, RunStatus, Usage, context_now
from orchestrator.providers.base import NOTIFICATION_RESULT, RunContext
from orchestrator.providers.claude import ClaudeProvider
from orchestrator.providers.codex import COMPACT_PROMPT, CodexProvider
from orchestrator.runner import (
    compact_limit,
    compact_summary,
    measured_context,
    record_usage,
    render_prompt,
)
from orchestrator.service import Orchestrator, OrchestratorError, agent_state
from orchestrator.store import COMPACT, Store
from orchestrator.web.app import app

ORIGIN = {"origin": "http://localhost"}


def finished(orch: Orchestrator, agent_id: str) -> dict:
    done, (view,) = orch.wait([agent_id], timeout=30)
    assert done, "agent did not finish in time"
    return view


def seen_by_provider(run_id: int) -> dict:
    """What the runner passed the fake provider for the run (FakeProvider.invocation)."""
    return json.loads((config.run_dir(run_id) / "ctx.json").read_text())


def test_the_context_after_a_turn_is_stored_and_shown(orch: Orchestrator, project: str) -> None:
    agent, run = orch.spawn(project, "CONTEXT 780 TOKENS 10", worktree=False)

    view = finished(orch, agent.id)

    assert view["context"] == {"tokens": 780, "window": 1000, "percent": 78, "compacted": False}
    usage = orch.store.get_usage(run.id)
    assert (usage.context_tokens, usage.context_window, usage.compact_trigger) == (780, 1000, None)
    assert usage.input_tokens == 10  # the run's own usage is unchanged


def test_an_agent_without_context_figures_shows_none(orch: Orchestrator, project: str) -> None:
    agent, _ = orch.spawn(project, "TOKENS 10", worktree=False)
    assert finished(orch, agent.id)["context"] is None
    never = orch.create_agent(project, worktree=False)
    assert orch.view(never)["context"] is None


def test_a_compaction_queues_the_clis_own_command_verbatim_and_stores_a_summary(
    orch: Orchestrator, project: str
) -> None:
    agent, _ = orch.spawn(project, "CONTEXT 780", worktree=False, role_schema=True)
    finished(orch, agent.id)
    instructions = "keep {{result:" + agent.id + "}} and  the\nplan"

    run = orch.compact(agent.id, instructions)
    view = finished(orch, agent.id)

    assert orch.store.meta(run.id).kind == COMPACT
    # Not rendered: a {{result:…}} reference stays as it is; whitespace is not touched here.
    seen = seen_by_provider(run.id)
    assert seen["prompt"] == f"/compact {instructions}"
    assert seen["slash_commands"] is True and seen["output_schema"] is None
    done = orch.store.get_run(run.id)
    assert (done.status, done.result) == (RunStatus.DONE, "compacted 48k -> 901 tokens")
    assert view["context"] == {"tokens": None, "window": None, "percent": None, "compacted": True}
    usage = orch.store.get_usage(run.id)
    assert (usage.compact_trigger, usage.compact_pre_tokens, usage.compact_post_tokens) == (
        "manual",
        48450,
        901,
    )
    infos = [e.text for e in orch.store.events(agent.id) if e.kind == "info"]
    assert infos == ["compacted (manual)"]
    detail = orch.view(orch.agent(agent.id), detail=True)
    assert detail["last_run"]["kind"] == "message" and detail["last_run"]["id"] != run.id
    assert detail["last_compaction"]["id"] == run.id
    assert detail["last_compaction"]["result"] == "compacted 48k -> 901 tokens"
    assert orch.agent(agent.id).session_id == "fake-session"  # the same session goes on

    orch.send(agent.id, "CONTEXT 300")
    assert finished(orch, agent.id)["context"]["tokens"] == 300


def test_a_compaction_the_cli_does_not_perform_fails(orch: Orchestrator, project: str) -> None:
    agent, _ = orch.spawn(project, "CONTEXT 780", worktree=False)
    finished(orch, agent.id)

    run = orch.compact(agent.id, "NOOP")
    finished(orch, agent.id)

    failed = orch.store.get_run(run.id)
    assert failed.status is RunStatus.FAILED
    assert failed.error == (
        "nothing was compacted (the CLI answered: /compact isn't available in this environment.)"
    )
    assert orch.view(orch.agent(agent.id))["context"]["tokens"] == 780  # nothing changed


def test_a_failed_compaction_leaves_the_agents_status_and_answer_alone(
    orch: Orchestrator, project: str
) -> None:
    agent, work = orch.spawn(project, "CONTEXT 780", worktree=False)
    finished(orch, agent.id)

    run = orch.compact(agent.id, "NOOP")
    view = finished(orch, agent.id)
    (listed,) = orch.overview(project)
    detail = orch.view(orch.agent(agent.id), detail=True)
    shown = CliRunner().invoke(cli.app, ["status", agent.id]).output

    assert orch.store.get_run(run.id).status is RunStatus.FAILED
    for one in (view, listed, detail):
        assert one["status"] == "done" and one["last_run"]["id"] == work.id
        assert one["last_run"]["result"] == "echo[new]: CONTEXT 780"
    assert detail["last_compaction"]["status"] == "failed"
    assert f"compaction   #{run.id} failed: " in shown
    assert "done" in CliRunner().invoke(cli.app, ["ps"]).output.split(agent.id)[1].split()[0]


def test_a_running_compaction_keeps_the_agent_busy(orch: Orchestrator, project: str) -> None:
    agent, _ = orch.spawn(project, "CONTEXT 10", worktree=False)
    finished(orch, agent.id)
    first = orch.store.runs_of(agent.id)[0]
    compaction = orch.store.add_run(agent.id, "/compact", kind=COMPACT)

    status, active, shown = agent_state(orch.store.runs_of(agent.id), {compaction.id})
    assert (status, [r.id for r in active], shown.id) == ("waiting", [compaction.id], first.id)
    orch.store.finish_run(compaction.id, RunStatus.FAILED, error="x")
    status, active, shown = agent_state(orch.store.runs_of(agent.id), {compaction.id})
    assert (status, active, shown.id) == ("done", [], first.id)
    assert agent_state(orch.store.runs_of(agent.id), {compaction.id, first.id})[0] == "new"


def test_a_result_reference_skips_compactions(orch: Orchestrator, project: str) -> None:
    analyst, _ = orch.spawn(project, "analysis", worktree=False)
    finished(orch, analyst.id)
    orch.compact(analyst.id)
    finished(orch, analyst.id)

    builder, run = orch.spawn(
        project, "implement {{result:" + analyst.id + "}}", worktree=False, after=[analyst.id]
    )
    finished(orch, builder.id)

    assert orch.store.get_run(run.id).result == "echo[new]: implement echo[new]: analysis"
    assert render_prompt(orch.store, "{{result:" + analyst.id + "}}") == "echo[new]: analysis"


def test_wait_for_agents_never_delivers_a_compaction(
    orch: Orchestrator, project: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    lead = orch.create_agent(project, name="boss", role="lead", worktree=False)
    child, first = orch.spawn(project, "CONTEXT 10", worktree=False, parent_id=lead.id)
    finished(orch, child.id)
    finished(orch, lead.id)  # the notification of the first answer
    turn = orch.send(lead.id, "SLEEP 4")  # the lead's turn, in which it waits
    while orch.store.get_run(turn.id).status is not RunStatus.RUNNING:
        time.sleep(0.05)
    monkeypatch.setenv("ORCH_AGENT_ID", lead.id)
    second = orch.send(child.id, "SLEEP 1 second", sender=lead.id)
    compaction = orch.compact(child.id)  # by the user, while the child works

    reply = mcp_server._wait(orch, [child.id], timeout=30, limit=4000)

    (outcome,) = reply["agents"]
    assert reply["finished"] and outcome["status"] == "done"
    assert outcome["last_run"]["id"] == second.id
    assert outcome["last_run"]["result"] == "echo[resumed]: SLEEP 1 second"
    assert "earlier_runs" not in outcome and "other_runs" not in outcome
    assert orch.store.get_run(compaction.id).status is RunStatus.DONE
    assert orch.store.claim(lead.id, [compaction.id]) == {compaction.id}  # never claimed
    assert first.id not in [r["id"] for r in outcome.get("earlier_runs", [])]
    again = mcp_server._wait(orch, [child.id], timeout=5, limit=4000)
    assert again["agents"][0]["last_run"]["already_delivered"]
    finished(orch, lead.id)


def test_a_leads_compaction_ends_with_its_empty_result(orch: Orchestrator, project: str) -> None:
    lead = orch.lead(project)
    orch.send(lead.id, "CONTEXT 500")
    finished(orch, lead.id)

    run = orch.compact(lead.id)
    view = finished(orch, lead.id)

    done = orch.store.get_run(run.id)
    assert (done.status, done.result) == (RunStatus.DONE, "compacted 48k -> 901 tokens")
    assert seen_by_provider(run.id)["prompt"] == "/compact"
    assert (Path(lead.cwd) / ".fake-launches").read_text().split() == ["memory", "memory"]
    assert view["context"]["compacted"]


def test_a_compaction_reaches_no_parent(orch: Orchestrator, project: str) -> None:
    lead = orch.create_agent(project, name="boss", role="lead", worktree=False)
    child, _ = orch.spawn(project, "CONTEXT 10", worktree=False, parent_id=lead.id)
    child_view = finished(orch, child.id)
    assert child_view["status"] == "done"
    finished(orch, lead.id)  # the notification of the child's answer
    lead_runs = len(orch.store.runs_of(lead.id))

    run = orch.compact(child.id)
    finished(orch, child.id)
    finished(orch, lead.id)

    assert orch.store.get_run(run.id).status is RunStatus.DONE
    assert len(orch.store.runs_of(lead.id)) == lead_runs  # nobody was woken
    with orch.store.connection() as conn:
        (kid,) = notify.children(conn, lead.id)
    assert run.id not in kid.deliverable


def test_a_compaction_keeps_its_place_in_the_queue(orch: Orchestrator, project: str) -> None:
    agent, _ = orch.spawn(project, "CONTEXT 10", worktree=False)
    finished(orch, agent.id)
    first = orch.send(agent.id, "SLEEP 1 CONTEXT 20")
    compaction = orch.compact(agent.id)
    after = orch.send(agent.id, "after")
    finished(orch, agent.id)

    runs = {run.id: run for run in orch.store.runs_of(agent.id)[1:]}
    started = [e.run_id for e in orch.store.events(agent.id) if e.kind == "prompt"]
    assert started[1:] == [first.id, compaction.id, after.id]
    assert all(run.status is RunStatus.DONE for run in runs.values())
    assert runs[after.id].result == "echo[resumed]: after"


def test_compaction_is_refused_without_a_conversation(orch: Orchestrator, project: str) -> None:
    agent = orch.create_agent(project, worktree=False)

    with pytest.raises(OrchestratorError, match="has no conversation yet"):
        orch.compact(agent.id)
    assert orch.store.runs_of(agent.id) == []
    assert orch.view(agent, detail=True)["compact_refusal"] == "it has no conversation yet"


def test_codex_compaction_needs_a_known_size_and_takes_no_instructions(
    orch: Orchestrator, project: str
) -> None:
    agent = orch.create_agent(project, worktree=False, provider="codex")
    orch.store.set_session(agent.id, "thread-1")

    with pytest.raises(OrchestratorError, match="context size is not known yet"):
        orch.compact(agent.id)
    run = orch.store.add_run(agent.id, "x")
    orch.store.set_usage(run.id, agent.id, Usage(context_tokens=40_000, session_id="thread-1"))
    orch.store.finish_run(run.id, RunStatus.DONE)
    with pytest.raises(OrchestratorError, match="instructions are not supported"):
        orch.compact(agent.id, "keep the plan")
    assert orch.compact_refusal(orch.agent(agent.id)) is None


# -- what the indicator shows --------------------------------------------------


def runs_with(usages: list[Usage | None]) -> tuple[list[Run], dict[int, Usage]]:
    runs = [Run(id=n, agent_id="a", prompt="x") for n in range(1, len(usages) + 1)]
    return runs, {n: u for n, u in enumerate(usages, start=1) if u is not None}


def test_the_indicator_is_the_latest_run_of_the_current_session_with_figures() -> None:
    old_session = Usage(context_tokens=900, context_window=1000, session_id="old")
    sized = Usage(context_tokens=500, context_window=1000, session_id="s")
    without = Usage(input_tokens=5, session_id="s")  # e.g. a run by older code
    compacted = Usage(compact_trigger="manual", compact_pre_tokens=500, session_id="s")

    assert context_now(*runs_with([sized, without, None]), "s") is not None
    assert context_now(*runs_with([sized, without, None]), "s").context_tokens == 500
    assert context_now(*runs_with([sized, old_session]), "s").context_tokens == 500
    assert context_now(*runs_with([old_session]), "s") is None
    assert context_now(*runs_with([sized]), None) is None
    after = context_now(*runs_with([sized, compacted]), "s")
    assert after.context_tokens is None and after.compact_trigger == "manual"
    auto = Usage(context_tokens=300, compact_trigger="auto", session_id="s")  # in mid-turn
    assert context_now(*runs_with([sized, compacted, auto]), "s").context_tokens == 300


def test_context_figures_survive_session_totals(tmp_path: Path) -> None:
    store = Store(tmp_path / "orch.db")
    store.add_agent(Agent(id="a", project=None, name="a", role="w", provider="codex", cwd="/"))
    run = store.add_run("a", "x")
    total = Usage(100, 10, 0, 0, cumulative=True)
    measured = Usage(context_tokens=14_084, context_window=258_400)

    record_usage(
        store, run, store.get_agent("a"), Event("result", usage=total), None, "t", measured
    )

    usage = store.get_usage(run.id)
    assert (usage.input_tokens, usage.context_tokens, usage.context_window) == (100, 14084, 258400)
    # Measured figures are stored even when the turn ended without a result.
    second = store.add_run("a", "y")
    record_usage(store, second, store.get_agent("a"), None, None, "t", measured)
    assert store.get_usage(second.id).context_tokens == 14_084
    assert record_usage(store, second, store.get_agent("a"), None, None, "t", Usage()) is None


def test_an_existing_run_usage_table_gets_the_context_columns(tmp_path: Path) -> None:
    path = tmp_path / "orch.db"
    Store(path)
    with sqlite3.connect(path) as conn:  # a run_usage table as the previous version made it
        conn.execute("DROP TABLE run_usage")
        conn.execute(
            "CREATE TABLE run_usage (run_id INTEGER PRIMARY KEY, agent_id TEXT NOT NULL,"
            " input_tokens INTEGER, output_tokens INTEGER, cache_read_tokens INTEGER,"
            " cache_creation_tokens INTEGER, cost_usd REAL, model TEXT,"
            " created_at TEXT NOT NULL, session_id TEXT)"
        )
        conn.execute(
            "INSERT INTO run_usage (run_id, agent_id, input_tokens, created_at, session_id)"
            " VALUES (1, 'a', 7, 't', 's')"
        )
    conn.close()

    store = Store(path)
    Store(path)  # a second opener finds them there

    old = store.get_usage(1)
    assert (old.input_tokens, old.session_id, old.context_tokens) == (7, "s", None)
    store.set_usage(2, "a", Usage(context_tokens=5, context_window=10, compact_trigger="auto"))
    new = store.get_usage(2)
    assert (new.context_tokens, new.context_window, new.compact_trigger) == (5, 10, "auto")


# -- CLI and API -------------------------------------------------------------------


def test_the_cli_shows_the_context_and_compacts(orch: Orchestrator, project: str) -> None:
    agent, _ = orch.spawn(project, "CONTEXT 850", worktree=False)
    finished(orch, agent.id)
    runner = CliRunner()

    listed = runner.invoke(cli.app, ["ps"])
    shown = runner.invoke(cli.app, ["status", agent.id])

    assert "850/1k 85%" in listed.output
    assert "850/1k 85%" in shown.output and f"orch compact {agent.id}" in shown.output

    compacted = runner.invoke(cli.app, ["compact", agent.id, "keep the plan"])
    assert compacted.exit_code == 0 and compacted.output.startswith("run ")
    finished(orch, agent.id)
    after = runner.invoke(cli.app, ["status", agent.id])
    assert "compacted; its size is known after the next turn" in after.output
    assert "compaction   #2 done: compacted 48k -> 901 tokens" in after.output
    assert "last run 1: done" in after.output  # its last real turn
    assert "compacted" in runner.invoke(cli.app, ["ps"]).output

    never = orch.create_agent(project, worktree=False)
    refused = runner.invoke(cli.app, ["compact", never.id])
    assert refused.exit_code == 1 and "no conversation yet" in refused.output


def test_cli_sizes_read_at_a_glance() -> None:
    assert [cli.short(n) for n in (999, 48_450, 780_000, 1_000_000, 1_500_000)] == [
        "999",
        "48k",
        "780k",
        "1M",
        "1.5M",
    ]
    big = {"tokens": 780_000, "window": 1_000_000, "percent": 78, "compacted": False}
    assert cli.context_text(big) == "780k/1M 78%"
    assert cli.context_text(None) == ""


def test_the_api_reports_the_context_and_queues_a_compaction(
    orch: Orchestrator, project: str
) -> None:
    client = TestClient(app, base_url="http://localhost")
    agent, _ = orch.spawn(project, "CONTEXT 400", worktree=False)
    finished(orch, agent.id)

    state = client.get("/api/state").json()
    (listed,) = state["projects"][0]["agents"]
    detail = client.get(f"/api/agents/{agent.id}/events").json()["agent"]

    assert listed["context"]["percent"] == 40
    assert detail["context"]["tokens"] == 400 and detail["compact_refusal"] is None
    path = f"/api/agents/{agent.id}/compact"
    assert client.post(path, json={}).status_code == 403  # guarded like every write
    queued = client.post(path, json={"instructions": " keep it "}, headers=ORIGIN).json()
    assert queued["agent_id"] == agent.id
    finished(orch, agent.id)
    assert seen_by_provider(queued["run_id"])["prompt"] == "/compact keep it"
    history = client.get(f"/api/agents/{agent.id}/events").json()["agent"]["recent_runs"]
    assert [run["kind"] for run in history] == ["message", COMPACT]

    never = orch.create_agent(project, worktree=False)
    refused = client.post(f"/api/agents/{never.id}/compact", json={}, headers=ORIGIN)
    assert refused.status_code == 400 and "no conversation yet" in refused.json()["error"]


# -- what the CLIs report ------------------------------------------------------------

FIXTURES = Path(__file__).parent / "fixtures"
BIG_CACHE = {"cache_read_input_tokens": 500_000, "cache_creation_input_tokens": 0}


def claude_events(*lines: str) -> list[Event]:
    claude = ClaudeProvider()
    return [event for line in lines for event in claude.parse(line)]


def fixture_lines(name: str) -> list[str]:
    return (FIXTURES / name).read_text().splitlines()


def test_claude_context_is_the_last_main_chain_request_of_the_turn() -> None:
    # Recorded (Claude Code 2.1.322): Read of a large file, three requests in one turn.
    events = claude_events(*fixture_lines("claude_context_turn.jsonl"))

    sizes = [e.usage.context_tokens for e in events if e.kind == "context"]
    (result,) = [e for e in events if e.kind == "result"]
    measured = measured_context(
        next(e.usage for e in reversed(events) if e.kind == "context"), None, result.windows
    )

    assert sorted(set(sizes)) == [9078, 31758, 91364]  # each message line repeats its usage
    assert (measured.context_tokens, measured.context_window) == (91364, 1_000_000)
    assert result.usage.cache_read_tokens == 45919  # summed over the turn: no measure


def test_claude_subagent_and_synthetic_messages_say_nothing_about_the_context() -> None:
    last = json.loads(fixture_lines("claude_context_turn.jsonl")[-2])
    assert last["type"] == "assistant"
    subagent = {**last, "parent_tool_use_id": "toolu_1"}
    subagent["message"] = {**last["message"], "usage": {"input_tokens": 5, **BIG_CACHE}}
    zero = {"input_tokens": 0, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}
    synthetic = {**last, "message": {**last["message"], "model": "<synthetic>", "usage": zero}}

    events = claude_events(json.dumps(subagent), json.dumps(synthetic))

    assert [e.kind for e in events if e.kind == "context"] == []
    assert [e.text for e in events if e.kind == "text"][0].startswith("[subagent] ")


def test_claude_compaction_is_reported_and_its_empty_result_answers_the_prompt() -> None:
    # Recorded: `/compact Keep the code word…` on a 91k conversation.
    events = claude_events(*fixture_lines("claude_compact.jsonl"))

    (compact,) = [e for e in events if e.kind == "compact"]
    (result,) = [e for e in events if e.kind in ("result", NOTIFICATION_RESULT)]
    assert compact.text == "context compacted (manual): 91458 -> 1341 tokens"
    assert [e.kind for e in events if e.kind == "context"] == []
    assert (result.kind, result.text, result.is_error) == ("result", "", False)
    measured = measured_context(None, compact.usage, result.windows)
    assert measured.context_tokens is None
    assert compact_summary(measured) == "compacted 91k -> 1k tokens"
    assert result.usage.cost_usd == pytest.approx(0.00725681)  # the compaction request


def test_claude_compaction_runs_with_slash_commands_and_without_a_schema(tmp_path: Path) -> None:
    agent = Agent(id="w", project="p", name="w", role="worker", provider="claude", cwd="/")
    agent.session_id = "s-1"
    plain = Loadout(tools=("Read",), slash_commands=False)
    ctx = RunContext(agent=agent, prompt="/compact keep it", system_prompt="", scratch=tmp_path)

    normal = ClaudeProvider().invocation(replace(ctx, loadout=plain, output_schema={"a": 1}))
    compact = ClaudeProvider().invocation(replace(ctx, loadout=replace(plain, slash_commands=True)))
    lead = ClaudeProvider().invocation(
        replace(ctx, lead=True, loadout=Loadout(tools=("Read",), slash_commands=True, memory=True))
    )

    assert "--disable-slash-commands" in normal.argv and "--json-schema" in normal.argv
    assert "--disable-slash-commands" not in compact.argv and "--json-schema" not in compact.argv
    assert compact.stdin == "/compact keep it" and "--resume" in compact.argv
    assert json.loads(lead.stdin)["message"]["content"][0]["text"] == "/compact keep it"
    assert ClaudeProvider.compact_prompt("  keep\n the  plan ") == "/compact keep the plan"
    assert ClaudeProvider.compact_prompt(None) == ClaudeProvider.compact_prompt(" ") == "/compact"


def test_codex_context_comes_from_the_rollout_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    day = tmp_path / "sessions" / "2026" / "11" / "10"
    day.mkdir(parents=True)
    rollout = day / "rollout-2026-11-10T19-47-05-thread-1.jsonl"
    lines = fixture_lines("codex_rollout.jsonl")

    assert CodexProvider.context_after("thread-1", "2026-11-10T18:47:05+00:00") is None
    rollout.write_text("\n".join(lines[:5]) + "\n")  # the first turn
    first = CodexProvider.context_after("thread-1", "2026-11-10T18:47:05+00:00")
    assert (first.context_tokens, first.context_window, first.compact_trigger) == (
        15172,
        258400,
        None,
    )
    rollout.write_text("\n".join(lines[:8]) + "\n")  # compacted, no request since
    gone = CodexProvider.context_after("thread-1", "2026-11-10T18:47:11+00:00")
    assert (gone.context_tokens, gone.compact_trigger) == (None, "auto")
    rollout.write_text("\n".join([*lines, "not json", "{}"]) + "\n")  # the compaction turn
    after = CodexProvider.context_after("thread-1", "2026-11-10T18:47:11+00:00")
    assert (after.context_tokens, after.compact_trigger) == (15344, "auto")
    later = CodexProvider.context_after("thread-1", "2026-11-10T18:48:00+00:00")
    assert (later.context_tokens, later.compact_trigger) == (15344, None)
    assert CodexProvider.context_after("other", "2026-11-10T18:47:11+00:00") is None
    assert CodexProvider.context_after("*", "2026-11-10T18:47:11+00:00") is None
    rollout.write_text('{"type": "event_msg", "payload": {"type": "token_count", "info": 3}}\n')
    assert CodexProvider.context_after("thread-1", "2026-11-10T18:47:11+00:00") is None


def test_codex_compaction_lowers_the_auto_compaction_limit(tmp_path: Path) -> None:
    agent = Agent(id="c", project="p", name="c", role="worker", provider="codex", cwd="/")
    agent.session_id = "thread-1"
    ctx = RunContext(agent=agent, prompt=COMPACT_PROMPT, system_prompt="", scratch=tmp_path)

    argv = CodexProvider().invocation(replace(ctx, compact_below=7586)).argv

    assert argv[argv.index("model_auto_compact_token_limit=7586") - 1] == "-c"
    assert compact_limit(Usage(context_tokens=15172)) == 7586
    assert compact_limit(None) is None and compact_limit(Usage()) is None
    assert "model_auto_compact_token_limit" not in " ".join(CodexProvider().invocation(ctx).argv)


def test_a_queued_compaction_can_be_withdrawn_but_not_edited(
    orch: Orchestrator, project: str
) -> None:
    agent, _ = orch.spawn(project, "CONTEXT 10", worktree=False)
    finished(orch, agent.id)
    busy = orch.send(agent.id, "SLEEP 1.5 busy")
    compaction = orch.compact(agent.id)
    later = orch.send(agent.id, "later")

    with pytest.raises(OrchestratorError, match="can be withdrawn, not edited"):
        orch.replace_message(compaction.id, "/compact other")
    orch.withdraw(compaction.id)
    finished(orch, agent.id)

    assert orch.store.get_run(compaction.id) is None
    assert [orch.store.get_run(r.id).status for r in (busy, later)] == [RunStatus.DONE] * 2
    infos = [e.text for e in orch.store.events(agent.id) if e.kind == "info"]
    assert f"queued compaction {compaction.id} withdrawn: /compact" in infos

    # The dashboard's route withdraws it the same way while it is queued.
    client = TestClient(app, base_url="http://localhost")
    orch.send(agent.id, "SLEEP 1.5 busy again")
    again = orch.compact(agent.id)
    withdrawn = client.post(f"/api/runs/{again.id}/withdraw", json={}, headers=ORIGIN)
    assert withdrawn.json() == {"withdrawn": again.id, "agent_id": agent.id}
    finished(orch, agent.id)
    assert orch.store.get_run(again.id) is None


def test_a_compaction_that_needs_the_size_fails_cleanly_without_it(
    orch: Orchestrator, project: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_NEEDS_SIZE", "1")  # like Codex: the limit is half the size
    agent, _ = orch.spawn(project, "CONTEXT 780", worktree=False)
    finished(orch, agent.id)
    orch.send(agent.id, "SLEEP 1 busy")
    first = orch.compact(agent.id)
    second = orch.compact(agent.id)  # still allowed: the size is known when it is queued
    view = finished(orch, agent.id)

    assert seen_by_provider(first.id)["compact_below"] == 390
    failed = orch.store.get_run(second.id)
    assert failed.status is RunStatus.FAILED and failed.error.startswith("not compacted: the ")
    assert not (config.run_dir(second.id) / "ctx.json").exists()  # never launched
    assert view["status"] == "done" and view["context"]["compacted"]


def test_a_no_op_reply_to_a_compaction_reads_as_a_failure(orch: Orchestrator, project: str) -> None:
    agent, _ = orch.spawn(project, "CONTEXT 780", worktree=False)
    finished(orch, agent.id)

    run = orch.compact(agent.id, "NOOP CODEX")
    finished(orch, agent.id)

    failed = orch.store.get_run(run.id)
    assert failed.error == "nothing was compacted (the CLI answered: compacted)"
    shown = CliRunner().invoke(cli.app, ["status", agent.id]).output
    assert f"#{run.id} failed: nothing was compacted" in shown


def test_a_run_without_a_result_keeps_the_known_window(orch: Orchestrator, project: str) -> None:
    agent, _ = orch.spawn(project, "CONTEXT 500", worktree=False)
    finished(orch, agent.id)

    run = orch.send(agent.id, "CONTEXT 960 DIE")  # the CLI dies before its result
    view = finished(orch, agent.id)

    assert orch.store.get_run(run.id).status is RunStatus.FAILED
    usage = orch.store.get_usage(run.id)
    assert (usage.context_tokens, usage.context_window) == (960, 1000)
    assert view["context"] == {"tokens": 960, "window": 1000, "percent": 96, "compacted": False}
    assert "960/1k 96%" in CliRunner().invoke(cli.app, ["ps"]).output
