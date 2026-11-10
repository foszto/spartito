"""Per-run values and agent totals when CLIs report session totals and old-code runs mix in."""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

import pytest
from test_store import OLD_SCHEMA

from orchestrator import store as store_module
from orchestrator.models import Agent, Event, RunStatus, Usage
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


def new_code_run(store: Store, usage: Usage | None) -> int:
    """A run executed by the current runner; `usage` is what the CLI's result reported."""
    run = store.add_run("a-1", "x")
    result = None if usage is None else Event("result", usage=usage)
    stored = record_usage(store, run, store.get_agent("a-1"), result, "claude-haiku-5-5")
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
