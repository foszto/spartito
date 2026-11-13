"""Cost reports and budgets (core/costs.py): the report from the recorded usage and what
deleted agents had spent, local-time days, Codex turns without a cost, budgets with their
warnings, the opt-in hard stop, older databases, backups.
Every test works in conftest's temporary home, with invented data."""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from datetime import date
from pathlib import Path

import pytest
from test_store import OLD_SCHEMA
from typer.testing import CliRunner

from orchestrator import cli, config
from orchestrator.core import costs
from orchestrator.errors import OrchestratorError
from orchestrator.models import Agent, Project, RunStatus, Usage
from orchestrator.service import Orchestrator
from orchestrator.store import Store

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
    running = orch.send(agent.id, "SLEEP 2 TOKENS 100")
    deadline = time.monotonic() + 15
    while orch.get_run(running.id).status is not RunStatus.RUNNING:
        assert time.monotonic() < deadline
        time.sleep(0.05)
    orch.set_budget(0.0005, project, hard_stop=True)  # reached while the turn runs

    finished(orch, agent.id)
    assert orch.get_run(running.id).status is RunStatus.DONE

    refused = orch.send(agent.id, "TOKENS 100")
    finished(orch, agent.id)
    run = orch.get_run(refused.id)
    assert run.status is RunStatus.FAILED
    assert f"the project {project} budget is reached" in run.error
    assert f"`{config.CLI_NAME} budget set <amount> -p {project}`" in run.error
    assert f"`{config.CLI_NAME} budget remove -p {project}`" in run.error
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


def test_utc_start_and_local_day_agree_on_the_boundary(zone: None) -> None:
    start = costs.utc_start(date(2026, 11, 12))
    assert costs.local_day(start) == "2026-11-12"
    one_second_before = "2026-11-11T22:59:59+00:00"
    assert costs.local_day(one_second_before) == "2026-11-11"
    assert costs.local_day("2026-11-11T23:00:00") == "2026-11-12"  # no zone: UTC
    assert costs.current_period("month", date(2026, 11, 13)) == ("2026-11", date(2026, 11, 1))
    assert costs.current_period("total", date(2026, 11, 13)) == ("total", None)
