"""Per-run values and agent totals when CLIs report session totals and old-code runs mix in."""

from __future__ import annotations

import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest
from test_store import OLD_SCHEMA

from orchestrator import store as store_module
from orchestrator.models import COMPACT, Agent, Event, RunStatus, Usage, spent
from orchestrator.runner import record_usage
from orchestrator.service import Orchestrator
from orchestrator.store import Store


@pytest.fixture
def store(tmp_path: Path) -> Store:
    """Agent a-1 with two runs by old code: Claude session totals 0.25, then 0.75."""
    path = tmp_path / "orch.db"
    with sqlite3.connect(path) as conn:
        conn.executescript(OLD_SCHEMA)
    conn.close()
    return Store(path)


def without_old_runs(store: Store) -> None:
    store.delete_agent("a-1")
    store.add_agent(
        Agent(id="a-1", project="p", name="a", role="worker", provider="claude", cwd="/w")
    )


def new_code_run(store: Store, usage: Usage | None, session: str | None = None) -> int:
    """A run executed by the current runner; `usage` is what the CLI's result reported."""
    run = store.add_run("a-1", "x")
    result = None if usage is None else Event("result", usage=usage)
    agent = store.get_agent("a-1")
    stored = record_usage(store, run, agent, result, "claude-haiku-5-5", session)
    store.finish_run(run.id, RunStatus.DONE, cost_usd=stored.cost_usd if stored else None)
    return run.id


def old_code_run(store: Store, session_cost: float) -> int:
    """A run executed by a runner still on the old code: no usage row, session total."""
    run = store.add_run("a-1", "x")
    store.finish_run(run.id, RunStatus.DONE, cost_usd=session_cost)
    return run.id


def session_total(cost: float, tokens: int = 0) -> Usage:
    return Usage(tokens, tokens // 10, 0, 0, cost_usd=cost, cumulative=True)


def total_cost(store: Store) -> float:
    return Orchestrator(store).view(store.get_agent("a-1"))["cost_usd"]


def stored(store: Store, run_id: int) -> tuple:
    usage = store.get_usage(run_id)
    return usage.input_tokens, usage.output_tokens, usage.cost_usd


def test_a_session_total_after_old_code_runs_stores_only_the_new_share(store: Store) -> None:
    run_id = new_code_run(store, session_total(0.85))

    assert store.get_usage(run_id).cost_usd == 0.1
    assert store.get_run(run_id).cost_usd == 0.1
    assert total_cost(store) == 0.85


@pytest.mark.parametrize("cumulative", [False, True])
def test_an_old_code_run_between_new_runs_counts_once(store: Store, cumulative: bool) -> None:
    first = new_code_run(store, Usage(cost_usd=0.1))  # per-run, like model_usage_since_...
    old_code_run(store, 0.95)  # session total: 0.85 so far plus 0.10 of its own
    last = new_code_run(store, session_total(1.05) if cumulative else Usage(cost_usd=0.1))

    assert (store.get_usage(first).cost_usd, store.get_usage(last).cost_usd) == (0.1, 0.1)
    assert total_cost(store) == 1.05


def test_a_failed_turn_between_session_totals_lands_in_the_next_run(store: Store) -> None:
    without_old_runs(store)
    first = new_code_run(store, session_total(0.1, tokens=100))
    failed = new_code_run(store, None)  # stopped/crashed: no result, no usage
    last = new_code_run(store, session_total(0.3, tokens=250))

    view = Orchestrator(store).view(store.get_agent("a-1"))

    assert stored(store, first) == (100, 10, 0.1)
    assert store.get_usage(failed) is None
    assert stored(store, last) == (150, 15, 0.2)
    assert view["tokens"] == {"in": 250, "out": 25, "cache_read": 0, "cache_write": 0}
    assert view["cost_usd"] == 0.3


def test_a_restarted_session_never_stores_negative_values(store: Store) -> None:
    without_old_runs(store)
    first = new_code_run(store, session_total(0.5, tokens=500))
    restarted = new_code_run(store, session_total(0.05, tokens=40))

    assert stored(store, first) == (500, 50, 0.5)
    assert stored(store, restarted) == (40, 4, 0.05)
    assert total_cost(store) == 0.55


def reported(path: str, amount: int) -> Usage:
    """A session total as Codex (tokens, no cost) or Claude's fallback (cost only) reports it."""
    if path == "tokens":
        return Usage(amount, amount // 10, 0, 0, cumulative=True)
    return Usage(cost_usd=amount / 1000, cumulative=True)


def own(store: Store, run_id: int, path: str) -> int:
    usage = store.get_usage(run_id)
    return usage.input_tokens if path == "tokens" else round(usage.cost_usd * 1000)


@pytest.mark.parametrize("path", ["tokens", "cost"])
def test_runs_after_a_session_restart_count_only_their_new_session(store: Store, path: str) -> None:
    without_old_runs(store)
    runs = [
        new_code_run(store, reported(path, 500), "session-1"),
        new_code_run(store, reported(path, 40), "session-2"),  # started over
        new_code_run(store, reported(path, 90), "session-2"),
        new_code_run(store, reported(path, 100), "session-2"),
    ]

    view = Orchestrator(store).view(store.get_agent("a-1"))

    assert [own(store, run_id, path) for run_id in runs] == [500, 40, 50, 10]
    assert [store.get_usage(run_id).session_id for run_id in runs] == [
        "session-1", "session-2", "session-2", "session-2"
    ]  # fmt: skip
    if path == "tokens":
        assert view["tokens"]["in"] == 600 and view["cost_usd"] == 0
    else:
        assert view["tokens"] is None and view["cost_usd"] == 0.6


def test_the_total_spent_adds_up_each_agents_own_total(store: Store) -> None:
    """Old-code session totals count once per agent (a-1: 0.25 then 0.75 is 0.75), not
    across agents; a usage row whose run is gone (older code deleted it) is not counted."""
    store.add_agent(
        Agent(id="b-1", project="p", name="b", role="worker", provider="claude", cwd="/w")
    )
    old = store.add_run("b-1", "x")
    store.finish_run(old.id, RunStatus.DONE, cost_usd=0.5)
    new = store.add_run("b-1", "y")
    store.set_usage(new.id, "b-1", Usage(40, 4, 0, 0, cost_usd=0.125))
    store.set_usage(999, "gone", Usage(1000, 1000, 0, 0, cost_usd=9.0))
    orch = Orchestrator(store)
    agents = [orch.view(agent) for agent in store.list_agents()]

    assert sum(view["cost_usd"] for view in agents) == 1.375
    assert orch.total_spent() == {
        "cost_usd": 1.375,
        "tokens": {"in": 40, "out": 4, "cache_read": 0, "cache_write": 0},
    }


def test_the_stores_sums_give_what_walking_every_run_gives(store: Store) -> None:
    """usage_sums (summed in SQL, old-code agents walked) against models.spent over all
    runs, on a mixed history: usage records, old-code session totals between them, a
    compaction, a failed run, runs without a cost, a zero cost, an archived agent and a
    record whose run is gone."""
    for agent_id in ("b-1", "c-1", "d-1"):
        store.add_agent(
            Agent(id=agent_id, project="p", name=agent_id, role="worker", provider="claude",
                  cwd="/w")
        )  # fmt: skip

    def turn(agent_id: str, usage: Usage | None, cost: float | None, **options) -> None:
        status = options.pop("status", RunStatus.DONE)
        run = store.add_run(agent_id, "x", **options)
        if usage is not None:
            store.set_usage(run.id, agent_id, usage)
        store.finish_run(run.id, status, cost_usd=cost)

    turn("a-1", Usage(30, 3, 300, 30, cost_usd=0.1), 0.1)  # after the fixture's old runs
    turn("a-1", None, 1.3)  # old code again: a session total
    turn("b-1", Usage(10, 1, 100, 10, cost_usd=0.3), 0.3)
    turn("b-1", Usage(5, 1, 0, 0, cost_usd=0.07), 0.07, kind=COMPACT)
    turn("b-1", None, None, status=RunStatus.FAILED)  # no result, no usage
    turn("b-1", Usage(7, 2, 0, 0), None)  # tokens without a cost (Codex)
    turn("c-1", None, 0.0)  # a zero cost: not an old-code session total
    turn("c-1", Usage(1, 1, 1, 1, cost_usd=0.2), 0.2)
    turn("d-1", Usage(2, 2, 2, 2, cost_usd=0.4), 0.4)
    store.archive("d-1")
    store.set_usage(999, "gone", Usage(1000, 1000, 0, 0, cost_usd=9.0))
    every_run, every_record = [], {}
    for agent in store.list_agents():
        runs, _, usage, _ = store.snapshot(agent.id)
        every_run += runs
        every_record |= usage
    walked = spent(every_run, every_record)

    sums, old_code_runs, _ = store.usage_sums()

    assert len(sums) == 3 and {run.agent_id for run in old_code_runs} == {"a-1"}
    assert Orchestrator(store).total_spent() == {
        "cost_usd": walked.cost,
        "tokens": walked.tokens(),
    }
    assert walked.cost == 2.27  # a-1: 0.75 + 0.1 raised to 1.3; b-1: 0.37; c-1: 0.2; d-1: 0.4
    assert walked.tokens() == {"in": 55, "out": 10, "cache_read": 403, "cache_write": 43}


def test_nothing_spent_yet_is_zero_without_tokens(tmp_path: Path) -> None:
    assert Orchestrator(Store(tmp_path / "orch.db")).total_spent() == {
        "cost_usd": 0,
        "tokens": None,
    }


def test_a_run_usage_table_from_before_session_ids_is_upgraded(tmp_path: Path) -> None:
    path = tmp_path / "orch.db"
    with sqlite3.connect(path) as conn:
        conn.executescript(OLD_SCHEMA)
        conn.executescript(
            """
            CREATE TABLE run_usage (run_id INTEGER PRIMARY KEY, agent_id TEXT NOT NULL,
                input_tokens INTEGER, output_tokens INTEGER, cache_read_tokens INTEGER,
                cache_creation_tokens INTEGER, cost_usd REAL, model TEXT,
                created_at TEXT NOT NULL);
            INSERT INTO run_usage VALUES (1, 'a-1', 5, 1, 0, 0, NULL, 'm', 't');
            """
        )
    conn.close()
    script = "import sys; from pathlib import Path; from orchestrator.store import Store; Store(Path(sys.argv[1]))"
    procs = [
        subprocess.Popen([sys.executable, "-c", script, str(path)], stderr=subprocess.PIPE)
        for _ in range(6)
    ]
    errors = [proc.communicate()[1].decode() for proc in procs]

    store = Store(path)
    run_id = new_code_run(store, Usage(1, 1, 0, 0, cost_usd=0.1), "s")

    assert [proc.returncode for proc in procs] == [0] * 6, errors
    assert store.get_usage(1) == Usage(5, 1, 0, 0, model="m")
    assert store.get_usage(run_id).session_id == "s"


def test_switching_to_wal_gives_up_quickly_while_another_process_holds_the_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "busy.db"
    holder = sqlite3.connect(path, isolation_level=None)
    holder.execute("CREATE TABLE t (x)")
    holder.execute("BEGIN EXCLUSIVE")
    monkeypatch.setattr(store_module, "WAL_WAIT_SECONDS", 0.5)
    pending = Store.__new__(Store)
    pending.path = path

    started = time.monotonic()
    pending._use_wal()  # must neither raise nor wait for the busy timeout
    elapsed = time.monotonic() - started
    holder.execute("COMMIT")
    holder.close()

    assert elapsed < 2
    Store(path)
    with sqlite3.connect(path) as conn:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    conn.close()
