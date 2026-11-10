"""The MCP tools and the `orch` command line over a store with recorded usage."""

from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner

from orchestrator import cli, mcp_server
from orchestrator.models import RunStatus, Usage
from orchestrator.service import Orchestrator


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
    assert (listed["model_override"], listed["effort_override"]) == (None, "high")
    assert listed["usage"] == {
        "input_tokens": 1200,
        "output_tokens": 340,
        "cache_read_tokens": 56000,
        "cache_creation_tokens": 7800,
        "cost_usd": 0.0123,
    }
    assert listed["last_run"]["usage"]["model"] == "fake-model-1"
    assert "recent_runs" not in listed
    first, second = detail["recent_runs"]
    assert (first["status"], first["cost_usd"], "input_tokens" in first) == ("done", None, False)
    assert (second["input_tokens"], second["model"], second["cost_usd"]) == (
        1200,
        "fake-model-1",
        0.0123,
    )
    assert json.loads(json.dumps(detail))["usage"]["output_tokens"] == 340


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
