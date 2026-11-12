from __future__ import annotations

import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from orchestrator import board, decisions
from orchestrator.models import RunStatus, Usage
from orchestrator.service import Orchestrator
from orchestrator.store import Store

# The schema as released before per-run usage existed, with one agent and one finished run.
OLD_SCHEMA = """
CREATE TABLE projects (name TEXT PRIMARY KEY, path TEXT NOT NULL, base_branch TEXT NOT NULL,
    default_provider TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE agents (id TEXT PRIMARY KEY, project TEXT REFERENCES projects(name), parent_id TEXT,
    name TEXT NOT NULL, role TEXT NOT NULL, provider TEXT NOT NULL, model TEXT, cwd TEXT NOT NULL,
    branch TEXT, base_ref TEXT, session_id TEXT, permission TEXT, created_at TEXT NOT NULL,
    effort TEXT);
CREATE TABLE settings (scope TEXT PRIMARY KEY, data TEXT NOT NULL);
CREATE TABLE runs (id INTEGER PRIMARY KEY AUTOINCREMENT, agent_id TEXT NOT NULL REFERENCES agents(id),
    prompt TEXT NOT NULL, status TEXT NOT NULL, depends_on TEXT NOT NULL DEFAULT '[]',
    output_schema TEXT, result TEXT, structured TEXT, error TEXT, cost_usd REAL, pid INTEGER,
    created_at TEXT NOT NULL, started_at TEXT, ended_at TEXT);
CREATE TABLE events (id INTEGER PRIMARY KEY AUTOINCREMENT, run_id INTEGER NOT NULL,
    agent_id TEXT NOT NULL, ts TEXT NOT NULL, kind TEXT NOT NULL, text TEXT NOT NULL);
CREATE INDEX runs_agent ON runs(agent_id, id);
CREATE INDEX events_agent ON events(agent_id, id);
INSERT INTO projects VALUES ('p', '/repo', 'main', 'claude', 't');
INSERT INTO agents (id, project, name, role, provider, cwd, created_at)
    VALUES ('a-1', 'p', 'a', 'worker', 'claude', '/w', 't');
INSERT INTO runs (agent_id, prompt, status, cost_usd, created_at)
    VALUES ('a-1', 'one', 'done', 0.25, 't'), ('a-1', 'two', 'done', 0.75, 't');
"""
OLD_RUN_COLUMNS = [
    "id", "agent_id", "prompt", "status", "depends_on", "output_schema", "result", "structured",
    "error", "cost_usd", "pid", "created_at", "started_at", "ended_at",
]  # fmt: skip


@pytest.fixture(params=["wal", "delete"])
def old_db(tmp_path: Path, request: pytest.FixtureRequest) -> Path:
    """Live databases are in WAL mode; a database never opened by Store is not."""
    path = tmp_path / "old.db"
    with sqlite3.connect(path) as conn:
        conn.execute(f"PRAGMA journal_mode={request.param}")
        conn.executescript(OLD_SCHEMA)
    conn.close()
    return path


def columns(path: Path, table: str) -> list[str]:
    with sqlite3.connect(path) as conn:
        names = [row[1] for row in conn.execute(f"PRAGMA table_info({table})")]
    conn.close()
    return names


def test_an_old_database_gets_usage_without_changing_the_existing_tables(old_db: Path) -> None:
    agent_columns = columns(old_db, "agents")

    store = Store(old_db)
    old_runs = store.runs_of("a-1")
    new_run = store.add_run("a-1", "three")
    store.set_usage(new_run.id, "a-1", Usage(10, 2, 30, 4, cost_usd=0.5, model="m"))

    assert [store.get_usage(run.id) for run in old_runs] == [None, None]
    assert store.get_usage(new_run.id) == Usage(10, 2, 30, 4, cost_usd=0.5, model="m")
    assert list(store.usage_of("a-1")) == [new_run.id]
    # Processes still running the old code build Run(**row) from SELECT *.
    assert columns(old_db, "runs") == OLD_RUN_COLUMNS
    assert columns(old_db, "agents") == agent_columns


def test_legacy_runs_count_their_session_running_total_once(old_db: Path) -> None:
    store = Store(old_db)
    run = store.add_run("a-1", "three")
    store.finish_run(run.id, RunStatus.DONE, cost_usd=0.1)
    store.set_usage(run.id, "a-1", Usage(1, 1, 0, 0, cost_usd=0.1))

    view = Orchestrator(store).view(store.get_agent("a-1"), detail=True)

    # 0.25 then 0.75 were Claude's session totals: 0.75 was spent before, plus 0.1 now.
    assert view["cost_usd"] == 0.85
    assert view["tokens"]["in"] == 1
    assert view["last_run"]["usage"]["cost_usd"] == 0.1


def test_an_old_database_without_usage_reports_none(old_db: Path) -> None:
    store = Store(old_db)

    view = Orchestrator(store).view(store.get_agent("a-1"), detail=True)

    assert view["last_run"]["usage"] == {"tokens": None, "cost_usd": None, "model": None}
    assert view["tokens"] is None and view["cost_usd"] == 0.75


def test_several_processes_may_open_an_old_database_at_once(old_db: Path) -> None:
    script = "import sys; from pathlib import Path; from orchestrator.store import Store; Store(Path(sys.argv[1]))"
    procs = [
        subprocess.Popen([sys.executable, "-c", script, str(old_db)], stderr=subprocess.PIPE)
        for _ in range(6)
    ]
    errors = [proc.communicate()[1].decode() for proc in procs]

    Store(old_db)  # and once more after the upgrade

    assert [proc.returncode for proc in procs] == [0] * 6, errors
    assert {"run_usage", "deliveries"} <= {
        row[0] for row in sqlite3.connect(old_db).execute("SELECT name FROM sqlite_master")
    }


def test_rows_with_columns_from_a_newer_version_still_load(old_db: Path) -> None:
    store = Store(old_db)
    with sqlite3.connect(old_db) as conn:
        for table in ("projects", "agents", "runs", "events", "run_usage"):
            conn.execute(f"ALTER TABLE {table} ADD COLUMN future TEXT")
    conn.close()
    run = store.add_run("a-1", "x")
    store.add_event(run.id, "a-1", "info", "hello")
    store.set_usage(run.id, "a-1", Usage(1, 1, 1, 1))

    assert store.get_project("p").name == "p"
    assert store.get_agent("a-1").id == "a-1"
    assert store.get_run(run.id).prompt == "x"
    assert store.events("a-1")[0].text == "hello"
    assert store.get_usage(run.id).input_tokens == 1


def test_removing_an_agent_removes_its_usage(old_db: Path) -> None:
    store = Store(old_db)
    run = store.add_run("a-1", "x")
    store.set_usage(run.id, "a-1", Usage(1, 1, 1, 1))

    store.delete_agent("a-1")

    assert store.get_usage(run.id) is None


MASTER_ERA_SCHEMA = Path(__file__).parent / "fixtures" / "master_era_schema.sql"


def test_a_master_era_database_gets_every_new_table_and_column(tmp_path: Path) -> None:
    path = tmp_path / "master.db"
    with sqlite3.connect(path) as conn:
        conn.execute("PRAGMA journal_mode=wal")
        conn.executescript(MASTER_ERA_SCHEMA.read_text())
        conn.execute("INSERT INTO projects VALUES ('p', '/tmp/p', 'main', 'fake', 't')")
        conn.execute(
            "INSERT INTO agents (id, project, name, role, provider, cwd, created_at)"
            " VALUES ('a-1', 'p', 'a', 'worker', 'fake', '/tmp/p', 't')"
        )
        conn.execute(
            "INSERT INTO runs (agent_id, prompt, status, created_at, started_at, ended_at)"
            " VALUES ('a-1', 'old', 'done', 't', 't', 't')"
        )
        conn.execute("INSERT INTO run_meta (run_id, agent_id, kind) VALUES (1, 'a-1', 'compact')")
        conn.execute("INSERT INTO run_usage (run_id, agent_id, created_at) VALUES (1, 'a-1', 't')")
    conn.close()
    assert "auto" not in columns(path, "run_meta")

    store = Store(path)

    tables = {row[0] for row in sqlite3.connect(path).execute("SELECT name FROM sqlite_master")}
    assert {"decisions", "board_items"} <= tables
    assert {"auto", "stopping"} <= set(columns(path, "run_meta"))
    assert "first_context_tokens" in columns(path, "run_usage")
    assert store.meta(1).auto == 0  # an old manual compaction stays manual
    assert store.meta(1).stopping == 0
    assert store.get_usage(1).first_context_tokens is None
    agent = store.get_agent("a-1")
    decision = decisions.record(store, agent, "Keep it?", ["yes"], None)
    item = board.add(store, "p", "An idea")
    assert decisions.listing(store) == [decision] and board.listing(store, "p") == [item]
    Store(path)  # opening it again changes nothing
    assert len(decisions.listing(store)) == len(board.listing(store, "p")) == 1
