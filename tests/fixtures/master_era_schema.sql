-- The schema of store.py at master c60b5f1, before automatic compaction (run_meta.auto,
-- run_usage.first_context_tokens), decisions and the board (test_store.py).
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
-- Which run results wait_for_agents already returned to which caller ('' is `orch chat`),
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
-- checked: 1 once the run has ended and its parents were checked for a notification.
CREATE TABLE IF NOT EXISTS run_meta (
    run_id INTEGER PRIMARY KEY,
    agent_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    sender TEXT,
    notify TEXT NOT NULL DEFAULT 'each',
    origin TEXT,
    cause_run INTEGER,
    claimed TEXT,
    checked INTEGER NOT NULL DEFAULT 0
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
CREATE INDEX IF NOT EXISTS runs_agent ON runs(agent_id, id);
CREATE INDEX IF NOT EXISTS events_agent ON events(agent_id, id);
