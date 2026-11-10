"""The MCP tools and the `orch` command line over a store with recorded usage."""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from typing import Any

import click
import pytest
import typer
from typer.testing import CliRunner

from orchestrator import cli, mcp_server
from orchestrator.models import RunStatus, Usage
from orchestrator.service import QUEUE_LISTED, Orchestrator
from orchestrator.store import NOTIFICATION


@pytest.fixture
def busy_agent(orch: Orchestrator, project: str) -> str:
    """A worker with two finished runs (the first one from before usage was recorded)."""
    orch.update_settings({"worker": {"fake": {"model": "fake-small", "effort": "low"}}})
    agent = orch.create_agent(project, name="busy", worktree=False, effort="high")
    old = orch.store.add_run(agent.id, "one")
    orch.store.finish_run(old.id, RunStatus.DONE, result="first")
    new = orch.store.add_run(agent.id, "two")
    orch.store.finish_run(new.id, RunStatus.DONE, result="second", cost_usd=0.0123)
    usage = Usage(1200, 340, 56000, 7800, cost_usd=0.0123, model="fake-model-1")
    orch.store.set_usage(new.id, agent.id, usage)
    return agent.id


def test_mcp_list_agents_is_compact_and_get_agent_lists_recent_runs(
    orch: Orchestrator, busy_agent: str
) -> None:
    (listed,) = mcp_server.list_agents()
    detail = mcp_server.get_agent(busy_agent)

    assert (listed["model"], listed["effort"]) == ("fake-small", "high")
    assert listed["tokens"] == {"in": 1200, "out": 340, "cache_read": 56000, "cache_write": 7800}
    assert listed["cost_usd"] == 0.0123
    for key in ("recent_runs", "model_override", "effort_override"):
        assert key not in listed
    assert "usage" not in listed["last_run"]
    assert (detail["model_override"], detail["effort_override"]) == (None, "high")
    assert detail["last_run"]["usage"]["model"] == "fake-model-1"
    first, second = detail["recent_runs"]
    assert (first["status"], first["cost_usd"], first["tokens"]) == ("done", None, None)
    assert (second["tokens"]["in"], second["model"], second["cost_usd"]) == (
        1200,
        "fake-model-1",
        0.0123,
    )
    assert json.loads(json.dumps(detail))["tokens"]["out"] == 340


def test_usage_keeps_the_default_view_small(orch: Orchestrator, busy_agent: str) -> None:
    run = orch.store.last_run(busy_agent)
    big = Usage(12_345_678, 1_234_567, 98_765_432, 7_654_321, cost_usd=123.4567, model="m")
    orch.store.set_usage(run.id, busy_agent, big)

    view = orch.view(orch.agent(busy_agent), result_limit=400)
    without = {key: value for key, value in view.items() if key != "tokens"}

    # Everything else existed before usage: model/effort were there (as null) and cost_usd.
    assert len(json.dumps(view)) - len(json.dumps(without)) <= 120
    assert set(view["last_run"]) == {
        "id", "status", "result", "error", "started_at", "ended_at"
    }  # fmt: skip


def test_mcp_get_agent_shows_at_most_ten_runs(orch: Orchestrator, busy_agent: str) -> None:
    for n in range(12):
        run = orch.store.add_run(busy_agent, f"more {n}")
        orch.store.finish_run(run.id, RunStatus.DONE)

    runs = mcp_server.get_agent(busy_agent)["recent_runs"]

    assert len(runs) == 10 and runs[-1]["id"] == orch.store.last_run(busy_agent).id


def test_cli_ps_and_status_show_tokens_cost_and_the_effective_setup(
    orch: Orchestrator, busy_agent: str
) -> None:
    runner = CliRunner()

    ps = runner.invoke(cli.app, ["ps"])
    status = runner.invoke(cli.app, ["status", busy_agent])

    assert ps.exit_code == 0, ps.output
    (line,) = [row for row in ps.output.splitlines() if busy_agent in row]
    assert "fake-small/high*" in line and "65.0k/340 tok" in line and "$0.01" in line
    assert status.exit_code == 0, status.output
    assert "fake-small\n" in status.output and "high (set on the agent)" in status.output
    assert "in 1.2k · cache read 56.0k · cache write 7.8k · out 340 · $0.0123" in status.output
    assert "fake-model-1" in status.output


@pytest.fixture
def exec_argv(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    calls: list[list[str]] = []
    monkeypatch.setattr(cli.shutil, "which", lambda name: f"/bin/{name}")
    monkeypatch.setattr(cli.os, "execvp", lambda file, argv: calls.append(argv))
    return calls


def option(argv: list[str], flag: str) -> str | None:
    return argv[argv.index(flag) + 1] if flag in argv else None


def test_chat_runs_with_the_orchestrator_tier_settings(
    orch: Orchestrator, exec_argv: list[list[str]]
) -> None:
    orch.update_settings(
        {
            "orchestrator": {"claude": {"model": "opus", "effort": "high"}},
            "worker": {"claude": {"model": "haiku"}},
        }
    )
    runner = CliRunner()

    plain = runner.invoke(cli.app, ["chat", "--here"])
    chosen = runner.invoke(cli.app, ["chat", "--here", "--model", "sonnet", "--effort", "low"])
    bad = runner.invoke(cli.app, ["chat", "--here", "--effort", "extreme"])

    assert plain.exit_code == 0 and chosen.exit_code == 0
    (tier, explicit) = exec_argv
    assert (option(tier, "--model"), option(tier, "--effort")) == ("opus", "high")
    assert (option(explicit, "--model"), option(explicit, "--effort")) == ("sonnet", "low")
    assert bad.exit_code == 1 and "no effort 'extreme'" in bad.output


def test_chat_without_settings_leaves_model_and_effort_to_claude(
    orch: Orchestrator, exec_argv: list[list[str]]
) -> None:
    result = CliRunner().invoke(cli.app, ["chat", "--here"])

    assert result.exit_code == 0
    (argv,) = exec_argv
    assert "--model" not in argv and "--effort" not in argv


def test_chat_loads_the_orchestrator_loadout_and_keeps_bash_for_orch(
    orch: Orchestrator, exec_argv: list[list[str]]
) -> None:
    runner = CliRunner()
    plain = runner.invoke(cli.app, ["chat", "--here"])
    home = cli.config.home()
    (home / "loadout.toml").write_text(
        '[orchestrator]\ntools = "Read"\nmcp = "inherit"\nslash_commands = false\n'
    )
    overridden = runner.invoke(cli.app, ["chat", "--here"])
    (home / "loadout.toml").write_text("[orchestrator\n")
    broken = runner.invoke(cli.app, ["chat", "--here"])

    assert plain.exit_code == 0 and overridden.exit_code == 0
    (argv, custom) = exec_argv
    assert option(argv, "--tools") == "Bash,Read,Glob,Grep"
    assert option(argv, "--setting-sources") == "project,local"
    assert "--strict-mcp-config" in argv and "Bash(orch *)" in argv and "--mcp-config" in argv
    assert option(custom, "--tools") == "Read,Bash" and "--strict-mcp-config" not in custom
    # The orchestrator role turns slash commands off; the interactive session keeps them.
    assert "--disable-slash-commands" not in argv and "--disable-slash-commands" not in custom
    assert broken.exit_code == 1 and "invalid TOML" in broken.output


def test_a_continued_chat_keeps_the_orchestrator_loadout(
    orch: Orchestrator, exec_argv: list[list[str]]
) -> None:
    result = CliRunner().invoke(cli.app, ["chat", "--here", "--continue"])

    assert result.exit_code == 0
    (argv,) = exec_argv
    assert "--continue" in argv and "--name" not in argv
    assert option(argv, "--tools") == "Bash,Read,Glob,Grep" and "--strict-mcp-config" in argv
    assert option(argv, "--setting-sources") == "project,local"
    # Skills and commands stay on in the interactive session (/compact, /clear, ...).
    assert "--disable-slash-commands" not in argv


def until(predicate: Callable[[], Any], timeout: float = 30) -> Any:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if value := predicate():
            return value
        time.sleep(0.05)
    raise AssertionError("timed out")


def ps_lines(*agent_ids: str) -> list[str]:
    """The `orch ps` rows of these agents, from one listing."""
    result = CliRunner().invoke(cli.app, ["ps"])
    assert result.exit_code == 0, result.output
    rows = click.unstyle(result.output).splitlines()
    return [next(row for row in rows if f" {agent_id} " in row) for agent_id in agent_ids]


def ps_line(agent_id: str) -> str:
    (line,) = ps_lines(agent_id)
    return line


def test_ps_tells_an_idle_parent_with_working_children_from_a_running_one(
    orch: Orchestrator, project: str
) -> None:
    lead = orch.create_agent(project, name="boss", role="lead", worktree=False)
    child, _ = orch.spawn(project, "SLEEP 20", name="kid", worktree=False, parent_id=lead.id)
    try:
        until(lambda: orch.view(child)["status"] == "running")
        idle = ps_line(lead.id)
        orch.send(lead.id, "SLEEP 20")
        until(lambda: orch.view(lead)["status"] == "running")
        busy = ps_line(lead.id)
        orch.send(lead.id, "later")
        queued, kid = ps_lines(lead.id, child.id)
    finally:
        orch.stop(child.id)
        orch.stop(lead.id)

    assert " idle·1↓ " in idle and "lead" in idle
    assert " running·1↓ " in busy and "+" not in busy.split()[1]
    assert " running·1↓ +1 " in queued
    # The state column is as wide as the longest state, so the role column stays aligned.
    assert queued.index(" lead ") == kid.index(" worker ")


def test_status_lists_working_children_and_the_queue(orch: Orchestrator, project: str) -> None:
    lead = orch.create_agent(project, name="boss", role="lead", worktree=False)
    child = orch.create_agent(project, name="kid", worktree=False, parent_id=lead.id)
    orch.store.add_run(child.id, "work")  # queued, never started: counts as working
    first = orch.store.add_run(lead.id, "please review\nthe branch")
    second = orch.store.add_run(lead.id, "[orch notification] pending", kind=NOTIFICATION)
    third = orch.store.add_run(lead.id, "from a peer", sender=child.id)

    result = CliRunner().invoke(cli.app, ["status", lead.id])

    assert result.exit_code == 0, result.output
    lines = click.unstyle(result.output).splitlines()
    assert "children     1 agent working" in lines
    assert "queue        3 waiting" in lines
    (one,) = [line for line in lines if f"#{first.id} " in line]
    (two,) = [line for line in lines if f"#{second.id} " in line]
    (three,) = [line for line in lines if f"#{third.id} " in line]
    assert "message" in one and " user " in one and "please review the branch" in one
    assert "notification" in two and " system " in two
    assert f" {child.id} " in three and "from a peer" in three
    assert "[{" not in result.output and "'kind'" not in result.output


def test_status_of_an_idle_parent_says_its_agents_work(orch: Orchestrator, project: str) -> None:
    lead = orch.create_agent(project, name="boss", role="lead", worktree=False)
    done = orch.store.add_run(lead.id, "plan")
    orch.store.finish_run(done.id, RunStatus.DONE, result="dispatched")
    for name in ("a", "b"):
        child = orch.create_agent(project, name=name, worktree=False, parent_id=lead.id)
        orch.store.add_run(child.id, "work")

    status = CliRunner().invoke(cli.app, ["status", lead.id])
    ps = ps_line(lead.id)

    assert "idle while its agents work (last run: done)" in status.output
    assert "children     2 agents working" in status.output and "\nqueue " not in status.output
    assert " idle·2↓ " in ps


def test_a_failed_last_turn_is_not_shown_as_idle_while_children_work(
    orch: Orchestrator, project: str
) -> None:
    lead = orch.create_agent(project, name="boss", role="lead", worktree=False)
    orch.send(lead.id, "FAIL")
    until(lambda: orch.view(lead)["status"] == "failed")
    child = orch.create_agent(project, name="kid", worktree=False, parent_id=lead.id)
    orch.store.add_run(child.id, "work")  # queued, never started: counts as working

    result = CliRunner().invoke(cli.app, ["ps"], color=True)
    status = CliRunner().invoke(cli.app, ["status", lead.id])

    (line,) = [row for row in result.output.splitlines() if f" {lead.id} " in row]
    assert " failed·1↓ " in click.unstyle(line) and "idle" not in line
    assert typer.style("failed·1↓", fg=typer.colors.RED) in line
    assert "\nstatus       failed\n" in status.output and "1 agent working" in status.output


def test_ps_keeps_columns_aligned_with_two_digit_counts(orch: Orchestrator, project: str) -> None:
    lead = orch.create_agent(project, name="boss", role="lead", worktree=False)
    plain = orch.create_agent(project, name="plain", worktree=False)
    for n in range(12):
        child = orch.create_agent(project, name=f"kid{n}", worktree=False, parent_id=lead.id)
        orch.store.add_run(child.id, "work")
    for n in range(11):
        orch.store.add_run(lead.id, f"message {n}")

    busy, calm = ps_lines(lead.id, plain.id)

    assert " waiting·12↓ +10 " in busy
    assert busy.index(" lead ") == calm.index(" worker ")


def test_status_of_a_parent_that_never_ran_has_no_last_run(
    orch: Orchestrator, project: str
) -> None:
    lead = orch.create_agent(project, name="boss", role="lead", worktree=False)
    child = orch.create_agent(project, name="kid", worktree=False, parent_id=lead.id)
    orch.store.add_run(child.id, "work")

    result = CliRunner().invoke(cli.app, ["status", lead.id])

    assert "\nstatus       idle while its agents work\n" in result.output
    assert " idle·1↓ " in ps_line(lead.id)


def test_status_counts_queued_runs_beyond_the_listed_queue(
    orch: Orchestrator, project: str
) -> None:
    agent = orch.create_agent(project, name="busy", worktree=False)
    runs = [orch.store.add_run(agent.id, f"message {n}") for n in range(QUEUE_LISTED + 3)]

    result = CliRunner().invoke(cli.app, ["status", agent.id])

    assert result.exit_code == 0, result.output
    assert f"queue        {QUEUE_LISTED + 3} waiting" in result.output
    assert f"#{runs[QUEUE_LISTED - 1].id} " in result.output
    assert f"#{runs[QUEUE_LISTED].id} " not in result.output
    assert "\n  +3 more\n" in result.output
