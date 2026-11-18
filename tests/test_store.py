from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from orchestrator import board, decisions
from orchestrator import store as store_module
from orchestrator.models import Agent, Project, RunStatus, Usage
from orchestrator.service import Orchestrator
from orchestrator.store import Store, check_database, known_schema

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
    # Processes still running the old code build Run(**row) from SELECT *. Agents are read
    # with from_row since before the first release, which ignores a column it does not know.
    assert columns(old_db, "runs") == OLD_RUN_COLUMNS
    assert columns(old_db, "agents") == [*agent_columns, "provider_sessions", "capabilities"]


def test_an_old_database_has_no_session_kept_and_keeps_them_from_now_on(old_db: Path) -> None:
    store = Store(old_db)
    assert store.provider_sessions("a-1") == {}
    store.set_session("a-1", "s-claude")

    assert store.move_provider("a-1", "codex") == (True, None)
    assert store.provider_sessions("a-1") == {"claude": "s-claude"}
    store.set_session("a-1", "s-codex")
    assert store.move_provider("a-1", "claude") == (True, "s-claude")

    agent = store.get_agent("a-1")
    assert (agent.provider, agent.session_id) == ("claude", "s-claude")
    assert store.provider_sessions("a-1") == {"codex": "s-codex"}


@pytest.mark.parametrize("stored", [None, "", "not json", "[1]", '{"codex": 5}', '{"codex": ""}'])
def test_a_null_or_unreadable_kept_session_is_none(old_db: Path, stored: str | None) -> None:
    """NULL is what older code leaves (it never writes the column); anything else that is
    not a JSON object of session ids counts as nothing kept as well."""
    store = Store(old_db)
    with sqlite3.connect(old_db) as conn:
        conn.execute("UPDATE agents SET provider_sessions = ?", (stored,))
    conn.close()

    assert store.provider_sessions("a-1") == {}
    assert store.move_provider("a-1", "codex") == (True, None)
    assert store.get_agent("a-1").session_id is None


def test_an_old_database_has_no_capabilities_and_keeps_them_from_now_on(old_db: Path) -> None:
    store = Store(old_db)
    run = store.add_run("a-1", "three")

    assert store.get_agent("a-1").capabilities is None
    assert store.meta(run.id).capabilities is None
    store.set_agent_capabilities("a-1", {"chrome": True})
    store.set_run_capabilities(run.id, {"on": ["chrome"], "skipped": {}})
    assert json.loads(store.get_agent("a-1").capabilities) == {"chrome": True}
    assert json.loads(store.meta(run.id).capabilities) == {"on": ["chrome"], "skipped": {}}
    store.set_agent_capabilities("a-1", {})
    assert store.get_agent("a-1").capabilities is None
    # Code from before the columns inserts with named columns and reads with from_row.
    with sqlite3.connect(old_db) as conn:
        conn.execute(
            "INSERT INTO agents (id, project, name, role, provider, cwd, created_at)"
            " VALUES ('a-2', 'p', 'b', 'worker', 'claude', '/w', 't')"
        )
        conn.execute(
            "INSERT INTO run_meta (run_id, agent_id, kind, sender, notify)"
            " VALUES (99, 'a-2', 'message', NULL, 'each')"
        )
    conn.close()
    assert store.get_agent("a-2").capabilities is None
    assert store.meta(99).capabilities is None


def test_a_new_agent_stores_its_own_capabilities(old_db: Path) -> None:
    store = Store(old_db)
    agent = store.get_agent("a-1")
    agent.id, agent.capabilities = "a-3", json.dumps({"chrome": False})

    store.add_agent(agent)

    assert json.loads(store.get_agent("a-3").capabilities) == {"chrome": False}


def test_the_known_schema_has_the_capabilities_columns() -> None:
    known = known_schema()

    assert "capabilities" in known["agents"] and "capabilities" in known["run_meta"]


def test_a_busy_agent_is_not_moved_and_keeps_its_session(old_db: Path) -> None:
    store = Store(old_db)
    store.set_session("a-1", "s-claude")
    store.add_run("a-1", "queued")

    assert store.move_provider("a-1", "codex") == (False, None)
    agent = store.get_agent("a-1")
    assert (agent.provider, agent.session_id) == ("claude", "s-claude")
    assert store.provider_sessions("a-1") == {}


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
    assert "provider_sessions" in columns(path, "agents")
    assert columns(path, "agents")[-1] == columns(path, "run_meta")[-1] == "capabilities"
    assert store.get_agent("a-1").capabilities is None and store.meta(1).capabilities is None
    assert store.provider_sessions("a-1") == {}
    assert store.meta(1).auto == 0  # an old manual compaction stays manual
    assert store.meta(1).stopping == 0
    assert store.get_usage(1).first_context_tokens is None
    agent = store.get_agent("a-1")
    decision = decisions.record(store, agent, "Keep it?", ["yes"], None)
    item = board.add(store, "p", "An idea")
    assert decisions.listing(store) == [decision] and board.listing(store, "p") == [item]
    Store(path)  # opening it again changes nothing
    assert len(decisions.listing(store)) == len(board.listing(store, "p")) == 1


MCP_ROW = ("docs-db", "spartito", "stdio", {"command": "docs-db-mcp", "args": [], "env": {}}, None)


def test_an_old_database_gets_the_mcp_servers_table(old_db: Path) -> None:
    tables = {row[0] for row in sqlite3.connect(old_db).execute("SELECT name FROM sqlite_master")}
    assert "mcp_servers" not in tables

    store = Store(old_db)

    assert store.mcp_servers() == []
    row = store.add_mcp_server(*MCP_ROW)
    assert (row["name"], row["tools"], row["pin"]) == ("docs-db", None, None)
    assert columns(old_db, "mcp_servers") == [
        "name", "origin", "transport", "definition", "reaches", "tools", "pin",
        "created_at", "updated_at",
    ]  # fmt: skip
    assert columns(old_db, "agents")[-1] == "capabilities"  # nothing else changed for it
    Store(old_db)  # opening it again keeps the row
    assert [row["name"] for row in Store(old_db).mcp_servers()] == ["docs-db"]
    assert "mcp_servers" in known_schema()


def test_code_from_before_the_table_keeps_working_on_a_new_database(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Older code opens a database with the table (it creates only what it knows, and
    never reads this one) and works as before; the servers stay."""
    path = tmp_path / "new.db"
    new = Store(path)
    new.add_mcp_server(*MCP_ROW)
    start = store_module.SCHEMA.index("-- MCP servers")
    end = store_module.SCHEMA.index("\n);", start) + 3
    monkeypatch.setattr(
        store_module, "SCHEMA", store_module.SCHEMA[:start] + store_module.SCHEMA[end:]
    )

    old = Store(path)
    old.add_project(Project("p", "/repo", "main", "claude"))
    run = old.add_run(
        old.add_agent(
            Agent(id="a-1", project="p", name="a", role="worker", provider="claude", cwd="/w")
        ).id,
        "one",
    )
    old.set_settings("", {"capabilities": {"mcp:docs-db": True}})

    assert old.get_run(run.id).prompt == "one"
    assert old.get_settings("") == {"capabilities": {"mcp:docs-db": True}}
    assert [row["name"] for row in new.mcp_servers()] == ["docs-db"]
    assert check_database(path) == ["mcp_servers"]  # what older code would call unknown
