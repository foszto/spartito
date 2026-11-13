"""Cost reports and budgets (core/costs.py): the report from the recorded usage and what
deleted agents had spent, local-time days, Codex turns without a cost, budgets with their
warnings, the opt-in hard stop, the dashboard's endpoint and view, older databases, backups.
Every test works in conftest's temporary home, with invented data."""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from datetime import date, timedelta
from pathlib import Path

import pytest
from starlette.testclient import TestClient
from test_code_view import FAKE_DOM, page_functions, run_node
from test_store import OLD_SCHEMA
from typer.testing import CliRunner

from orchestrator import cli, config, mcp_server
from orchestrator.core import costs
from orchestrator.errors import OrchestratorError
from orchestrator.models import Agent, Project, RunStatus, Usage
from orchestrator.service import Orchestrator
from orchestrator.store import NOTIFICATION, Store
from orchestrator.web.app import app

ZONE = "Europe/Berlin"  # UTC+1 in November: local midnight is 23:00 UTC the day before


@pytest.fixture
def zone():
    """A fixed local time zone for this process (xdist runs each worker in its own)."""
    before = os.environ.get("TZ")
    os.environ["TZ"] = ZONE
    time.tzset()
    yield
    if before is None:
        os.environ.pop("TZ", None)
    else:
        os.environ["TZ"] = before
    time.tzset()


@pytest.fixture
def o() -> Orchestrator:
    orch = Orchestrator()
    orch.store.add_project(Project("my-app", "/src/my-app", "main", "claude"))
    orch.store.add_project(Project("docs-site", "/src/docs-site", "main", "codex"))
    for agent_id, project, provider in (
        ("orchestrator", None, "claude"),
        ("fix-login-3f2a", "my-app", "claude"),
        ("add-search-9c1d", "my-app", "claude"),
        ("write-guide-7b2e", "docs-site", "codex"),
    ):
        orch.store.add_agent(
            Agent(id=agent_id, project=project, name=agent_id, role="worker", provider=provider,
                  cwd="/w")
        )  # fmt: skip
    return orch


def turn(orch: Orchestrator, agent_id: str, when: str | None, cost: float | None, n: int) -> int:
    """A finished run of `agent_id` whose usage record was written at `when` (UTC; None: now)."""
    run = orch.store.add_run(agent_id, "x")
    orch.store.set_usage(run.id, agent_id, Usage(n, n // 10, 2 * n, n // 2, cost_usd=cost))
    orch.store.finish_run(run.id, RunStatus.DONE, cost_usd=cost)
    if when:
        with orch.store.connection() as conn:
            conn.execute("UPDATE run_usage SET created_at = ? WHERE run_id = ?", (when, run.id))
    return run.id


def seed(orch: Orchestrator) -> None:
    """Spend over three local days, two projects, the orchestrator and a deleted agent."""
    turn(orch, "orchestrator", "2026-11-11T09:00:00+00:00", 0.25, 1000)
    turn(orch, "fix-login-3f2a", "2026-11-11T22:59:59+00:00", 1.0, 2000)  # 23:59:59 local
    turn(orch, "fix-login-3f2a", "2026-11-11T23:00:00+00:00", 2.0, 3000)  # 00:00 on the 12th
    turn(orch, "add-search-9c1d", "2026-11-12T10:00:00+00:00", 0.5, 1000)
    turn(orch, "write-guide-7b2e", "2026-11-12T12:00:00+00:00", None, 4000)  # Codex: no cost
    turn(orch, "write-guide-7b2e", "2026-11-13T08:00:00+00:00", None, 1000)
    orch.store.delete_agent("add-search-9c1d")


def invoke(*args: str):
    return CliRunner().invoke(cli.app, list(args), prog_name=config.CLI_NAME)


def by_label(report: dict) -> dict[str, dict]:
    return {row["label"]: row for row in report["rows"]}


# -- the report --------------------------------------------------------------------------------


def test_the_report_groups_by_project_with_the_removed_agents_per_project(
    o: Orchestrator, zone: None
) -> None:
    seed(o)
    report = o.cost_report()

    assert report["by"] == "project"
    labels = [row["label"] for row in report["rows"]]
    orchestrator = f"{config.ORCHESTRATOR_NAME} (orchestrator)"
    assert labels == [orchestrator, "docs-site", "my-app", "my-app: removed agents"]
    rows = by_label(report)
    assert rows[orchestrator]["cost_usd"] == 0.25 and rows[orchestrator]["project"] is None
    assert rows["my-app"]["turns"] == 2 and rows["my-app"]["cost_usd"] == 3.0
    removed = rows["my-app: removed agents"]
    assert removed["removed"] and removed["turns"] is None and removed["cost_usd"] == 0.5
    assert removed["tokens"] == {"in": 1000, "out": 100, "cache_read": 2000, "cache_write": 500}
    codex = rows["docs-site"]
    assert codex["turns"] == 2 and codex["unpriced"] == 2 and codex["cost_usd"] is None
    assert codex["tokens"]["in"] == 5000
    total = report["total"]
    assert total["turns"] == 5 and total["cost_usd"] == 3.75 and total["unpriced"] == 2
    assert total["tokens"]["in"] == 12000
    assert report["removed_left_out"] is False


def test_the_report_groups_by_local_day_across_midnight(o: Orchestrator, zone: None) -> None:
    seed(o)
    report = o.cost_report("day")

    rows = by_label(report)
    assert list(rows) == ["2026-11-11", "2026-11-12", "2026-11-13", "removed agents (not dated)"]
    assert rows["2026-11-11"]["cost_usd"] == 1.25  # 22:59:59 UTC is still the 11th here
    assert rows["2026-11-12"]["cost_usd"] == 2.0 and rows["2026-11-12"]["unpriced"] == 1
    assert rows["2026-11-12"]["turns"] == 2  # 23:00 UTC on the 11th, and the Codex turn
    assert rows["2026-11-13"]["cost_usd"] is None
    assert rows["removed agents (not dated)"]["cost_usd"] == 0.5


def test_since_starts_at_local_midnight_and_leaves_the_undated_removed_spend_out(
    o: Orchestrator, zone: None
) -> None:
    seed(o)
    assert costs.utc_start(date(2026, 11, 12)) == "2026-11-11T23:00:00+00:00"

    report = o.cost_report("agent", since="2026-11-12")

    rows = by_label(report)
    assert set(rows) == {"fix-login-3f2a", "write-guide-7b2e"}
    assert rows["fix-login-3f2a"]["cost_usd"] == 2.0 and rows["fix-login-3f2a"]["turns"] == 1
    assert report["removed_left_out"] is True and report["since"] == "2026-11-12"
    assert report["total"]["cost_usd"] == 2.0


def test_the_report_by_agent_names_removed_agents_and_filters_by_project(
    o: Orchestrator, zone: None
) -> None:
    seed(o)
    report = o.cost_report("agent", project="my-app")

    assert [row["label"] for row in report["rows"]] == [
        "fix-login-3f2a",
        "add-search-9c1d (removed)",
    ]
    assert report["total"]["cost_usd"] == 3.5
    assert o.cost_report("project", project="docs-site")["total"]["cost_usd"] is None


def test_the_report_refuses_an_unknown_grouping_or_date(o: Orchestrator) -> None:
    with pytest.raises(OrchestratorError, match="unknown grouping"):
        o.cost_report("week")
    with pytest.raises(OrchestratorError, match="not a date"):
        o.cost_report(since="yesterday")


def test_the_command_shows_codex_turns_as_not_reported_never_as_zero(
    o: Orchestrator, zone: None
) -> None:
    seed(o)
    result = invoke("cost", "-p", "docs-site")

    assert result.exit_code == 0, result.output
    line = next(line for line in result.stdout.splitlines() if line.startswith("docs-site"))
    assert line.split()[-1] == "n/a" and "$0.00" not in result.stdout
    assert "2 turn(s) reported no cost (Codex reports none)" in result.stdout
    assert result.stdout.startswith("recorded usage of docs-site by project")

    every = invoke("cost", "--by", "day")
    assert "TOTAL" in every.stdout and "$3.75*" in every.stdout
    assert "removed agents (not dated)" in every.stdout
    data = json.loads(invoke("cost", "--json", "--since", "2026-11-12").stdout)
    assert data["removed_left_out"] is True
    assert "left out with --since" in invoke("cost", "--since", "2026-11-12").stdout
    assert invoke("cost", "--by", "week").exit_code == 1


def test_an_empty_home_reports_nothing_and_a_zero_total(o: Orchestrator) -> None:
    report = o.cost_report()
    assert report["rows"] == [] and report["total"]["cost_usd"] is None
    assert invoke("cost").exit_code == 0


# -- budgets -----------------------------------------------------------------------------------


def test_budgets_are_set_listed_changed_and_removed(o: Orchestrator, zone: None) -> None:
    assert "no budgets" in invoke("budget").stdout

    assert invoke("budget", "set", "10").exit_code == 0
    result = invoke(
        "budget", "set", "2.5", "-p", "my-app", "--period", "day", "--warn-at", "50", "--hard-stop"
    )
    assert result.exit_code == 0, result.output
    listed = {item["project"]: item for item in o.budget_list()}
    assert listed[None]["period"] == "month" and listed[None]["warn_at"] == 80
    assert listed[None]["hard_stop"] is False
    assert listed["my-app"]["amount_usd"] == 2.5 and listed["my-app"]["hard_stop"] is True

    # A value not given stays; the amount alone may be left out for an existing budget.
    assert invoke("budget", "set", "-p", "my-app", "--no-hard-stop").exit_code == 0
    mine = next(item for item in o.budget_list() if item["project"] == "my-app")
    assert (mine["amount_usd"], mine["period"], mine["warn_at"], mine["hard_stop"]) == (
        2.5,
        "day",
        50,
        False,
    )
    table = invoke("budget", "list").stdout
    assert "global" in table and "my-app" in table and "month" in table

    assert invoke("budget", "remove", "-p", "my-app").exit_code == 0
    assert [item["project"] for item in o.budget_list()] == [None]
    gone = invoke("budget", "remove", "-p", "my-app")
    assert gone.exit_code == 1 and "no project my-app budget" in gone.stderr
    assert invoke("budget", "remove").exit_code == 0 and o.budget_list() == []


@pytest.mark.parametrize(
    "args, message",
    [
        (["0"], "positive"),
        (["--", "-5"], "positive"),
        (["5", "--period", "week"], "unknown period"),
        (["5", "--warn-at", "120"], "from 1 to 100"),
        (["5", "-p", "no-such-app"], "unknown project"),
        ([], "give an amount"),
    ],
)
def test_a_budget_is_checked_before_it_is_stored(
    o: Orchestrator, args: list[str], message: str
) -> None:
    result = invoke("budget", "set", *args)
    assert result.exit_code != 0 and message in result.output
    assert o.budget_list() == []


def test_a_budget_counts_its_period_and_scope(o: Orchestrator, zone: None) -> None:
    seed(o)
    turn(o, "fix-login-3f2a", None, 0.75, 100)  # now: today and this month
    o.set_budget(1.0, "my-app", period="day")
    o.set_budget(10.0, period="total")
    o.set_budget(4.0, "docs-site", period="month")

    listed = {item["project"]: item for item in o.budget_list()}
    assert listed["my-app"]["spent_usd"] == 0.75 and listed["my-app"]["percent"] == 75
    assert listed["my-app"]["state"] == "ok" and listed["my-app"]["period_key"] == (
        date.today().isoformat()
    )
    # Total: everything recorded, the orchestrator and the removed agent too.
    assert listed[None]["spent_usd"] == pytest.approx(4.5) and listed[None]["state"] == "ok"
    assert listed["docs-site"]["spent_usd"] == 0  # Codex reports no cost: nothing counted
    o.set_budget(0.7, "my-app")
    assert {i["project"]: i for i in o.budget_list()}["my-app"]["state"] == "reached"
    o.set_budget(0.9, "my-app")
    assert {i["project"]: i for i in o.budget_list()}["my-app"]["state"] == "warning"


@pytest.fixture
def shown(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str | None]]:
    """The warnings the budgets showed (desktop.show replaced: nothing reaches a desktop)."""
    calls: list[tuple[str, str | None]] = []
    monkeypatch.setattr(costs.desktop, "show", lambda store, text, agent_id=None: calls.append((text, agent_id)))  # fmt: skip
    return calls


@pytest.fixture
def today(monkeypatch: pytest.MonkeyPatch) -> list[date]:
    """The local day the budgets take as today: today[0], changed by the test."""
    day = [date(2026, 11, 13)]
    real = costs.current_period
    monkeypatch.setattr(costs, "current_period", lambda period, _=None: real(period, day[0]))
    return day


def test_the_warning_fires_once_at_the_threshold_and_once_at_the_amount_per_period(
    o: Orchestrator, zone: None, shown: list, today: list[date]
) -> None:
    o.set_budget(1.0, "my-app", period="day")
    agent = "fix-login-3f2a"
    turn(o, agent, "2026-11-13T08:00:00+00:00", 0.5, 10)
    assert costs.usage_recorded(o.store, agent) == []  # 50 %: below 80 %

    turn(o, agent, "2026-11-13T09:00:00+00:00", 0.375, 10)  # 87.5 %
    assert costs.usage_recorded(o.store, agent) == [
        "Budget warning, project my-app: $0.88 of $1.00 today (87 %)"
    ]
    turn(o, agent, "2026-11-13T09:30:00+00:00", 0.0625, 10)  # 93.75 %: warned already
    assert costs.usage_recorded(o.store, agent) == []

    turn(o, agent, "2026-11-13T10:00:00+00:00", 0.25, 10)  # 118.75 %
    assert costs.usage_recorded(o.store, agent) == [
        "Budget reached, project my-app: $1.19 of $1.00 today (118 %); turns go on (no hard stop)"
    ]
    assert costs.usage_recorded(o.store, agent) == []
    assert Orchestrator().budget_list()[0]["announced"] == costs.REACHED  # a restart remembers
    assert [agent_id for _, agent_id in shown] == [agent, agent]

    today[0] = date(2026, 11, 14)  # the next day: a period of its own
    turn(o, agent, "2026-11-14T08:00:00+00:00", 0.875, 10)
    assert costs.usage_recorded(o.store, agent) == [
        "Budget warning, project my-app: $0.88 of $1.00 today (87 %)"
    ]
    assert len(shown) == 3


def test_a_jump_past_both_announces_the_amount_and_another_projects_turn_none(
    o: Orchestrator, zone: None, shown: list, today: list[date]
) -> None:
    o.set_budget(1.0, "my-app", period="month", hard_stop=True)
    turn(o, "fix-login-3f2a", "2026-11-02T08:00:00+00:00", 3.0, 10)

    assert costs.usage_recorded(o.store, "write-guide-7b2e") == []  # docs-site: not counted
    assert costs.usage_recorded(o.store, "fix-login-3f2a") == [
        "Budget reached, project my-app: $3.00 of $1.00 this month (300 %);"
        " new turns are refused (hard stop)"
    ]


def test_changing_a_budget_forgets_what_was_announced(
    o: Orchestrator, zone: None, shown: list, today: list[date]
) -> None:
    o.set_budget(1.0, period="day")
    turn(o, "orchestrator", "2026-11-13T08:00:00+00:00", 0.875, 10)
    assert len(costs.usage_recorded(o.store, "orchestrator")) == 1
    o.set_budget(None, hard_stop=True)  # the stop only: what was announced stays
    assert costs.usage_recorded(o.store, "orchestrator") == []
    o.set_budget(1.05)  # a new amount (83 %): its threshold warns anew
    assert len(costs.usage_recorded(o.store, "orchestrator")) == 1


def test_concurrent_recorders_announce_a_level_once(
    o: Orchestrator, zone: None, shown: list, today: list[date]
) -> None:
    o.set_budget(1.0, period="day")
    turn(o, "fix-login-3f2a", "2026-11-13T08:00:00+00:00", 0.875, 10)
    start = threading.Barrier(8)
    results: list[list[str]] = []

    def record() -> None:
        store = Store()  # each recorder with its own store, as each runner has
        start.wait()
        results.append(costs.usage_recorded(store, "fix-login-3f2a"))

    threads = [threading.Thread(target=record) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert sorted(len(texts) for texts in results) == [0] * 7 + [1]
    assert len(shown) == 1


def test_raise_announced_takes_one_level_once_and_not_after_a_change(o: Orchestrator) -> None:
    o.set_budget(1.0)
    budget = o.store.budgets()[0]
    assert o.store.raise_announced(budget, "2026-11", 1) is True
    assert o.store.raise_announced(budget, "2026-11", 1) is False
    assert o.store.raise_announced(budget, "2026-11", 2) is True
    assert o.store.raise_announced(budget, "2026-11", 1) is False
    assert o.store.raise_announced(budget, "2026-12", 1) is True
    o.set_budget(2.0)
    assert o.store.raise_announced(budget, "2026-12", 2) is False  # read before the change


# -- the warning and the hard stop on real turns (fake provider) -------------------------------


def finished(orch: Orchestrator, *agent_ids: str) -> None:
    done, _ = orch.wait(list(agent_ids), timeout=30)
    assert done, "agents did not finish in time"


def test_a_turn_that_crosses_a_threshold_warns_through_the_notices(
    orch: Orchestrator, project: str
) -> None:
    orch.set_budget(0.001, project, period="day")  # TOKENS 100 costs 0.001
    agent, run = orch.spawn(project, "TOKENS 100", worktree=False)
    finished(orch, agent.id)

    assert orch.get_run(run.id).status is RunStatus.DONE
    texts = [notice["text"] for notice in orch.notice_trace()]
    assert any(text.startswith(f"Budget reached, project {project}: $0.00") for text in texts)
    assert orch.budget_list()[0]["announced"] == costs.REACHED


def test_without_a_budget_or_with_the_stop_off_turns_run_as_before(
    orch: Orchestrator, project: str
) -> None:
    agent, first = orch.spawn(project, "TOKENS 100", worktree=False)
    finished(orch, agent.id)
    assert orch.get_run(first.id).status is RunStatus.DONE
    orch.set_budget(0.0001, project)  # reached, the hard stop off (the default)

    second = orch.send(agent.id, "TOKENS 100")
    finished(orch, agent.id)

    assert orch.get_run(second.id).status is RunStatus.DONE
    assert orch.budget_list()[0]["state"] == "reached"
    assert costs.refusal(orch.store, project) is None


def test_the_hard_stop_refuses_a_new_turn_but_never_stops_a_running_one(
    orch: Orchestrator, project: str
) -> None:
    agent, first = orch.spawn(project, "TOKENS 100", worktree=False)
    finished(orch, agent.id)
    pids = Path(agent.cwd) / ".fake-pids"  # each launch of the fake CLI adds its pid first
    launched = len(pids.read_text().splitlines())
    running = orch.send(agent.id, "SLEEP 2 TOKENS 100")
    deadline = time.monotonic() + 15
    while len(pids.read_text().splitlines()) <= launched:  # its CLI runs: past any refusal
        assert time.monotonic() < deadline
        time.sleep(0.05)
    orch.set_budget(0.0005, project, hard_stop=True)  # reached while the turn runs

    finished(orch, agent.id)
    assert orch.get_run(running.id).status is RunStatus.DONE

    refused = orch.send(agent.id, "TOKENS 100")
    finished(orch, agent.id)
    run = orch.get_run(refused.id)
    assert run.status is RunStatus.FAILED
    assert run.error == (
        f"refused by the hard stop of the project {project} budget, which is reached:"
        " $0.00 of $0.00 this month (400 %). Changing a budget is the owner's decision, not an"
        ' agent\'s (see "Budgets" in docs/configuration.md).'
    )
    assert config.CLI_NAME not in run.error  # the agent that reads it gets no command
    assert orch.get_usage(refused.id) is None  # the CLI never ran

    orch.set_budget(1.0, project)  # raised: turns run again, the stop stays on
    again = orch.send(agent.id, "TOKENS 100")
    finished(orch, agent.id)
    assert orch.get_run(again.id).status is RunStatus.DONE


def test_the_hard_stop_lets_a_compaction_run(orch: Orchestrator, project: str) -> None:
    agent, _ = orch.spawn(project, "TOKENS 100", worktree=False)
    finished(orch, agent.id)
    orch.set_budget(0.0005, hard_stop=True)  # the global budget, reached

    compaction = orch.compact(agent.id)
    finished(orch, agent.id)

    assert orch.get_run(compaction.id).status is RunStatus.DONE
    assert "budget" not in (orch.get_run(compaction.id).error or "")


def test_the_hard_stop_lets_the_parents_notification_turn_run(
    orch: Orchestrator, project: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    lead = orch.create_agent(project, name="lead", role="lead", worktree=False).id
    spender = orch.create_agent(project, name="spender", worktree=False).id
    turn(orch, spender, None, 0.001, 100)
    orch.set_budget(0.0005, hard_stop=True)  # the global budget, reached
    monkeypatch.setenv("ORCH_AGENT_ID", lead)  # the lead's MCP server spawns the child
    child = mcp_server.spawn_agent("TOKENS 10", worktree=False)["id"]
    monkeypatch.delenv("ORCH_AGENT_ID")

    assert orch.wait_idle([lead, child], 30), "agents did not settle in time"

    (refused,) = orch.store.runs_of(child)
    assert refused.status is RunStatus.FAILED and "refused by the hard stop" in refused.error
    meta = orch.store.meta_of(lead)
    notes = [run for run in orch.store.runs_of(lead) if meta[run.id].kind == NOTIFICATION]
    assert notes and all(run.status is RunStatus.DONE for run in notes)
    assert "refused by the hard stop" in notes[0].prompt  # the lead learns why


def test_a_failing_budget_check_lets_the_turn_run(
    orch: Orchestrator, project: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from orchestrator import runner

    def broken(*args: object) -> None:
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(costs, "refusal", broken)
    agent = orch.store.get_agent(orch.spawn(project, "x", worktree=False)[0].id)
    assert runner.budget_refusal(orch.store, agent) is None
    finished(orch, agent.id)


# -- the schema --------------------------------------------------------------------------------


def test_an_older_database_gets_the_table_and_works(tmp_path: Path) -> None:
    path = tmp_path / "old.db"
    with sqlite3.connect(path) as conn:
        conn.executescript(OLD_SCHEMA)
    conn.close()

    orch = Orchestrator(Store(path))

    assert orch.budget_list() == []
    orch.set_budget(5.0, "p", period="total")
    # The old runs have no usage records: recorded usage is empty, the budget counts nothing.
    assert orch.cost_report()["rows"] == []
    assert orch.budget_list()[0]["spent_usd"] == 0
    Store(path)  # opening it again changes nothing
    assert len(orch.budget_list()) == 1


def test_older_code_reading_the_settings_never_sees_a_budget(o: Orchestrator) -> None:
    o.set_budget(5.0)
    assert o.store.get_settings("") == {}


def test_backup_and_restore_carry_the_budgets(o: Orchestrator) -> None:
    o.set_budget(5.0, "my-app", period="day", warn_at=60, hard_stop=True)
    name = o.backup_state()["name"]
    o.remove_budget("my-app")
    o.set_budget(9.0)

    o.restore_state(name, confirm=True)

    (budget,) = o.budget_list()
    assert (budget["project"], budget["amount_usd"], budget["period"]) == ("my-app", 5.0, "day")
    assert budget["warn_at"] == 60 and budget["hard_stop"] is True


# -- the dashboard -----------------------------------------------------------------------------


def models_and_roles(orch: Orchestrator) -> None:
    """The seeded turns' models (the orchestrator's names none) and the agents' roles."""
    with orch.store.connection() as conn:
        for agent_id, model in (
            ("fix-login-3f2a", "claude-opus-5-5"),
            ("write-guide-7b2e", "gpt-5"),
        ):
            conn.execute("UPDATE run_usage SET model = ? WHERE agent_id = ?", (model, agent_id))
        for agent_id, role in (("orchestrator", "orchestrator"), ("fix-login-3f2a", "lead")):
            conn.execute("UPDATE agents SET role = ? WHERE id = ?", (role, agent_id))


def test_the_report_groups_by_model_and_by_role(o: Orchestrator, zone: None) -> None:
    seed(o)
    models_and_roles(o)

    by_model = by_label(o.cost_report("model"))
    assert list(by_model) == [
        "unknown",
        "claude-opus-5-5",
        "gpt-5",
        "removed agents (model not recorded)",
    ]
    assert by_model["unknown"]["cost_usd"] == 0.25 and by_model["unknown"]["key"] is None
    assert (
        by_model["claude-opus-5-5"]["turns"] == 2 and by_model["claude-opus-5-5"]["cost_usd"] == 3.0
    )
    assert by_model["gpt-5"]["cost_usd"] is None and by_model["gpt-5"]["unpriced"] == 2
    assert by_model["removed agents (model not recorded)"]["removed"]

    by_role = by_label(o.cost_report("role"))
    assert list(by_role) == ["lead", "orchestrator", "worker", "removed"]
    assert by_role["lead"]["cost_usd"] == 3.0 and by_role["worker"]["cost_usd"] is None
    assert by_role["removed"]["removed"] and by_role["removed"]["cost_usd"] == 0.5
    assert "removed" not in by_label(o.cost_report("role", since="2026-11-11"))


def test_until_ends_with_its_local_day_and_leaves_the_removed_spend_out(
    o: Orchestrator, zone: None
) -> None:
    seed(o)

    report = o.cost_report("agent", until="2026-11-11")

    rows = by_label(report)
    assert set(rows) == {"orchestrator", "fix-login-3f2a"}
    assert rows["fix-login-3f2a"]["cost_usd"] == 1.0  # 22:59:59 UTC is still the 11th here
    assert report["until"] == "2026-11-11" and report["since"] is None
    assert report["removed_left_out"] is True and report["days"] == ["2026-11-11"]
    one = o.cost_report("project", "2026-11-12", None, "2026-11-12")
    assert one["total"]["turns"] == 2 and one["total"]["cost_usd"] == 2.0
    assert one["days"] == ["2026-11-12"]


def test_the_report_refuses_an_inverted_range_or_a_bad_end_day(o: Orchestrator) -> None:
    with pytest.raises(OrchestratorError, match="before the start day"):
        o.cost_report(since="2026-11-12", until="2026-11-11")
    with pytest.raises(OrchestratorError, match="not a date"):
        o.cost_report(until="tomorrow")


@pytest.mark.parametrize(
    ("option", "day"),
    [("until", "9999-12-31"), ("until", "9999-12-30"), ("since", "0001-01-01")],
)
def test_a_day_at_the_edge_of_the_calendar_is_refused(
    o: Orchestrator, client: TestClient, zone: None, option: str, day: str
) -> None:
    seed(o)

    with pytest.raises(OrchestratorError, match=f"out of range: {day} .*edge of the calendar"):
        o.cost_report(**{option: day})

    answer = client.get(f"/api/cost?{option}={day}")
    assert answer.status_code == 400 and "out of range" in answer.json()["error"]
    result = invoke("cost", f"--{option}", day)
    assert result.exit_code == 1, result.output
    assert result.stderr == f"error: out of range: {day} (too close to the edge of the calendar)\n"


def test_a_range_of_more_than_ten_years_is_refused(
    o: Orchestrator, client: TestClient, zone: None
) -> None:
    seed(o)
    first = date(2016, 1, 1)
    limit = costs.MAX_RANGE_DAYS

    at_limit = o.cost_report(since="2016-01-01", until=str(first + timedelta(days=limit - 1)))

    assert len(at_limit["days"]) == limit and at_limit["days"][0] == "2016-01-01"
    beyond = str(first + timedelta(days=limit))
    with pytest.raises(OrchestratorError, match="has 3,661 days, more than 3,660 .*narrow"):
        o.cost_report(since="2016-01-01", until=beyond)
    assert client.get("/api/cost?since=0002-01-01").status_code == 400
    turn(o, "orchestrator", "2000-01-01T12:00:00+00:00", 0.25, 1000)  # all time: from here
    with pytest.raises(OrchestratorError, match="the range from 2000-01-01 to "):
        o.cost_report()


def test_the_series_has_every_day_of_the_range_and_the_dated_spend_per_group(
    o: Orchestrator, zone: None
) -> None:
    seed(o)
    models_and_roles(o)

    report = o.cost_report("model", since="2026-11-10", until="2026-11-14")

    assert report["days"] == [f"2026-11-{day}" for day in range(10, 15)]
    series = report["series"]
    assert set(series) == {"2026-11-11", "2026-11-12", "2026-11-13"}  # days with records
    assert series["2026-11-11"]["unknown"]["cost_usd"] == 0.25
    assert series["2026-11-11"]["claude-opus-5-5"]["cost_usd"] == 1.0
    codex = series["2026-11-12"]["gpt-5"]
    assert codex["cost_usd"] is None and codex["unpriced"] == 1 and codex["turns"] == 1
    assert codex["tokens"] == {"in": 4000, "out": 400, "cache_read": 8000, "cache_write": 2000}

    every = o.cost_report("project")
    assert every["days"][0] == "2026-11-11" and every["days"][-1] >= "2026-11-13"
    assert every["series"]["2026-11-12"]["my-app"]["cost_usd"] == 2.0  # removed spend: undated
    assert sum(len(groups) for groups in every["series"].values()) == 5
    assert o.cost_report("project", project="nothing-here")["days"] == []


def test_the_series_of_a_dated_range_adds_up_to_its_totals(o: Orchestrator, zone: None) -> None:
    seed(o)
    models_and_roles(o)

    report = o.cost_report("model", since="2026-11-10", until="2026-11-14")

    cells = [cell for groups in report["series"].values() for cell in groups.values()]
    total = report["total"]
    assert total["unpriced"] == 2  # the Codex turns
    assert sum(cell["cost_usd"] or 0 for cell in cells) == pytest.approx(total["cost_usd"])
    for short in total["tokens"]:
        assert sum(cell["tokens"][short] for cell in cells) == total["tokens"][short], short
    assert sum(cell["turns"] for cell in cells) == total["turns"]
    assert sum(cell["unpriced"] for cell in cells) == total["unpriced"]


def test_until_keeps_the_whole_of_a_25_hour_local_day(o: Orchestrator, zone: None) -> None:
    # Europe/Berlin leaves summer time on 2026-10-25: the local day runs from 22:00 UTC on the
    # 24th to 23:00 UTC on the 25th.
    turn(o, "fix-login-3f2a", "2026-10-24T22:00:00+00:00", 1.0, 1000)  # 00:00:00 local
    turn(o, "fix-login-3f2a", "2026-10-25T22:59:59+00:00", 2.0, 1000)  # 23:59:59 local
    turn(o, "fix-login-3f2a", "2026-10-25T23:00:00+00:00", 4.0, 1000)  # 00:00:00 on the 26th

    report = o.cost_report("day", until="2026-10-25")

    assert [row["label"] for row in report["rows"]] == ["2026-10-25"]
    assert report["total"]["turns"] == 2 and report["total"]["cost_usd"] == 3.0
    assert report["days"] == ["2026-10-25"]
    next_day = o.cost_report("day", since="2026-10-26", until="2026-10-26")
    assert next_day["total"]["cost_usd"] == 4.0


@pytest.fixture
def client(o: Orchestrator) -> TestClient:
    return TestClient(app, base_url="http://localhost")


def test_the_endpoint_serves_the_report_and_the_budgets(
    client: TestClient, o: Orchestrator, zone: None
) -> None:
    seed(o)
    o.set_budget(10.0, period="total")

    data = client.get("/api/cost?by=day&since=2026-11-12&project=my-app").json()

    assert data["report"]["by"] == "day" and data["report"]["project"] == "my-app"
    assert [row["label"] for row in data["report"]["rows"]] == ["2026-11-12"]
    assert data["budgets"][0]["spent_usd"] == pytest.approx(3.75)
    assert client.get("/api/cost").json()["report"]["by"] == "project"
    bad = client.get("/api/cost?by=week")
    assert bad.status_code == 400 and "unknown grouping" in bad.json()["error"]
    state = client.get("/api/state").json()
    assert "budgets" not in state and "report" not in state  # loaded only by the cost view


def test_the_endpoint_takes_the_end_day_and_the_new_groupings(
    client: TestClient, o: Orchestrator, zone: None
) -> None:
    seed(o)
    models_and_roles(o)

    data = client.get("/api/cost?by=role&since=2026-11-11&until=2026-11-11").json()["report"]

    assert [row["label"] for row in data["rows"]] == ["lead", "orchestrator"]
    assert data["until"] == "2026-11-11" and data["days"] == ["2026-11-11"]
    assert set(data["series"]["2026-11-11"]) == {"lead", "orchestrator"}
    model = client.get("/api/cost?by=model&until=2026-11-12").json()["report"]
    assert {row["label"] for row in model["rows"]} == {"unknown", "claude-opus-5-5", "gpt-5"}
    inverted = client.get("/api/cost?since=2026-11-12&until=2026-11-11")
    assert inverted.status_code == 400 and "before the start day" in inverted.json()["error"]
    assert client.get("/api/cost?until=soon").status_code == 400


def test_the_command_takes_the_end_day_and_the_new_groupings(o: Orchestrator, zone: None) -> None:
    seed(o)
    models_and_roles(o)

    result = invoke("cost", "--by", "role", "--until", "2026-11-11")

    assert result.exit_code == 0, result.output
    assert result.stdout.startswith("recorded usage by role, from the start to 2026-11-11")
    assert "ROLE" in result.stdout and "lead" in result.stdout
    assert "left out with --since or --until" in result.stdout
    assert "unknown" in invoke("cost", "--by", "model").stdout
    assert invoke("cost", "--since", "2026-11-12", "--until", "2026-11-11").exit_code == 1


def test_the_endpoint_is_get_only_behind_the_hosts_and_origins_guards(client: TestClient) -> None:
    assert client.get("/api/cost", headers={"host": "evil.example"}).status_code == 403
    foreign = client.post("/api/cost", json={}, headers={"origin": "http://evil.example"})
    assert foreign.status_code == 403
    own = client.post("/api/cost", json={}, headers={"origin": "http://localhost"})
    assert own.status_code == 405
    for method in ("put", "delete", "patch"):
        answer = getattr(client, method)("/api/cost", headers={"origin": "http://localhost"})
        assert answer.status_code in (405, 415), method


def test_the_page_builds_the_cost_url_and_texts() -> None:
    source = "const CLI = 'spartito';\n" + page_functions(
        "plural", "COST_PERIODS", "costUrl", "moneyText", "costCell", "costNotes", "budgetText"
    )
    cases = {
        "plain": "costUrl({by: 'project', since: '', project: ''})",
        "full": "costUrl({by: 'day', since: '2026-11-12', project: 'my app&x'})",
        "money": "[moneyText(1.234), moneyText(0), moneyText(null), moneyText(undefined)]",
        "cells": "[costCell({cost_usd: 2, unpriced: 1}), costCell({cost_usd: null, unpriced: 2}),"
        " costCell({cost_usd: 2, unpriced: 0})]",
        "notes": "costNotes({unpriced: 1, removed_left_out: true, rows: [{removed: true}]})",
        "budget": "budgetText({project: null, spent_usd: 8.5, amount_usd: 10, period: 'month',"
        " percent: 85, warn_at: 80, hard_stop: true})",
    }
    code = source + "\nprocess.stdout.write(JSON.stringify({"
    code += ", ".join(f"{key}: {expr}" for key, expr in cases.items()) + "}));"

    result = run_node(code)

    assert result["plain"] == "/api/cost?by=project"
    assert result["full"] == "/api/cost?by=day&since=2026-11-12&project=my+app%26x"
    assert result["money"] == ["$1.23", "$0.00", "n/a", "n/a"]
    assert result["cells"] == ["$2.00*", "n/a", "$2.00"]
    assert result["notes"][1].startswith("* 1 turn reported no cost (Codex reports none)")
    assert len(result["notes"]) == 4
    assert result["budget"] == (
        "Global: $8.50 of $10.00 this month (85 %) · warns at 80 % · hard stop on"
    )


def test_the_page_renders_the_report_and_budgets_as_text_never_as_markup() -> None:
    source = (
        "const CLI = 'spartito';\n"
        + FAKE_DOM
        + page_functions(
            "el",
            "count",
            "COST_PERIODS",
            "BUDGET_STATES",
            "moneyText",
            "costCell",
            "costTable",
            "budgetText",
            "budgetList",
        )
    )
    tokens = "{in: 1000, out: 20, cache_read: 500, cache_write: 0}"
    row = (
        "{label: '<img src=x onerror=alert(1)>', removed: false, turns: 2, unpriced: 1,"
        f" tokens: {tokens}, cost_usd: 1.5}}"
    )
    removed = f"{{label: 'my-app: removed agents', removed: true, turns: null, unpriced: 0, tokens: {tokens}, cost_usd: null}}"  # fmt: skip
    report = f"{{by: 'agent', rows: [{row}, {removed}], total: {row}}}"
    budget = (
        "{project: '<b>my-app</b>', spent_usd: 1, amount_usd: 1, period: 'day', percent: 100,"
        " warn_at: 80, hard_stop: false, state: '<i>odd</i>'}"
    )
    code = source + (
        f"\nprocess.stdout.write(JSON.stringify({{table: dump(costTable({report})),"
        f" budgets: dump(budgetList([{budget}])), none: dump(budgetList([]))}}));"
    )

    result = run_node(code)

    def texts(node: dict) -> list[str]:
        return [node["text"], *(t for child in node["children"] for t in texts(child))]

    table = result["table"]
    rows = table["children"]
    assert [cell["text"] for cell in rows[0]["children"]] == [
        "Agent",
        "Turns",
        "Prompt",
        "Output",
        "Cost",
    ]
    assert [cell["text"] for cell in rows[1]["children"]] == [
        "<img src=x onerror=alert(1)>",
        "2",
        "1.5k",
        "20",
        "$1.50*",
    ]
    assert rows[2]["cls"] == "removed" and rows[2]["children"][1]["text"] == "–"
    assert rows[2]["children"][4]["text"] == "n/a"
    assert rows[3]["cls"] == "total" and rows[3]["children"][0]["text"] == "Total"
    pill = result["budgets"]["children"][0]["children"][0]
    assert pill["cls"] == "pill budget-ok" and pill["text"] == "<i>odd</i>"
    assert any("<b>my-app</b>: $1.00 of $1.00 today (100 %)" in t for t in texts(result["budgets"]))
    assert "`spartito budget set AMOUNT [-p PROJECT]`" in texts(result["none"])[1]


def test_the_page_builds_the_range_and_the_chart_data() -> None:
    source = page_functions(
        "plural", "count", "COST_SERIES_MAX", "costUrl", "localDayText", "costRange",
        "moneyText", "allTokens", "costChartData", "costPartTitle",
    )  # fmt: skip
    tokens = "{in: 100, out: 10, cache_read: 0, cache_write: 0}"
    many = ", ".join(
        f"'g{i}': {{turns: 1, unpriced: 0, tokens: {tokens}, cost_usd: {i + 1}}}" for i in range(10)
    )
    report = (
        "{days: ['2026-11-12', '2026-11-13'], series: {'2026-11-12': {" + many + ","
        " 'codex': {turns: 2, unpriced: 2, tokens: {in: 5000, out: 0, cache_read: 0,"
        " cache_write: 0}, cost_usd: null}}}}"
    )
    cases = {
        "until": "costUrl({by: 'model', since: '2026-11-01', until: '2026-11-13', project: ''})",
        "week": "costRange('7', new Date(2026, 10, 13, 15))",
        "month": "costRange('30', new Date(2026, 0, 5))",
        "all": "costRange('all', new Date(2026, 10, 13))",
        "data": f"costChartData({report}, 'cost')",
        "tokens": f"costChartData({report}, 'tokens').names.slice(0, 1)",
    }
    code = source + "\nprocess.stdout.write(JSON.stringify({"
    code += ", ".join(f"{key}: {expr}" for key, expr in cases.items()) + "}));"
    code = code.replace(
        "JSON.stringify({",
        "JSON.stringify({title: costPartTitle('2026-11-12',"
        " {name: 'gpt-5', cost: null, tokens: 1200, turns: 2, unpriced: 2}), ",
        1,
    )

    result = run_node(code)

    assert result["until"] == "/api/cost?by=model&since=2026-11-01&until=2026-11-13"
    assert result["week"] == {"since": "2026-11-07", "until": "2026-11-13"}
    assert result["month"] == {"since": "2025-12-07", "until": "2026-01-05"}
    assert result["all"] == {"since": "", "until": ""}
    data = result["data"]
    assert data["names"][:2] == ["g9", "g8"] and data["names"][-1] == "other (3 more)"
    assert len(data["names"]) == 9
    first, second = data["days"]
    assert second == {"day": "2026-11-13", "parts": [], "value": 0, "unpriced": 0}
    assert first["value"] == 55 and data["max"] == 55 and first["unpriced"] == 2
    other = first["parts"][-1]
    assert other["series"] == 8 and other["cost"] == 3 and other["unpriced"] == 2
    assert other["turns"] == 4  # g1, g0 and the two Codex turns
    assert result["tokens"] == ["codex"]  # by tokens: the Codex group has the most
    assert result["title"] == "2026-11-12 · gpt-5: cost n/a · 1.2k tokens · 2 turns"


def test_the_page_draws_the_chart_as_text_never_as_markup() -> None:
    svg = (
        "Node.prototype.setAttribute = function (name, value) {"
        " (this.attrs ||= {})[name] = value; };\n"
        "document.createElementNS = (ns, tag) => new Node(tag);\n"
    )
    source = FAKE_DOM + svg + page_functions(
        "el", "plural", "count", "COST_SERIES_MAX", "moneyText", "allTokens", "costChartData",
        "costPartTitle", "svgNode", "costChart",
    )  # fmt: skip
    tokens = "{in: 100, out: 10, cache_read: 0, cache_write: 0}"
    report = (
        "{days: ['2026-11-12', '2026-11-13'], series: {'2026-11-12': {"
        f"'<b>my-app</b>': {{turns: 1, unpriced: 0, tokens: {tokens}, cost_usd: 1.5}},"
        f" 'docs-site': {{turns: 1, unpriced: 1, tokens: {tokens}, cost_usd: null}}}}}}}}"
    )
    code = source + (
        f"\nprocess.stdout.write(JSON.stringify({{cost: dump(costChart({report}, 'cost')),"
        f" tokens: dump(costChart({report}, 'tokens')),"
        " none: dump(costChart({days: [], series: {}}, 'cost')),"
        " unused: dump(costChart({days: ['2025-01-01', '2025-01-02'], series: {}}, 'cost'))}));"
    )

    result = run_node(code)

    def walk(node: dict) -> list[dict]:
        return [node, *(n for child in node["children"] for n in walk(child))]

    nodes = walk(result["cost"])
    texts = [node["text"] for node in nodes]
    assert "2026-11-12 · <b>my-app</b>: $1.50 · 110 tokens · 1 turn" in texts
    assert any(
        t.startswith("2026-11-12: 1 turn reported no cost (Codex reports none)") for t in texts
    )
    assert "2026-11-12: $1.50*" in texts  # the day's total: marked as partly unpriced
    assert "2026-11-13: nothing recorded" in texts
    assert "<b>my-app</b>" in texts and "turns without a reported cost" in texts  # the legend
    assert [n["tag"] for n in nodes].count("circle") == 1
    bars = [n for n in nodes if n["tag"] == "rect" and n["children"][0]["text"].count("·") == 3]
    assert len(bars) == 1  # the Codex part has no cost: no bar, never $0
    assert "11-12" in texts and "11-13" in texts and "$1.50" in texts  # axis labels
    tokens_nodes = walk(result["tokens"])
    assert [n["tag"] for n in tokens_nodes].count("circle") == 0
    assert (
        sum(1 for n in tokens_nodes if n["tag"] == "rect" and "·" in n["children"][0]["text"]) == 2
    )
    assert result["none"]["children"][0]["text"] == "No dated spend in this range."
    unused = result["unused"]["children"]  # days, but no usage: the note, never an empty axis
    assert [(n["tag"], n["text"]) for n in unused] == [("div", "No dated spend in this range.")]


def test_utc_start_and_local_day_agree_on_the_boundary(zone: None) -> None:
    start = costs.utc_start(date(2026, 11, 12))
    assert costs.local_day(start) == "2026-11-12"
    one_second_before = "2026-11-11T22:59:59+00:00"
    assert costs.local_day(one_second_before) == "2026-11-11"
    assert costs.local_day("2026-11-11T23:00:00") == "2026-11-12"  # no zone: UTC
    assert costs.current_period("month", date(2026, 11, 13)) == ("2026-11", date(2026, 11, 1))
    assert costs.current_period("total", date(2026, 11, 13)) == ("total", None)
