"""SQLite persistence.

The database is the only shared state: the CLI, the MCP server, the dashboard and
every detached runner open it independently, so each operation uses its own short
connection and WAL mode keeps readers and writers out of each other's way.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from . import config
from .models import ACTIVE, TOKEN_FIELDS, Agent, Event, Project, Run, RunStatus, Usage, from_row

SCHEMA = """
CREATE TABLE IF NOT EXISTS projects (
    name TEXT PRIMARY KEY,
    path TEXT NOT NULL,
    base_branch TEXT NOT NULL,
    default_provider TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS agents (
    id TEXT PRIMARY KEY,
    project TEXT REFERENCES projects(name),
    parent_id TEXT,
    name TEXT NOT NULL,
    role TEXT NOT NULL,
    provider TEXT NOT NULL,
    model TEXT,
    cwd TEXT NOT NULL,
    branch TEXT,
    base_ref TEXT,
    session_id TEXT,
    permission TEXT,
    created_at TEXT NOT NULL,
    effort TEXT
);
CREATE TABLE IF NOT EXISTS settings (
    scope TEXT PRIMARY KEY,  -- '' is global, otherwise a project name
    data TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_id TEXT NOT NULL REFERENCES agents(id),
    prompt TEXT NOT NULL,
    status TEXT NOT NULL,
    depends_on TEXT NOT NULL DEFAULT '[]',
    output_schema TEXT,
    result TEXT,
    structured TEXT,
    error TEXT,
    cost_usd REAL,
    pid INTEGER,
    created_at TEXT NOT NULL,
    started_at TEXT,
    ended_at TEXT
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL,
    agent_id TEXT NOT NULL,
    ts TEXT NOT NULL,
    kind TEXT NOT NULL,
    text TEXT NOT NULL
);
-- Added after the first release. Its own table, not columns on `runs`, because processes
-- still running older code build Run(**row) from `SELECT *` and would fail on a new column.
-- No foreign key either: older code deletes runs without knowing about this table.
CREATE TABLE IF NOT EXISTS run_usage (
    run_id INTEGER PRIMARY KEY,
    agent_id TEXT NOT NULL,
    input_tokens INTEGER,
    output_tokens INTEGER,
    cache_read_tokens INTEGER,
    cache_creation_tokens INTEGER,
    cost_usd REAL,
    model TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS runs_agent ON runs(agent_id, id);
CREATE INDEX IF NOT EXISTS events_agent ON events(agent_id, id);
"""

EVENT_TEXT_LIMIT = 4000
WAL_WAIT_SECONDS = 3.0


def now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def pid_alive(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


class Store:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or config.db_path()
        self._use_wal()
        with self._conn() as conn:
            conn.executescript(SCHEMA)
            columns = conn.execute("PRAGMA table_info(agents)").fetchall()
            if not any(column["name"] == "effort" for column in columns):
                conn.execute("ALTER TABLE agents ADD COLUMN effort TEXT")
        if any(column["name"] == "project" and column["notnull"] for column in columns):
            self._make_agent_project_nullable()

    def _use_wal(self) -> None:
        """Switch the database to WAL; the mode is stored in the file, so this is a no-op
        for a database that is in WAL already.

        Switching needs a moment in which no other connection uses the file, and SQLite
        may report "locked" at once instead of waiting, so it is retried with a short busy
        timeout for at most WAL_WAIT_SECONDS. If it still does not succeed the database
        stays usable in its current mode and the next open tries again.
        """
        deadline = time.monotonic() + WAL_WAIT_SECONDS
        conn = sqlite3.connect(self.path, timeout=0.2)
        try:
            while True:
                try:
                    if conn.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal":
                        return
                except sqlite3.OperationalError:
                    pass
                if time.monotonic() >= deadline:
                    return
                time.sleep(0.05)
        finally:
            conn.close()

    def _make_agent_project_nullable(self) -> None:
        """Databases created before the orchestrator agent existed required a project."""
        ddl = re.search(r"CREATE TABLE IF NOT EXISTS agents \((.*?)\n\);", SCHEMA, re.S).group(1)
        conn = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        try:
            conn.execute("PRAGMA foreign_keys=OFF")  # runs/events keep pointing at 'agents'
            conn.executescript(
                f"BEGIN IMMEDIATE; CREATE TABLE agents_new ({ddl});"
                " INSERT INTO agents_new SELECT * FROM agents;"
                " DROP TABLE agents; ALTER TABLE agents_new RENAME TO agents; COMMIT;"
            )
        finally:
            conn.close()

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    # -- projects ---------------------------------------------------------

    def add_project(self, project: Project) -> Project:
        project.created_at = now()
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO projects VALUES (?, ?, ?, ?, ?)",
                (
                    project.name,
                    project.path,
                    project.base_branch,
                    project.default_provider,
                    project.created_at,
                ),
            )
        return project

    def get_project(self, name: str) -> Project | None:
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM projects WHERE name = ?", (name,)).fetchone()
        return from_row(Project, row) if row else None

    def list_projects(self) -> list[Project]:
        with self._conn() as conn:
            rows = conn.execute("SELECT * FROM projects ORDER BY name").fetchall()
        return [from_row(Project, row) for row in rows]

    def delete_project(self, name: str) -> None:
        with self._conn() as conn:
            conn.execute("DELETE FROM settings WHERE scope = ?", (name,))
            conn.execute("DELETE FROM projects WHERE name = ?", (name,))

    # -- settings ---------------------------------------------------------

    def get_settings(self, scope: str) -> dict[str, Any]:
        with self._conn() as conn:
            row = conn.execute("SELECT data FROM settings WHERE scope = ?", (scope,)).fetchone()
        return json.loads(row["data"]) if row else {}

    def set_settings(self, scope: str, data: dict[str, Any]) -> None:
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO settings (scope, data) VALUES (?, ?)"
                " ON CONFLICT(scope) DO UPDATE SET data = excluded.data",
                (scope, json.dumps(data)),
            )

    # -- agents -----------------------------------------------------------

    def add_agent(self, agent: Agent) -> Agent:
        agent.created_at = now()
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO agents (id, project, parent_id, name, role, provider, model, cwd,"
                " branch, base_ref, session_id, permission, created_at, effort)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    agent.id,
                    agent.project,
                    agent.parent_id,
                    agent.name,
                    agent.role,
                    agent.provider,
                    agent.model,
                    agent.cwd,
                    agent.branch,
                    agent.base_ref,
                    agent.session_id,
                    agent.permission,
                    agent.created_at,
                    agent.effort,
                ),
            )
        return agent

    def get_agent(self, agent_id: str) -> Agent | None:
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM agents WHERE id = ?", (agent_id,)).fetchone()
        return from_row(Agent, row) if row else None

    def list_agents(self, project: str | None = None) -> list[Agent]:
        query, args = "SELECT * FROM agents", ()
        if project:
            query, args = query + " WHERE project = ?", (project,)
        with self._conn() as conn:
            rows = conn.execute(query + " ORDER BY created_at, id", args).fetchall()
        return [from_row(Agent, row) for row in rows]

    def set_session(self, agent_id: str, session_id: str) -> None:
        with self._conn() as conn:
            conn.execute("UPDATE agents SET session_id = ? WHERE id = ?", (session_id, agent_id))

    def delete_agent(self, agent_id: str) -> None:
        with self._conn() as conn:
            conn.execute("DELETE FROM events WHERE agent_id = ?", (agent_id,))
            conn.execute(
                "DELETE FROM run_usage WHERE run_id IN (SELECT id FROM runs WHERE agent_id = ?)",
                (agent_id,),
            )
            conn.execute("DELETE FROM runs WHERE agent_id = ?", (agent_id,))
            conn.execute("UPDATE agents SET parent_id = NULL WHERE parent_id = ?", (agent_id,))
            conn.execute("DELETE FROM agents WHERE id = ?", (agent_id,))

    # -- runs -------------------------------------------------------------

    @staticmethod
    def _run(row: sqlite3.Row) -> Run:
        data = dict(row)
        data["status"] = RunStatus(data["status"])
        data["depends_on"] = json.loads(data["depends_on"])
        for key in ("output_schema", "structured"):
            data[key] = json.loads(data[key]) if data[key] else None
        return from_row(Run, data)

    def add_run(
        self,
        agent_id: str,
        prompt: str,
        depends_on: list[int] | None = None,
        output_schema: dict[str, Any] | None = None,
    ) -> Run:
        with self._conn() as conn:
            cursor = conn.execute(
                "INSERT INTO runs (agent_id, prompt, status, depends_on, output_schema, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (
                    agent_id,
                    prompt,
                    RunStatus.WAITING.value,
                    json.dumps(depends_on or []),
                    json.dumps(output_schema) if output_schema else None,
                    now(),
                ),
            )
            run_id = cursor.lastrowid
        run = self.get_run(run_id)
        assert run is not None
        return run

    def get_run(self, run_id: int) -> Run | None:
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
        return self._run(row) if row else None

    def update_run(self, run_id: int, **fields: Any) -> None:
        if "status" in fields:
            fields["status"] = RunStatus(fields["status"]).value
        if "structured" in fields and fields["structured"] is not None:
            fields["structured"] = json.dumps(fields["structured"])
        assignments = ", ".join(f"{key} = ?" for key in fields)
        with self._conn() as conn:
            conn.execute(f"UPDATE runs SET {assignments} WHERE id = ?", (*fields.values(), run_id))

    def finish_run(self, run_id: int, status: RunStatus, **fields: Any) -> None:
        self.update_run(run_id, status=status, ended_at=now(), **fields)

    def runs_of(self, agent_id: str) -> list[Run]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM runs WHERE agent_id = ? ORDER BY id", (agent_id,)
            ).fetchall()
        return [self._run(row) for row in rows]

    def last_run(self, agent_id: str) -> Run | None:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM runs WHERE agent_id = ? ORDER BY id DESC LIMIT 1", (agent_id,)
            ).fetchone()
        return self._run(row) if row else None

    def active_runs(self, agent_id: str | None = None) -> list[Run]:
        query = f"SELECT * FROM runs WHERE status IN ('{ACTIVE[0]}', '{ACTIVE[1]}')"
        args: tuple[Any, ...] = ()
        if agent_id:
            query, args = query + " AND agent_id = ?", (agent_id,)
        with self._conn() as conn:
            rows = conn.execute(query + " ORDER BY id", args).fetchall()
        return [self._run(row) for row in rows]

    def reconcile(self) -> None:
        """Fail active runs whose runner process no longer exists (crash, reboot)."""
        for run in self.active_runs():
            if run.pid is not None and not pid_alive(run.pid):
                # Re-read: the runner may have finished between the query and the check.
                fresh = self.get_run(run.id)
                if fresh and fresh.status.active:
                    self.finish_run(run.id, RunStatus.FAILED, error="runner process died")

    # -- usage ------------------------------------------------------------

    def set_usage(self, run_id: int, agent_id: str, usage: Usage) -> None:
        with self._conn() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO run_usage (run_id, agent_id, input_tokens, output_tokens,"
                " cache_read_tokens, cache_creation_tokens, cost_usd, model, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    run_id,
                    agent_id,
                    *(getattr(usage, key) for key in TOKEN_FIELDS),
                    usage.cost_usd,
                    usage.model,
                    now(),
                ),
            )

    def usage_of(self, agent_id: str) -> dict[int, Usage]:
        """Per-run usage of an agent's runs, by run id. Runs without a record are absent."""
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT u.* FROM run_usage u JOIN runs r ON r.id = u.run_id"
                " WHERE r.agent_id = ? ORDER BY u.run_id",
                (agent_id,),
            ).fetchall()
        return {row["run_id"]: from_row(Usage, row) for row in rows}

    def get_usage(self, run_id: int) -> Usage | None:
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM run_usage WHERE run_id = ?", (run_id,)).fetchone()
        return from_row(Usage, row) if row else None

    # -- events -----------------------------------------------------------

    def add_event(self, run_id: int, agent_id: str, kind: str, text: str) -> None:
        if len(text) > EVENT_TEXT_LIMIT:
            text = text[:EVENT_TEXT_LIMIT] + f"… [{len(text) - EVENT_TEXT_LIMIT} more chars]"
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO events (run_id, agent_id, ts, kind, text) VALUES (?, ?, ?, ?, ?)",
                (run_id, agent_id, now(), kind, text),
            )

    def events(self, agent_id: str, after: int = 0, tail: int | None = None) -> list[Event]:
        with self._conn() as conn:
            if tail:
                rows = conn.execute(
                    "SELECT * FROM (SELECT * FROM events WHERE agent_id = ? AND id > ?"
                    " ORDER BY id DESC LIMIT ?) ORDER BY id",
                    (agent_id, after, tail),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM events WHERE agent_id = ? AND id > ? ORDER BY id",
                    (agent_id, after),
                ).fetchall()
        return [from_row(Event, row) for row in rows]
