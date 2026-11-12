"""SQLite persistence.

The database is the only shared state: the CLI, the MCP server, the dashboard and
every detached runner open it independently, so each operation uses its own short
connection and WAL mode keeps readers and writers out of each other's way.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import sqlite3
import time
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from . import config
from .models import (
    ACTIVE,
    COMPACT,
    CONTEXT_FIELDS,
    IN_ORDER,
    MESSAGE,
    NOTIFICATION,
    NOTIFY_MODES,
    TOKEN_FIELDS,
    Agent,
    Event,
    Project,
    Run,
    RunStatus,
    Usage,
    from_row,
    running_total,
)

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
    created_at TEXT NOT NULL,
    session_id TEXT
);
-- Which run results wait_for_agents already returned to which caller ('' is `spartito chat`),
-- so a later wait does not repeat them. Its own table for the same reasons as run_usage.
CREATE TABLE IF NOT EXISTS deliveries (
    caller TEXT NOT NULL,
    run_id INTEGER NOT NULL,
    delivered_at TEXT NOT NULL,
    PRIMARY KEY (caller, run_id)
);
-- What kind of turn a run is and who queued it; its own table for the same reasons as
-- run_usage. Runs without a row (queued by older code) are messages of unknown sender.
-- kind: 'message' | 'notification' | 'compact' (a compaction turn, see Orchestrator.compact,
-- queued in order with messages but never an answer to anyone); sender: the agent whose MCP server queued the run
-- (NULL: user, CLI, dashboard); notify: 'each' | 'all', how the sender wants to be woken;
-- origin: whom the turn answers (a message: its sender; a notification: the parent, when
-- the work it reports was done for that parent); cause_run: the run of the parent during
-- whose turn this one was queued (a notification: that of the work it reports);
-- claimed: JSON list of the runs a notification reported (released if its turn fails);
-- checked: 1 once the run has ended and its parents were checked for a notification;
-- auto: 1 for a compaction queued automatically (Orchestrator.auto_compact), which goes
-- ahead of the agent's queued messages (older code ignores it and keeps queue order);
-- stopping: 1 once a stop signalled the run's runner (launcher.stop_runs), so that whoever
-- finds the runner dead treats the run as stopped, not as crashed (older code ignores it).
CREATE TABLE IF NOT EXISTS run_meta (
    run_id INTEGER PRIMARY KEY,
    agent_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    sender TEXT,
    notify TEXT NOT NULL DEFAULT 'each',
    origin TEXT,
    cause_run INTEGER,
    claimed TEXT,
    checked INTEGER NOT NULL DEFAULT 0,
    auto INTEGER NOT NULL DEFAULT 0,
    stopping INTEGER NOT NULL DEFAULT 0
);
-- Archived agents: hidden from the default listings, everything else kept. Its own table
-- for the same reasons as run_usage. A row whose restored_at is set is no longer archived
-- (restored_at counts as activity for the automatic archiving); how: 'manual' | 'auto'.
CREATE TABLE IF NOT EXISTS archived_agents (
    agent_id TEXT PRIMARY KEY,
    archived_at TEXT NOT NULL,
    how TEXT NOT NULL,
    restored_at TEXT
);
-- Decisions an agent asks the owner for (decisions.py); a table of its own, unknown to older
-- code. project: NULL for the orchestrator; options: JSON list of short strings;
-- state: 'open' | 'answered' | 'withdrawn'; run_id: the message that delivered the answer;
-- note: why it was closed without an answer (e.g. the agent was removed).
CREATE TABLE IF NOT EXISTS decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_id TEXT NOT NULL,
    project TEXT,
    question TEXT NOT NULL,
    options TEXT NOT NULL DEFAULT '[]',
    recommendation TEXT,
    state TEXT NOT NULL DEFAULT 'open',
    answer TEXT,
    created_at TEXT NOT NULL,
    closed_at TEXT,
    run_id INTEGER,
    note TEXT
);
CREATE INDEX IF NOT EXISTS decisions_state ON decisions(state, id);
-- The task board of each project (board.py); a table of its own, unknown to older code.
-- state: 'idea' | 'ready' | 'in_progress' | 'done'; position: order within one state of one
-- project; agent_id, branch: optional links (plain text, no foreign key).
CREATE TABLE IF NOT EXISTS board_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project TEXT NOT NULL,
    title TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    state TEXT NOT NULL DEFAULT 'idea',
    position INTEGER NOT NULL DEFAULT 0,
    agent_id TEXT,
    branch TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS board_items_order ON board_items(project, state, position);
-- Desktop notifications as records (desktop.show), so an open dashboard page can show them
-- with a click action; a table of its own, unknown to older code. kind: 'decision' |
-- 'finished'; agent_id: whom it is about (a decision: the requesting agent); decision_id:
-- NULL for 'finished'; claimed_at: set once a page or osascript showed it (the first claim
-- wins). Rows older than a day are pruned when one is added.
CREATE TABLE IF NOT EXISTS notices (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    kind TEXT NOT NULL,
    agent_id TEXT NOT NULL,
    decision_id INTEGER,
    text TEXT NOT NULL,
    claimed_at TEXT
);
-- How each notice went (desktop.py: the trace), one row per step, written by whichever process
-- took the step; a table of its own, unknown to older code (its notices have no steps). step:
-- 'record' | 'route' | 'escalate' | 'claim' | 'shown' | 'counted' | 'leave' | 'release' |
-- 'fallback' | 'drop';
-- path: the way it concerns ('window-system', 'launcher-helper', 'osascript', 'waiting', ...);
-- actor: the process, '<role>:<pid>'; detail: the reason, in words. Pruned with the notices.
CREATE TABLE IF NOT EXISTS notice_steps (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    notice_id INTEGER NOT NULL,
    at TEXT NOT NULL,
    step TEXT NOT NULL,
    path TEXT,
    actor TEXT NOT NULL,
    detail TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS notice_steps_notice ON notice_steps(notice_id, id);
CREATE INDEX IF NOT EXISTS runs_agent ON runs(agent_id, id);
-- Covers what Store.usage_sums reads of every run (agent_id, cost_usd and the id), so the
-- dashboard's total never reads the runs' text.
CREATE INDEX IF NOT EXISTS runs_spend ON runs(agent_id, cost_usd);
-- What the agents deleted for good (Store.delete_agent) had spent: their sums as the total
-- counted them (models.running_total), written in the deletion's transaction, so the total
-- (Store.usage_sums) keeps them. Older code neither writes nor reads it. project, agent_id
-- and removed_at are kept for a later per-project view; nothing reads them yet.
CREATE TABLE IF NOT EXISTS spent_removed (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_id TEXT NOT NULL,
    project TEXT,
    input_tokens INTEGER,
    output_tokens INTEGER,
    cache_read_tokens INTEGER,
    cache_creation_tokens INTEGER,
    cost_usd REAL,
    removed_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS events_agent ON events(agent_id, id);
-- What happened to each board item (board.py), written in the same transaction as the
-- change; a table of its own, unknown to older code (a change it makes is a gap, shown as
-- unrecorded). actor_kind: 'owner' | 'agent' | 'auto' (NULL for an 'unrecorded' gap); actor:
-- the agent id or the rule (NULL: the owner, or the interactive session); action: add | edit
-- | state | move | place | dispatch | restore | unrecorded (a change by such code, found at
-- the next recorded one; ts: its time); changes: JSON {field: [old, new]} (the description
-- only "changed"); run_id: the dispatch's run, or the acting agent's running turn.
CREATE TABLE IF NOT EXISTS board_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id INTEGER NOT NULL,
    ts TEXT NOT NULL,
    actor_kind TEXT,
    actor TEXT,
    action TEXT NOT NULL,
    changes TEXT NOT NULL DEFAULT '{}',
    run_id INTEGER
);
CREATE INDEX IF NOT EXISTS board_history_item ON board_history(item_id, id);
-- The last known git state of a board item's branch (core/board_git.py); a cache, a table of
-- its own, unknown to older code. A row whose branch or base is not the item's current one is
-- stale. ahead_tip: the branch's tip at the last check that saw it ahead of `base` (NULL again
-- once its work is found not to be in the base); ahead_in_base: 1 once its work is found in
-- the base; done_tip: the ahead_tip whose merge was acted on (the item moved to done) or
-- noted; merged: 0 | 1, as git.branch_state tells it; error: why the last check failed (the
-- rest is from before).
CREATE TABLE IF NOT EXISTS board_git (
    item_id INTEGER PRIMARY KEY,
    branch TEXT NOT NULL,
    base TEXT NOT NULL,
    tip TEXT,
    ahead INTEGER,
    behind INTEGER,
    dirty INTEGER,
    ahead_tip TEXT,
    ahead_in_base INTEGER NOT NULL DEFAULT 0,
    done_tip TEXT,
    merged INTEGER NOT NULL DEFAULT 0,
    error TEXT,
    checked_at TEXT NOT NULL
);
-- The conversation message a board item was made from (board.py; one per item): the agent,
-- its event and that event's run. A table of its own, unknown to older code.
CREATE TABLE IF NOT EXISTS board_sources (
    item_id INTEGER PRIMARY KEY,
    agent_id TEXT NOT NULL,
    event_id INTEGER NOT NULL,
    run_id INTEGER
);
-- Decisions linked to the board items they block (board.py). A table of its own, unknown to
-- older code; no foreign keys (items and decisions are never deleted).
CREATE TABLE IF NOT EXISTS board_blockers (
    item_id INTEGER NOT NULL,
    decision_id INTEGER NOT NULL,
    linked_at TEXT NOT NULL,
    PRIMARY KEY (item_id, decision_id)
);
CREATE INDEX IF NOT EXISTS board_blockers_decision ON board_blockers(decision_id);
"""

ANYONE = object()  # withdraw_queued/replace_queued: whoever queued the run

# run_meta columns added after its first version (run_id, agent_id, kind, sender, notify).
RUN_META_ADDED = (
    ("origin", "TEXT"),
    ("cause_run", "INTEGER"),
    ("claimed", "TEXT"),
    ("checked", "INTEGER NOT NULL DEFAULT 0"),
    ("auto", "INTEGER NOT NULL DEFAULT 0"),
    ("stopping", "INTEGER NOT NULL DEFAULT 0"),
)
# notices columns added after its first version (desktop.py). route: whom the notice is left
# to ('window', 'helper', 'page', 'banner', 'waiting'; 'osascript' or 'linux' when shown at
# once, 'quiet' when the environment says nothing is shown; both claimed at once; NULL:
# recorded by older code, anyone may take it); claimed_by: '<role>:<pid>' of the
# claimer (NULL: older code). Older code inserts and claims with named columns, unaffected.
NOTICES_ADDED = (("route", "TEXT"), ("claimed_by", "TEXT"))
# run_usage columns added after its first version.
RUN_USAGE_ADDED = (
    ("session_id", "TEXT"),
    ("context_tokens", "INTEGER"),
    ("context_window", "INTEGER"),
    ("compact_trigger", "TEXT"),
    ("compact_pre_tokens", "INTEGER"),
    ("compact_post_tokens", "INTEGER"),
    ("first_context_tokens", "INTEGER"),
)
ARCHIVE_MANUAL, ARCHIVE_AUTO = "manual", "auto"
RESTORE_SQL = (
    "UPDATE archived_agents SET restored_at = ? WHERE agent_id = ? AND restored_at IS NULL"
)
NOTICE_KEEP = timedelta(days=1)
LOST_TRIGGER_SECONDS = 120  # an ended run still unchecked this long lost its trigger

EVENT_TEXT_LIMIT = 4000
WAL_WAIT_SECONDS = 3.0
# The tables every state database has had since the first release (tests/test_store.py:
# OLD_SCHEMA): a file without them is not one. The newer ones are added when it is opened.
STATE_TABLES = ("projects", "agents", "settings", "runs", "events")


def now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _schema(conn: sqlite3.Connection) -> dict[str, set[str]]:
    """The tables of a database and their columns."""
    tables = [
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
        )
    ]
    quoted = {table: table.replace('"', '""') for table in tables}
    return {
        table: {row[1] for row in conn.execute(f'PRAGMA table_info("{quoted[table]}")')}
        for table in tables
    }


def known_schema() -> dict[str, set[str]]:
    """The tables and columns this version creates: SCHEMA and the *_ADDED lists."""
    conn = sqlite3.connect(":memory:")
    try:
        conn.executescript(SCHEMA)
        added = (("run_meta", RUN_META_ADDED), ("run_usage", RUN_USAGE_ADDED))
        for table, columns in (*added, ("notices", NOTICES_ADDED)):
            present = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
            for name, ddl in columns:
                if name not in present:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")
        return _schema(conn)
    finally:
        conn.close()


def check_database(path: Path) -> list[str]:
    """Raise ValueError unless the file `path` is an intact state database: readable as
    SQLite, `PRAGMA integrity_check` ok, with the tables of STATE_TABLES. Else return the
    tables and `table.column`s it has that this version does not know (a newer version
    wrote it). Opening it may fold a -wal file beside it into it, as any open does."""
    if not path.is_file():
        raise ValueError(f"{path} is not a file")
    try:
        conn = sqlite3.connect(path, timeout=30)
        try:
            result = [row[0] for row in conn.execute("PRAGMA integrity_check")]
            schema = _schema(conn)
        finally:
            conn.close()
    except sqlite3.DatabaseError as exc:
        raise ValueError(f"{path} is not a readable SQLite database ({exc})") from None
    if result != ["ok"]:
        raise ValueError(f"{path} fails the integrity check: {'; '.join(result[:5])}")
    if missing := [table for table in STATE_TABLES if table not in schema]:
        raise ValueError(f"{path} is not a state database (no table {', '.join(missing)})")
    known = known_schema()
    unknown: list[str] = []
    for table, columns in sorted(schema.items()):
        if table not in known:
            unknown.append(table)
        else:
            unknown += [f"{table}.{column}" for column in sorted(columns - known[table])]
    return unknown


def _settle(conn: sqlite3.Connection, note: str, run_floor: int) -> None:
    """Prepare a restored database (Store.replace_from): its queued and running turns end as
    stopped with `note`, every ended run counts as checked (its parents are not woken), and
    run ids continue above `run_floor`. Works on the schema of any version."""
    with conn:
        conn.execute(
            f"UPDATE runs SET status = ?, error = ?, ended_at = ?"
            f" WHERE status IN ('{ACTIVE[0]}', '{ACTIVE[1]}')",
            (RunStatus.STOPPED.value, note, now()),
        )
        if "checked" in _schema(conn).get("run_meta", set()):
            conn.execute("UPDATE run_meta SET checked = 1 WHERE checked = 0")
        raised = conn.execute(
            "UPDATE sqlite_sequence SET seq = MAX(seq, ?) WHERE name = 'runs'", (run_floor,)
        ).rowcount
        if not raised and run_floor:
            conn.execute("INSERT INTO sqlite_sequence (name, seq) VALUES ('runs', ?)", (run_floor,))


def _older_than(timestamp: str | None, seconds: float) -> bool:
    """Whether a stored timestamp lies more than `seconds` back (a live runner marks its
    run checked within seconds; a pid that still answers may belong to another process)."""
    if not timestamp:
        return False
    try:
        then = datetime.fromisoformat(timestamp)
    except ValueError:
        return False
    if then.tzinfo is None:
        then = then.replace(tzinfo=UTC)
    return (datetime.now(UTC) - then).total_seconds() > seconds


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


@dataclass(frozen=True)
class NoticeStep:
    """One step of a notice's way (table notice_steps; desktop.py writes them)."""

    step: str
    path: str | None
    actor: str
    detail: str = ""
    at: str | None = None  # None: when it is written


@dataclass
class RunMeta:
    """A run's row in run_meta; runs without one get `RunMeta.legacy()`."""

    kind: str = MESSAGE
    sender: str | None = None
    notify: str = "each"
    origin: str | None = None
    cause_run: int | None = None
    claimed: str | None = None  # JSON list of run ids
    checked: int = 0
    auto: int = 0  # 1: an automatic compaction
    stopping: int = 0  # 1: a stop signalled its runner
    known: bool = True  # False: queued by code from before run metadata

    @classmethod
    def legacy(cls) -> RunMeta:
        return cls(known=False)


def subtree_sql(column: str = "?") -> str:
    """A recursive CTE `tree(id)`: the agent `column` and all its descendants."""
    return (
        f"WITH RECURSIVE tree(id) AS (SELECT {column} UNION"
        " SELECT a.id FROM agents a JOIN tree t ON a.parent_id = t.id)"
    )


class Store:
    # Called as on_run_end(run_id, agent_id, recheck) when reconcile ended a run (or
    # deleted a notification that never started: recheck, the agent may still be due one,
    # unless a stop ended it)
    # or found an ended run whose runner died before checking the parents. The
    # Orchestrator (a runner: core/lifecycle.attach) sets it so parents get notified.
    on_run_end: Callable[[int, str, bool], None] | None = None

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or config.db_path()
        self._use_wal()
        with self._conn() as conn:
            conn.executescript(SCHEMA)
            columns = conn.execute("PRAGMA table_info(agents)").fetchall()
            if not any(column["name"] == "effort" for column in columns):
                conn.execute("ALTER TABLE agents ADD COLUMN effort TEXT")
            for table, added in (("run_usage", RUN_USAGE_ADDED), ("notices", NOTICES_ADDED)):
                present = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
                for name, ddl in added:
                    if name in present:
                        continue
                    try:  # tables from before the column was recorded
                        conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")
                    except sqlite3.OperationalError as exc:  # another process was quicker
                        if "duplicate column" not in str(exc):
                            raise
        self._complete_run_meta()
        with self._conn() as conn:
            conn.execute("CREATE INDEX IF NOT EXISTS run_meta_unchecked ON run_meta(checked)")
        if any(column["name"] == "project" and column["notnull"] for column in columns):
            self._make_agent_project_nullable()

    def _complete_run_meta(self) -> None:
        """Add the columns a first-version run_meta lacks, under the write lock so that
        concurrent openers add them once. Rows that exist when `checked` is added count as
        checked: their runs ended long ago, and checking them now would wake parents."""
        with self.transaction() as conn:
            present = {row["name"] for row in conn.execute("PRAGMA table_info(run_meta)")}
            missing = [(name, ddl) for name, ddl in RUN_META_ADDED if name not in present]
            for name, ddl in missing:
                try:
                    conn.execute(f"ALTER TABLE run_meta ADD COLUMN {name} {ddl}")
                except sqlite3.OperationalError as exc:  # another process was quicker
                    if "duplicate column" not in str(exc):
                        raise
            if any(name == "checked" for name, _ in missing):
                conn.execute("UPDATE run_meta SET checked = 1")

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
    def _conn(self, wait: float = 30) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path, timeout=wait)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def connection(self) -> contextlib.AbstractContextManager[sqlite3.Connection]:
        """A short connection whose work is committed when the block ends."""
        return self._conn()

    @contextmanager
    def transaction(self, wait: float = 30) -> Iterator[sqlite3.Connection]:
        """One connection holding the write lock from the first statement on, so checks
        and the writes based on them are atomic across processes. Use only this
        connection inside: another one that writes would wait for this lock. `wait`:
        seconds to wait for the lock (then sqlite3.OperationalError)."""
        conn = sqlite3.connect(self.path, timeout=wait, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        try:
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
            except BaseException:
                conn.execute("ROLLBACK")
                raise
            conn.execute("COMMIT")
        finally:
            conn.close()

    @contextmanager
    def _read(self) -> Iterator[sqlite3.Connection]:
        """One connection whose reads all see the database as it was at the first one (a
        read transaction; it takes no write lock)."""
        conn = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        try:
            conn.execute("BEGIN")
            try:
                yield conn
            except BaseException:
                conn.execute("ROLLBACK")
                raise
            conn.execute("COMMIT")
        finally:
            conn.close()

    # -- backup and restore (core/state.py) -------------------------------

    def backup(self, dest: Path) -> None:
        """Copy the database into the file `dest` (new, or empty) with SQLite's online backup
        API: one consistent snapshot, taken in one step under a read transaction, so it is
        safe while other processes write; never a copy of the file. The copy is in
        rollback-journal mode: a single file, with no -wal beside it."""
        source = sqlite3.connect(self.path, timeout=30)
        try:
            target = sqlite3.connect(dest)
            try:
                source.backup(target)
                target.execute("PRAGMA journal_mode=DELETE")
            finally:
                target.close()
        finally:
            source.close()

    def replace_from(
        self, source: Path, note: str, before: Callable[[], None] = lambda: None
    ) -> None:
        """Make this database's content that of the database file `source`, in place with
        the backup API: no file is swapped, so the connections other processes keep (a
        dashboard) see the new content with their next transaction, and the database stays
        in WAL. `source` is first read into memory and settled there (_settle: its queued and
        running turns end as stopped with `note`, as their runners are gone; nothing wakes a
        parent; run ids continue above the ones this database used, so a turn never reuses
        an earlier turn's logs/run-<id>). `before` runs right before the copy: raising there
        leaves the database as it was. The tables and columns `source` lacks (an older
        version's) are added by the next open (Store())."""
        with self._conn() as conn:
            used = conn.execute(
                "SELECT MAX(COALESCE((SELECT MAX(id) FROM runs), 0),"
                " COALESCE((SELECT MAX(seq) FROM sqlite_sequence WHERE name = 'runs'), 0))"
            ).fetchone()[0]
        memory = sqlite3.connect(":memory:")
        try:
            disk = sqlite3.connect(source, timeout=30)
            try:
                disk.backup(memory)
            finally:
                disk.close()
            _settle(memory, note, used)
            before()
            target = sqlite3.connect(self.path, timeout=30)
            try:
                memory.backup(target)
            finally:
                target.close()
        finally:
            memory.close()

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

    def set_overrides(self, agent_id: str, changes: dict[str, str | None]) -> None:
        """Set the agent's own `model` and/or `effort` (None: inherit from the settings).
        Only the given columns are written, so two changes at once do not undo each other."""
        columns = [key for key in ("model", "effort") if key in changes]
        if not columns:
            return
        assignments = ", ".join(f"{key} = ?" for key in columns)
        with self._conn() as conn:
            conn.execute(
                f"UPDATE agents SET {assignments} WHERE id = ?",
                (*(changes[key] for key in columns), agent_id),
            )

    def delete_agent(self, agent_id: str) -> None:
        """Delete the agent with its runs; what they spent stays in spent_removed (one
        transaction: all of it or nothing)."""
        with self.transaction() as conn:
            self._keep_spent(conn, agent_id)
            conn.execute("DELETE FROM events WHERE agent_id = ?", (agent_id,))
            for table in ("run_usage", "run_meta"):
                conn.execute(
                    f"DELETE FROM {table} WHERE run_id IN (SELECT id FROM runs WHERE agent_id = ?)",
                    (agent_id,),
                )
            conn.execute(
                "DELETE FROM deliveries WHERE caller = ?"
                " OR run_id IN (SELECT id FROM runs WHERE agent_id = ?)",
                (agent_id, agent_id),
            )
            conn.execute("DELETE FROM runs WHERE agent_id = ?", (agent_id,))
            conn.execute("DELETE FROM archived_agents WHERE agent_id = ?", (agent_id,))
            conn.execute("UPDATE agents SET parent_id = NULL WHERE parent_id = ?", (agent_id,))
            conn.execute("DELETE FROM agents WHERE id = ?", (agent_id,))

    @staticmethod
    def _keep_spent(conn: sqlite3.Connection, agent_id: str) -> None:
        """Record what the agent's runs spent (models.running_total, as the total counts
        it) in spent_removed, unless they spent nothing."""
        runs = conn.execute(
            "SELECT id, agent_id, cost_usd FROM runs WHERE agent_id = ?", (agent_id,)
        ).fetchall()
        records = conn.execute(
            "SELECT u.* FROM run_usage u JOIN runs r ON r.id = u.run_id WHERE r.agent_id = ?",
            (agent_id,),
        ).fetchall()
        spent = running_total(
            [Run(row["id"], row["agent_id"], "", cost_usd=row["cost_usd"]) for row in runs],
            {row["run_id"]: from_row(Usage, row) for row in records},
        )
        sums = [getattr(spent, key) for key in (*TOKEN_FIELDS, "cost_usd")]
        if all(value is None for value in sums):
            return
        row = conn.execute("SELECT project FROM agents WHERE id = ?", (agent_id,)).fetchone()
        conn.execute(
            f"INSERT INTO spent_removed (agent_id, project, {', '.join(TOKEN_FIELDS)},"
            " cost_usd, removed_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (agent_id, row["project"] if row else None, *sums, now()),
        )

    # -- archive ----------------------------------------------------------

    def archived(self, project: str | None = None) -> dict[str, dict[str, str]]:
        """The archived agents (of one project): id -> {"archived_at", "how"}."""
        query = (
            "SELECT x.agent_id, x.archived_at, x.how FROM archived_agents x"
            " JOIN agents a ON a.id = x.agent_id WHERE x.restored_at IS NULL"
        )
        args: tuple[Any, ...] = ()
        if project:
            query, args = query + " AND a.project = ?", (project,)
        with self._conn() as conn:
            rows = conn.execute(query, args).fetchall()
        return {
            row["agent_id"]: {"archived_at": row["archived_at"], "how": row["how"]} for row in rows
        }

    def archive(
        self,
        agent_id: str,
        how: str = ARCHIVE_MANUAL,
        idle_since: str | None = None,
        held: Callable[[sqlite3.Connection], str | None] | None = None,
    ) -> bool | str | None:
        """Archive the agent unless it or a descendant has a queued or running turn (or,
        with `idle_since`, it was active after that time, or `held(conn)` gives a reason);
        atomic against a message arriving meanwhile. None: archived; True: refused as
        busy or recently active; a string: refused for the reason `held` gave."""
        with self.transaction() as conn:
            busy = conn.execute(
                subtree_sql() + " SELECT 1 FROM runs WHERE agent_id IN (SELECT id FROM tree)"
                f" AND status IN ('{ACTIVE[0]}', '{ACTIVE[1]}') LIMIT 1",
                (agent_id,),
            ).fetchone()
            if busy:
                return True
            if idle_since and (self._last_activity(conn, agent_id) or "") > idle_since:
                return True
            if held and (reason := held(conn)):
                return reason
            conn.execute(
                "INSERT INTO archived_agents (agent_id, archived_at, how, restored_at)"
                " VALUES (?, ?, ?, NULL) ON CONFLICT(agent_id) DO UPDATE SET"
                " archived_at = excluded.archived_at, how = excluded.how, restored_at = NULL",
                (agent_id, now(), how),
            )
        return None

    def restore(self, agent_id: str) -> bool:
        """Take the agent out of the archive; False if it was not archived."""
        with self._conn() as conn:
            return conn.execute(RESTORE_SQL, (now(), agent_id)).rowcount == 1

    def last_activity(self) -> dict[str, str]:
        """Per agent, the latest of: created, a run queued, started or ended, restored."""
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT a.id, MAX(a.created_at, COALESCE(r.latest, ''), COALESCE(x.restored_at, ''))"
                " AS latest FROM agents a"
                " LEFT JOIN (SELECT agent_id, MAX(MAX(created_at), COALESCE(MAX(started_at), ''),"
                " COALESCE(MAX(ended_at), '')) AS latest FROM runs GROUP BY agent_id) r"
                " ON r.agent_id = a.id LEFT JOIN archived_agents x ON x.agent_id = a.id"
            ).fetchall()
        return {row["id"]: row["latest"] for row in rows}

    @staticmethod
    def _last_activity(conn: sqlite3.Connection, agent_id: str) -> str | None:
        row = conn.execute(
            "SELECT MAX(t) AS latest FROM (SELECT created_at AS t FROM agents WHERE id = ?"
            " UNION ALL SELECT MAX(created_at) FROM runs WHERE agent_id = ?"
            " UNION ALL SELECT MAX(started_at) FROM runs WHERE agent_id = ?"
            " UNION ALL SELECT MAX(ended_at) FROM runs WHERE agent_id = ?"
            " UNION ALL SELECT restored_at FROM archived_agents WHERE agent_id = ?)",
            (agent_id,) * 5,
        ).fetchone()
        return row["latest"]

    # -- runs -------------------------------------------------------------

    @staticmethod
    def run_from_row(row: sqlite3.Row) -> Run:
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
        kind: str = MESSAGE,
        sender: str | None = None,
        notify: str = "each",
        cause_run: int | None = None,
    ) -> Run:
        with self._conn() as conn:
            run_id = self.insert_run(
                conn, agent_id, prompt, depends_on, output_schema, kind, sender, notify, cause_run
            )
        run = self.get_run(run_id)
        assert run is not None
        return run

    @staticmethod
    def insert_run(
        conn: sqlite3.Connection,
        agent_id: str,
        prompt: str,
        depends_on: list[int] | None = None,
        output_schema: dict[str, Any] | None = None,
        kind: str = MESSAGE,
        sender: str | None = None,
        notify: str = "each",
        cause_run: int | None = None,
        auto: bool = False,
    ) -> int:
        """Insert a waiting run and its metadata on `conn`; returns the run id. A message's
        origin is its sender; a notification's is set when it starts. `auto`: an automatic
        compaction (see may_start)."""
        if notify not in NOTIFY_MODES:
            raise ValueError(f"notify must be one of {', '.join(NOTIFY_MODES)}")
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
        conn.execute(
            "INSERT INTO run_meta (run_id, agent_id, kind, sender, notify, origin, cause_run, auto)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                run_id,
                agent_id,
                kind,
                sender,
                notify,
                sender if kind == MESSAGE else None,
                cause_run,
                int(auto),
            ),
        )
        if kind == MESSAGE:  # a message to an archived agent restores it
            conn.execute(RESTORE_SQL, (now(), agent_id))
        return run_id

    def delete_run(self, run_id: int, conn: sqlite3.Connection | None = None) -> None:
        """Remove a run that never had a turn (a notification with nothing to say)."""
        if conn is None:
            with self._conn() as own:
                self.delete_run(run_id, own)
            return
        for table in ("events", "run_usage", "deliveries", "run_meta"):
            conn.execute(f"DELETE FROM {table} WHERE run_id = ?", (run_id,))
        conn.execute("DELETE FROM runs WHERE id = ?", (run_id,))

    def withdraw_queued(self, run_id: int, sender: Any = ANYONE) -> Run:
        """Delete a queued message or compaction run that has not started; returns it as it
        was. Atomic against its runner taking the turn (`take_turn` then finds it gone). Raises
        ValueError, saying why, for anything else; with `sender`, also for a run that
        another sender queued."""
        with self.transaction() as conn:
            run = self._queued_message(conn, run_id, sender, compaction=True)
            self.delete_run(run_id, conn)
        return run

    def replace_queued(self, run_id: int, prompt: str, sender: Any = ANYONE) -> Run:
        """Replace the text of a queued message run that has not started (see
        `withdraw_queued`); its runner reads the text when it takes the turn."""
        with self.transaction() as conn:
            self._queued_message(conn, run_id, sender)
            conn.execute("UPDATE runs SET prompt = ? WHERE id = ?", (prompt, run_id))
        run = self.get_run(run_id)
        assert run is not None
        return run

    def _queued_message(
        self, conn: sqlite3.Connection, run_id: int, sender: Any, compaction: bool = False
    ) -> Run:
        """The queued run, if it is a message (with `compaction`: or a compaction)."""
        row = conn.execute(
            "SELECT r.*, m.kind AS meta_kind, m.sender AS meta_sender FROM runs r"
            " LEFT JOIN run_meta m ON m.run_id = r.id WHERE r.id = ?",
            (run_id,),
        ).fetchone()
        if row is None or (sender is not ANYONE and row["meta_sender"] != sender):
            raise ValueError(f"no run {run_id}" + ("" if sender is ANYONE else " sent by you"))
        if row["meta_kind"] == COMPACT and not compaction:
            raise ValueError(f"run {run_id} is a compaction (it can be withdrawn, not edited)")
        if (row["meta_kind"] or MESSAGE) not in IN_ORDER:
            raise ValueError(f"run {run_id} is a notification (written when it starts)")
        if row["status"] == RunStatus.RUNNING.value:
            raise ValueError(f"run {run_id} has already started")
        if row["status"] != RunStatus.WAITING.value:
            raise ValueError(f"run {run_id} has already ended ({row['status']})")
        return self.run_from_row(row)

    def meta(self, run_id: int) -> RunMeta:
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM run_meta WHERE run_id = ?", (run_id,)).fetchone()
        return from_row(RunMeta, row) if row else RunMeta.legacy()

    def meta_of(self, agent_id: str) -> dict[int, RunMeta]:
        """Metadata of an agent's runs, by run id. Runs without a row are absent."""
        with self._conn() as conn:
            return self._meta_of(conn, agent_id)

    @staticmethod
    def _meta_of(conn: sqlite3.Connection, agent_id: str) -> dict[int, RunMeta]:
        rows = conn.execute(
            "SELECT * FROM run_meta WHERE agent_id = ? ORDER BY run_id", (agent_id,)
        ).fetchall()
        return {row["run_id"]: from_row(RunMeta, row) for row in rows}

    def compactions(self, agent_id: str) -> set[int]:
        """Ids of the agent's compaction runs (see models.answers)."""
        with self._conn() as conn:
            return self._compactions(conn, agent_id)

    @staticmethod
    def _compactions(conn: sqlite3.Connection, agent_id: str) -> set[int]:
        rows = conn.execute(
            "SELECT run_id FROM run_meta WHERE agent_id = ? AND kind = ?", (agent_id, COMPACT)
        ).fetchall()
        return {row["run_id"] for row in rows}

    def add_auto_compaction(self, agent_id: str, prompt: str) -> Run | None:
        """Queue an automatic compaction of the agent unless a compaction of it is queued
        or running already (atomic); None then."""
        with self.transaction() as conn:
            pending = conn.execute(
                "SELECT 1 FROM runs r JOIN run_meta m ON m.run_id = r.id WHERE r.agent_id = ?"
                f" AND m.kind = ? AND r.status IN ('{ACTIVE[0]}', '{ACTIVE[1]}') LIMIT 1",
                (agent_id, COMPACT),
            ).fetchone()
            if pending:
                return None
            run_id = self.insert_run(conn, agent_id, prompt, kind=COMPACT, auto=True)
        return self.get_run(run_id)

    def ready_messages(self, agent_id: str) -> list[int]:
        """Ids of the agent's waiting message and compaction runs (not notifications) that
        could start once the agent's turn is free: none of their dependencies is still
        active."""
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT r.id, r.depends_on FROM runs r LEFT JOIN run_meta m ON m.run_id = r.id"
                " WHERE r.agent_id = ? AND r.status = ? AND COALESCE(m.kind, ?) IN (?, ?)"
                " ORDER BY r.id",
                (agent_id, RunStatus.WAITING.value, MESSAGE, *IN_ORDER),
            ).fetchall()
            return [
                row["id"] for row in rows if not self._blocked(conn, json.loads(row["depends_on"]))
            ]

    def running_run(self, agent_id: str) -> Run | None:
        """The agent's running run, if any (there is at most one)."""
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM runs WHERE agent_id = ? AND status = ? ORDER BY id LIMIT 1",
                (agent_id, RunStatus.RUNNING.value),
            ).fetchone()
        return self.run_from_row(row) if row else None

    def set_parent(self, agent_id: str, parent_id: str) -> None:
        with self._conn() as conn:
            conn.execute("UPDATE agents SET parent_id = ? WHERE id = ?", (parent_id, agent_id))

    def mark_stopping(self, run_ids: list[int]) -> None:
        """Record that a stop is about to signal these runs' runners (see reconcile)."""
        with self._conn() as conn:
            conn.executemany(
                "UPDATE run_meta SET stopping = 1 WHERE run_id = ?", [(i,) for i in run_ids]
            )

    def mark_checked(self, run_id: int) -> bool:
        """Record that the ended run's parents were checked; False if that was done before."""
        with self._conn() as conn:
            cursor = conn.execute(
                "UPDATE run_meta SET checked = 1 WHERE run_id = ? AND checked = 0", (run_id,)
            )
        return cursor.rowcount == 1

    def subtree_busy(self, agent_id: str) -> bool:
        """Whether the agent or a descendant has a waiting or running run, or an ended one
        whose parents were not checked yet for a notification. One query."""
        with self._conn() as conn:
            row = conn.execute(
                subtree_sql() + " SELECT 1 FROM runs r LEFT JOIN run_meta m ON m.run_id = r.id"
                " WHERE r.agent_id IN (SELECT id FROM tree)"
                f" AND (r.status IN ('{ACTIVE[0]}', '{ACTIVE[1]}') OR m.checked = 0) LIMIT 1",
                (agent_id,),
            ).fetchone()
        return row is not None

    def active_descendants(self, agent_id: str) -> int:
        """How many of the agent's descendants (not itself) have a waiting or running run."""
        with self._conn() as conn:
            row = conn.execute(
                subtree_sql() + " SELECT COUNT(DISTINCT r.agent_id) AS n FROM runs r"
                " WHERE r.agent_id IN (SELECT id FROM tree) AND r.agent_id != ?"
                f" AND r.status IN ('{ACTIVE[0]}', '{ACTIVE[1]}')",
                (agent_id, agent_id),
            ).fetchone()
        return row["n"]

    # -- turn order -------------------------------------------------------
    #
    # At most one run of an agent runs at a time. Message runs (and compaction runs, which
    # are scheduled like them) keep their order among themselves, except that an automatic
    # compaction goes ahead of them (a queued message would otherwise pay for the whole
    # context; it still runs after the compaction, however that ends); a waiting
    # notification never holds a message back and starts only when
    # no message is ready to. Runs queued by older code (no metadata) are treated with
    # care because their runner may be old code too, which waits for every earlier
    # active run and starts without checking: a notification lets an earlier one go first
    # and does not wait for a later one (whose old runner waits for the notification).

    def may_start(self, conn: sqlite3.Connection, run_id: int, agent_id: str, kind: str) -> bool:
        """Whether the run may take the agent's turn now (dependencies aside for messages)."""
        rows = conn.execute(
            "SELECT r.id, r.status, r.depends_on, m.kind, m.auto FROM runs r"
            " LEFT JOIN run_meta m ON m.run_id = r.id"
            f" WHERE r.agent_id = ? AND r.status IN ('{ACTIVE[0]}', '{ACTIVE[1]}')"
            " ORDER BY r.id",
            (agent_id,),
        ).fetchall()
        own = next((row for row in rows if row["id"] == run_id), None)
        rows = [row for row in rows if row["id"] != run_id]
        if any(row["status"] == RunStatus.RUNNING.value for row in rows):
            return False

        def place(row: sqlite3.Row) -> tuple[int, int]:  # queue order: automatic ones first
            return (0 if row["auto"] else 1, row["id"])

        messages = sorted((row for row in rows if (row["kind"] or MESSAGE) in IN_ORDER), key=place)
        if kind in IN_ORDER:
            mine = place(own) if own is not None else (1, run_id)
            # Never ahead of a run queued by older code: its runner would not wait for it.
            older = any(row["kind"] is None and row["id"] < run_id for row in messages)
            return not older and not any(place(row) < mine for row in messages)
        if any(row["kind"] == NOTIFICATION and row["id"] < run_id for row in rows):
            return False
        for position, row in enumerate(messages):
            if row["kind"] is None:  # queued by older code
                if row["id"] < run_id:
                    return False
                continue
            head = position == 0
            if head and not self._blocked(conn, json.loads(row["depends_on"])):
                return False  # a message is ready to start: it goes first
        return True

    @staticmethod
    def _blocked(conn: sqlite3.Connection, depends_on: list[int]) -> bool:
        if not depends_on:
            return False
        marks = ", ".join("?" * len(depends_on))
        row = conn.execute(
            f"SELECT COUNT(*) AS n FROM runs WHERE id IN ({marks})"
            f" AND status IN ('{ACTIVE[0]}', '{ACTIVE[1]}')",
            depends_on,
        ).fetchone()
        return row["n"] > 0

    def take_turn(self, run: Run, kind: str = MESSAGE) -> bool | None:
        """Atomically mark the run running if it may start now. True: started; False: not
        yet; None: the run is gone or no longer waiting."""
        if not self.could_start(run, kind):
            return False
        with self.transaction() as conn:
            row = conn.execute("SELECT status FROM runs WHERE id = ?", (run.id,)).fetchone()
            if row is None or row["status"] != RunStatus.WAITING.value:
                return None
            if not self.may_start(conn, run.id, run.agent_id, kind):
                return False
            self.start_run(conn, run.id)
            return True

    def could_start(self, run: Run, kind: str) -> bool:
        """`may_start` without the write lock: a cheap check while waiting, to be repeated
        in a transaction before starting. A run that is gone answers True (to be seen)."""
        with self._conn() as conn:
            return self.may_start(conn, run.id, run.agent_id, kind)

    @staticmethod
    def start_run(conn: sqlite3.Connection, run_id: int, **fields: Any) -> None:
        fields = {"status": RunStatus.RUNNING.value, "started_at": now(), **fields}
        assignments = ", ".join(f"{key} = ?" for key in fields)
        conn.execute(f"UPDATE runs SET {assignments} WHERE id = ?", (*fields.values(), run_id))
        # A turn that starts (a notification too) brings an archived agent back.
        conn.execute(
            "UPDATE archived_agents SET restored_at = ? WHERE restored_at IS NULL"
            " AND agent_id = (SELECT agent_id FROM runs WHERE id = ?)",
            (fields["started_at"], run_id),
        )

    def get_run(self, run_id: int) -> Run | None:
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
        return self.run_from_row(row) if row else None

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
            return self._runs_of(conn, agent_id)

    @classmethod
    def _runs_of(cls, conn: sqlite3.Connection, agent_id: str) -> list[Run]:
        rows = conn.execute(
            "SELECT * FROM runs WHERE agent_id = ? ORDER BY id", (agent_id,)
        ).fetchall()
        return [cls.run_from_row(row) for row in rows]

    def snapshot(
        self, agent_id: str
    ) -> tuple[list[Run], dict[int, RunMeta], dict[int, Usage], set[int]]:
        """An agent's runs (runs_of), their metadata (meta_of), usage records (usage_of) and
        compaction ids (compactions), read in one transaction so that they agree with each
        other; timeline.AgentRuns is built from them."""
        with self._read() as conn:
            return (
                self._runs_of(conn, agent_id),
                self._meta_of(conn, agent_id),
                self._usage_of(conn, agent_id),
                self._compactions(conn, agent_id),
            )

    def last_run(self, agent_id: str) -> Run | None:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM runs WHERE agent_id = ? ORDER BY id DESC LIMIT 1", (agent_id,)
            ).fetchone()
        return self.run_from_row(row) if row else None

    def active_runs(self, agent_id: str | None = None) -> list[Run]:
        query = f"SELECT * FROM runs WHERE status IN ('{ACTIVE[0]}', '{ACTIVE[1]}')"
        args: tuple[Any, ...] = ()
        if agent_id:
            query, args = query + " AND agent_id = ?", (agent_id,)
        with self._conn() as conn:
            rows = conn.execute(query + " ORDER BY id", args).fetchall()
        return [self.run_from_row(row) for row in rows]

    def reconcile(self) -> None:
        """Fail active runs whose runner process no longer exists (crash, reboot); a
        notification that never started is deleted instead. A run whose runner a stop
        signalled (`stopping`) ends as stopped instead of failed, and a notification of it
        that never started does not recheck its agent: the stop was deliberate, whichever
        process sees the runner dead first. Then hand every run ended this way, and every
        ended run whose runner died before checking the parents, to `on_run_end`, once
        (whoever marks it checked first)."""
        ended: list[tuple[int, str, bool]] = []
        for run in self.active_runs():
            if run.pid is not None and not pid_alive(run.pid):
                # Re-read: the runner may have finished between the query and the check.
                fresh = self.get_run(run.id)
                if not (fresh and fresh.status.active):
                    continue
                meta = self.meta(run.id)
                if fresh.started_at is None and meta.kind == NOTIFICATION:
                    self.delete_run(run.id)
                    ended.append((run.id, run.agent_id, not meta.stopping))
                else:
                    if meta.stopping:
                        self.finish_run(run.id, RunStatus.STOPPED, error="stopped")
                    else:
                        self.finish_run(run.id, RunStatus.FAILED, error="runner process died")
                    if self.mark_checked(run.id):
                        ended.append((run.id, run.agent_id, False))
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT r.id, r.agent_id, r.pid, r.ended_at FROM run_meta m"
                " JOIN runs r ON r.id = m.run_id"
                f" WHERE m.checked = 0 AND r.status NOT IN ('{ACTIVE[0]}', '{ACTIVE[1]}')"
            ).fetchall()
        for row in rows:  # the trigger was lost with its runner
            lost = not pid_alive(row["pid"]) or _older_than(row["ended_at"], LOST_TRIGGER_SECONDS)
            if lost and self.mark_checked(row["id"]):
                ended.append((row["id"], row["agent_id"], False))
        if self.on_run_end:
            for run_id, agent_id, recheck in ended:
                self.on_run_end(run_id, agent_id, recheck)

    # -- usage ------------------------------------------------------------

    def set_usage(self, run_id: int, agent_id: str, usage: Usage) -> None:
        with self._conn() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO run_usage (run_id, agent_id, input_tokens, output_tokens,"
                " cache_read_tokens, cache_creation_tokens, cost_usd, model, created_at,"
                f" session_id, {', '.join(CONTEXT_FIELDS)})"
                f" VALUES ({', '.join('?' * (10 + len(CONTEXT_FIELDS)))})",
                (
                    run_id,
                    agent_id,
                    *(getattr(usage, key) for key in TOKEN_FIELDS),
                    usage.cost_usd,
                    usage.model,
                    now(),
                    usage.session_id,
                    *(getattr(usage, key) for key in CONTEXT_FIELDS),
                ),
            )

    def usage_of(self, agent_id: str) -> dict[int, Usage]:
        """Per-run usage of an agent's runs, by run id. Runs without a record are absent."""
        with self._conn() as conn:
            return self._usage_of(conn, agent_id)

    @staticmethod
    def _usage_of(conn: sqlite3.Connection, agent_id: str) -> dict[int, Usage]:
        rows = conn.execute(
            "SELECT u.* FROM run_usage u JOIN runs r ON r.id = u.run_id"
            " WHERE r.agent_id = ? ORDER BY u.run_id",
            (agent_id,),
        ).fetchall()
        return {row["run_id"]: from_row(Usage, row) for row in rows}

    def usage_sums(self) -> tuple[list[Usage], list[Run], dict[int, Usage]]:
        """What models.spent needs of every agent's runs (archived agents too), read
        together and cheaply enough for every dashboard refresh: the sums of the usage
        records per agent, for the agents without a run by old code; and, for those with
        one (no record but a cost: a session total, see models.running_total), their runs
        (only id, agent_id and cost_usd; the rest left at its default) and records, to be
        walked in order. Reads the runs through the runs_spend index, never their text.
        The sums also hold what the agents deleted for good had spent (spent_removed),
        read in the same transaction, so a deletion meanwhile is counted once."""
        keys = (*TOKEN_FIELDS, "cost_usd")
        sums = ", ".join(f"SUM(u.{key}) AS {key}" for key in keys)
        with self._read() as conn:
            rows = conn.execute(
                f"SELECT r.agent_id, {sums},"
                " SUM(u.run_id IS NULL AND COALESCE(r.cost_usd, 0) <> 0) AS old_code"
                " FROM runs r LEFT JOIN run_usage u ON u.run_id = r.id GROUP BY r.agent_id"
            ).fetchall()
            walked = [row["agent_id"] for row in rows if row["old_code"]]
            marks = ", ".join("?" * len(walked))
            runs = conn.execute(
                f"SELECT id, agent_id, cost_usd FROM runs WHERE agent_id IN ({marks})", walked
            ).fetchall()
            records = conn.execute(
                "SELECT u.* FROM run_usage u JOIN runs r ON r.id = u.run_id"
                f" WHERE r.agent_id IN ({marks})",
                walked,
            ).fetchall()
            removed = conn.execute(
                f"SELECT 0 AS old_code, {', '.join(f'SUM({key}) AS {key}' for key in keys)}"
                " FROM spent_removed"
            ).fetchone()
        return (
            [from_row(Usage, row) for row in (*rows, removed) if not row["old_code"]],
            [Run(row["id"], row["agent_id"], "", cost_usd=row["cost_usd"]) for row in runs],
            {row["run_id"]: from_row(Usage, row) for row in records},
        )

    def get_usage(self, run_id: int) -> Usage | None:
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM run_usage WHERE run_id = ?", (run_id,)).fetchone()
        return from_row(Usage, row) if row else None

    # -- deliveries -------------------------------------------------------

    def claim(self, caller: str, run_ids: list[int]) -> set[int]:
        """Record that the results of `run_ids` go to `caller`; returns the run ids this
        call recorded. Ids recorded before (by any process) are left out, so of several
        concurrent claims of a run exactly one gets it."""
        claimed = set()
        with self._conn() as conn:
            for run_id in run_ids:
                cursor = conn.execute(
                    "INSERT OR IGNORE INTO deliveries (caller, run_id, delivered_at)"
                    " VALUES (?, ?, ?)",
                    (caller, run_id, now()),
                )
                if cursor.rowcount == 1:
                    claimed.add(run_id)
        return claimed

    def last_delivered(self, caller: str, agent_id: str) -> int | None:
        """The newest run of `agent_id` whose result went to `caller`, if any."""
        with self._conn() as conn:
            row = conn.execute(
                "SELECT MAX(d.run_id) AS run_id FROM deliveries d JOIN runs r ON r.id = d.run_id"
                " WHERE d.caller = ? AND r.agent_id = ?",
                (caller, agent_id),
            ).fetchone()
        return row["run_id"]

    # -- notices ----------------------------------------------------------

    def add_notice(
        self,
        kind: str,
        agent_id: str,
        text: str,
        decision_id: int | None = None,
        wait: float = 30,
        route: str | None = None,
        claimed_by: str | None = None,
        created_at: str | None = None,
        steps: Sequence[NoticeStep] = (),
    ) -> int:
        """Record a notice and prune those (and their steps) older than NOTICE_KEEP; its id.
        `route`: whom it is left to; with `claimed_by` it is claimed at once (shown by the
        caller). `steps` are written in the same transaction. `wait`: seconds to wait for the
        write lock (then sqlite3.OperationalError)."""
        cutoff = (datetime.now(UTC) - NOTICE_KEEP).isoformat(timespec="seconds")
        stamp = now()
        with self._conn(wait) as conn:
            conn.execute("DELETE FROM notices WHERE created_at < ?", (cutoff,))
            conn.execute(
                "DELETE FROM notice_steps WHERE notice_id < (SELECT COALESCE(MIN(id), 0)"
                " FROM notices)"
            )
            cursor = conn.execute(
                "INSERT INTO notices (created_at, kind, agent_id, decision_id, text, route,"
                " claimed_at, claimed_by) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    created_at or stamp,
                    kind,
                    agent_id,
                    decision_id,
                    text,
                    route,
                    stamp if claimed_by else None,
                    claimed_by,
                ),
            )
            assert cursor.lastrowid is not None
            self._add_steps(conn, cursor.lastrowid, steps)
        return cursor.lastrowid

    @staticmethod
    def _add_steps(conn: sqlite3.Connection, notice_id: int, steps: Sequence[NoticeStep]) -> None:
        conn.executemany(
            "INSERT INTO notice_steps (notice_id, at, step, path, actor, detail)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            [(notice_id, s.at or now(), s.step, s.path, s.actor, s.detail) for s in steps],
        )

    def add_notice_step(self, notice_id: int, step: NoticeStep, wait: float = 30) -> None:
        with self._conn(wait) as conn:
            self._add_steps(conn, notice_id, [step])

    def notices(self, after: int = 0) -> list[dict[str, Any]]:
        """The unclaimed notices with an id above `after`, oldest first."""
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT id, kind, agent_id, decision_id, text, created_at, route FROM notices"
                " WHERE id > ? AND claimed_at IS NULL ORDER BY id",
                (after,),
            ).fetchall()
        return [dict(row) for row in rows]

    def notice(self, notice_id: int) -> dict[str, Any] | None:
        """One notice, claimed or not (every column), or None."""
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM notices WHERE id = ?", (notice_id,)).fetchone()
        return dict(row) if row else None

    def notice_cursor(self) -> int:
        """The highest notice id (0: none): a reader starting here skips what came before."""
        with self._conn() as conn:
            return conn.execute("SELECT COALESCE(MAX(id), 0) FROM notices").fetchone()[0]

    def claim_notice(
        self,
        notice_id: int,
        wait: float = 30,
        routes: Sequence[str] | None = None,
        by: str | None = None,
        step: NoticeStep | None = None,
    ) -> bool:
        """Mark an unclaimed notice as shown; True only for the one caller that did. With
        `routes`, only a notice left to one of them (or recorded by older code: no route);
        `step` is written with the claim. `wait` as in add_notice."""
        where, args = self._routes(routes)
        with self._conn(wait) as conn:
            cursor = conn.execute(
                "UPDATE notices SET claimed_at = ?, claimed_by = ?"
                f" WHERE id = ? AND claimed_at IS NULL{where}",
                (now(), by, notice_id, *args),
            )
            if cursor.rowcount == 1 and step is not None:
                self._add_steps(conn, notice_id, [step])
        return cursor.rowcount == 1

    @staticmethod
    def _routes(routes: Sequence[str] | None) -> tuple[str, tuple[str, ...]]:
        if routes is None:
            return "", ()
        marks = ", ".join("?" * len(routes))
        return f" AND (route IS NULL OR route IN ({marks}))", tuple(routes)

    def release_notice(
        self,
        notice_id: int,
        wait: float = 30,
        by: str | None = None,
        route: str | None = None,
        step: NoticeStep | None = None,
    ) -> bool:
        """Make a claimed notice unclaimed again: its taker could not show it, so another way
        may claim it. With `by`, only a claim of that claimer; `route`: whom it is left to
        now. True when it was released."""
        mine = " AND claimed_by = ?" if by is not None else ""
        with self._conn(wait) as conn:
            cursor = conn.execute(
                "UPDATE notices SET claimed_at = NULL, claimed_by = NULL,"
                f" route = COALESCE(?, route) WHERE id = ? AND claimed_at IS NOT NULL{mine}",
                (route, notice_id, *((by,) if by is not None else ())),
            )
            if cursor.rowcount == 1 and step is not None:
                self._add_steps(conn, notice_id, [step])
        return cursor.rowcount == 1

    def reroute_notice(
        self,
        notice_id: int,
        route: str,
        step: NoticeStep,
        was: Sequence[str] | None = None,
        claimed_by: str | None = None,
        wait: float = 30,
    ) -> bool:
        """Leave an unclaimed notice to `route` (from one of `was`, when given); with
        `claimed_by`, claim it too (its new taker shows it at once). True when it was."""
        where, args = self._routes(was)
        with self._conn(wait) as conn:
            cursor = conn.execute(
                "UPDATE notices SET route = ?, claimed_at = ?, claimed_by = ?"
                f" WHERE id = ? AND claimed_at IS NULL{where}",
                (route, now() if claimed_by else None, claimed_by, notice_id, *args),
            )
            if cursor.rowcount == 1:
                self._add_steps(conn, notice_id, [step])
        return cursor.rowcount == 1

    def counted_in_text(self, notice_id: int) -> set[int]:
        """The notices claimed as counted in the text of `notice_id` (by any taker: the step
        written with the claim ends "counted in the text of #<notice_id>",
        core/notices.worth_showing)."""
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT DISTINCT notice_id FROM notice_steps WHERE step = 'drop' AND detail LIKE ?",
                (f"%counted in the text of #{notice_id}",),
            ).fetchall()
        return {row[0] for row in rows}

    def notice_trace(self, limit: int = 20) -> list[dict[str, Any]]:
        """The newest `limit` notices, newest first, claimed or not, each with its `steps`
        (oldest first; none for a notice of older code)."""
        with self._conn() as conn:
            notices = [
                dict(row)
                for row in conn.execute(
                    "SELECT * FROM notices ORDER BY id DESC LIMIT ?", (max(0, limit),)
                )
            ]
            if not notices:
                return []
            low = notices[-1]["id"]
            steps: dict[int, list[dict[str, Any]]] = {}
            for row in conn.execute(
                "SELECT * FROM notice_steps WHERE notice_id >= ? ORDER BY id", (low,)
            ):
                steps.setdefault(row["notice_id"], []).append(dict(row))
        for notice in notices:
            notice["steps"] = steps.get(notice["id"], [])
        return notices

    # -- events -----------------------------------------------------------

    def add_event(self, run_id: int, agent_id: str, kind: str, text: str) -> None:
        if len(text) > EVENT_TEXT_LIMIT:
            text = text[:EVENT_TEXT_LIMIT] + f"… [{len(text) - EVENT_TEXT_LIMIT} more chars]"
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO events (run_id, agent_id, ts, kind, text) VALUES (?, ?, ?, ?, ?)",
                (run_id, agent_id, now(), kind, text),
            )

    def get_event(self, event_id: int) -> Event | None:
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM events WHERE id = ?", (event_id,)).fetchone()
        return from_row(Event, row) if row else None

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

    # -- board git (cache of core/board_git) -----------------------------------

    BOARD_GIT_COLUMNS = (
        "item_id",
        "branch",
        "base",
        "tip",
        "ahead",
        "behind",
        "dirty",
        "ahead_tip",
        "ahead_in_base",
        "done_tip",
        "merged",
        "error",
        "checked_at",
    )

    def board_git(self, item_ids: list[int] | None = None) -> dict[int, dict[str, Any]]:
        """The cached git state rows by item id: all, or those of `item_ids`."""
        query, args = "SELECT * FROM board_git", ()
        if item_ids is not None:
            query += " WHERE item_id IN (SELECT value FROM json_each(?))"
            args = (json.dumps(list(item_ids)),)
        with self._conn() as conn:
            return {row["item_id"]: dict(row) for row in conn.execute(query, args)}

    def save_board_git(
        self, row: dict[str, Any], conn: sqlite3.Connection | None = None, wait: float = 30
    ) -> None:
        """Insert or replace one row (every column of BOARD_GIT_COLUMNS), in `conn` if given;
        else waiting at most `wait` seconds for the write lock (then sqlite3.OperationalError)."""
        names = ", ".join(self.BOARD_GIT_COLUMNS)
        marks = ", ".join("?" for _ in self.BOARD_GIT_COLUMNS)
        values = tuple(row[name] for name in self.BOARD_GIT_COLUMNS)
        sql = f"INSERT OR REPLACE INTO board_git ({names}) VALUES ({marks})"
        if conn is not None:
            conn.execute(sql, values)
            return
        with self._conn(wait) as own:
            own.execute(sql, values)
