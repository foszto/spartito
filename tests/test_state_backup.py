"""`spartito state`: backups made with SQLite's online backup API, their listing, and restoring
one in place (core/state.py, Store.backup, Store.replace_from). Every test works in the
temporary ORCH_HOME of conftest."""

from __future__ import annotations

import os
import sqlite3
import stat
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest
from typer.testing import CliRunner

from orchestrator import cli, config
from orchestrator.core import state
from orchestrator.errors import NeedsConfirmation, OrchestratorError
from orchestrator.models import Agent, Project, RunStatus
from orchestrator.service import Orchestrator
from orchestrator.store import Store

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def o() -> Orchestrator:
    orch = Orchestrator()
    orch.store.add_project(Project("my-app", "/src/my-app", "main"))
    add_agent(orch, "fix-login-3f2a")
    return orch


def add_agent(orch: Orchestrator, agent_id: str) -> None:
    orch.store.add_agent(
        Agent(id=agent_id, project="my-app", name=agent_id, role="worker", provider="claude",
              cwd=f"/w/{agent_id}")
    )  # fmt: skip


def finished_run(orch: Orchestrator, agent_id: str, prompt: str) -> int:
    run = orch.store.add_run(agent_id, prompt)
    orch.store.finish_run(run.id, RunStatus.DONE, result=f"answer to {prompt}")
    return run.id


def prompts(path: Path | None = None) -> list[str]:
    conn = sqlite3.connect(path or config.db_path())
    try:
        return [row[0] for row in conn.execute("SELECT prompt FROM runs ORDER BY id")]
    finally:
        conn.close()


def backups() -> list[str]:
    folder = config.backups_dir()
    return sorted(entry.name for entry in folder.iterdir()) if folder.exists() else []


def invoke(*args: str):
    return CliRunner().invoke(cli.app, list(args), prog_name=config.CLI_NAME)


# -- backup ------------------------------------------------------------------------------------


def test_a_backup_taken_while_another_thread_writes_is_one_consistent_snapshot(
    o: Orchestrator,
) -> None:
    """Each write is one transaction of a run and its two events; the backup holds whole
    transactions only, passes the integrity check and opens with Store."""
    stop, written = threading.Event(), [0]

    def writer() -> None:
        conn = sqlite3.connect(config.db_path(), timeout=30, isolation_level=None)
        try:
            while not stop.is_set():
                conn.execute("BEGIN IMMEDIATE")
                run_id = conn.execute(
                    "INSERT INTO runs (agent_id, prompt, status, created_at)"
                    " VALUES ('fix-login-3f2a', 'p', 'done', 't')"
                ).lastrowid
                for kind in ("one", "two"):
                    conn.execute(
                        "INSERT INTO events (run_id, agent_id, ts, kind, text)"
                        " VALUES (?, 'fix-login-3f2a', 't', ?, 'x')",
                        (run_id, kind),
                    )
                conn.execute("COMMIT")
                written[0] += 1
        finally:
            conn.close()

    thread = threading.Thread(target=writer)
    thread.start()
    try:
        deadline = time.monotonic() + 10
        while written[0] < 30 and time.monotonic() < deadline:
            time.sleep(0.01)
        result = o.backup_state()
        during = written[0]
    finally:
        stop.set()
        thread.join()
    assert written[0] > during >= 30  # the writer went on after the backup
    database = Path(result["database"])
    assert state.check_database(database) == []
    conn = sqlite3.connect(database)
    try:
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "delete"  # a single file
        runs = conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0]
        partial = conn.execute(
            "SELECT COUNT(*) FROM runs r WHERE (SELECT COUNT(*) FROM events e"
            " WHERE e.run_id = r.id) != 2"
        ).fetchone()[0]
    finally:
        conn.close()
    assert 30 <= runs <= written[0] and partial == 0
    assert not list(database.parent.glob("*-wal"))
    copy = Store(database)
    assert copy.get_agent("fix-login-3f2a") is not None
    assert len(copy.runs_of("fix-login-3f2a")) == runs


def test_a_backup_is_private_and_holds_the_user_files_but_logs_only_when_asked(
    o: Orchestrator,
) -> None:
    home = config.home()
    (home / "roles").mkdir()
    (home / "roles" / "docs-writer.md").write_text("---\ndescription: d\n---\nWrite docs.")
    (home / "loadout.toml").write_text("[default]\ntools = 'Bash'\n")
    (config.run_dir(1)).mkdir(parents=True)
    (config.run_dir(1) / "result.md").write_text("done")
    (home / "worktrees" / "my-app" / "fix-login-3f2a").mkdir(parents=True)
    (home / "workspace").mkdir(exist_ok=True)

    plain = Path(o.backup_state()["path"])
    with_logs = Path(o.backup_state(logs=True)["path"])

    assert plain.parent == config.backups_dir()
    assert sorted(p.name for p in plain.iterdir()) == ["loadout.toml", "orch.db", "roles"]
    assert sorted(p.name for p in with_logs.iterdir()) == [
        "loadout.toml", "logs", "orch.db", "roles",
    ]  # fmt: skip
    assert (with_logs / "logs" / "run-1" / "result.md").read_text() == "done"
    assert (plain / "roles" / "docs-writer.md").read_text().endswith("Write docs.")
    for root in (plain, with_logs):
        for path in [root, *root.rglob("*")]:
            mode = stat.S_IMODE(path.stat().st_mode)
            assert mode == (0o700 if path.is_dir() else 0o600), path
    assert stat.S_IMODE(config.backups_dir().stat().st_mode) == 0o700


def test_a_backup_without_user_files_holds_the_database_only_and_never_overwrites(
    o: Orchestrator, tmp_path: Path
) -> None:
    target = tmp_path / "elsewhere" / "nightly"
    result = o.backup_state(target)
    assert result["path"] == str(target) and result["files"] == []
    assert [p.name for p in target.iterdir()] == ["orch.db"]
    with pytest.raises(OrchestratorError, match="exists already"):
        o.backup_state(target)


def test_symbolic_links_are_left_out(o: Orchestrator, tmp_path: Path) -> None:
    outside = tmp_path / "outside.md"
    outside.write_text("not the home's")
    roles = config.home() / "roles"
    roles.mkdir()
    (roles / "linked.md").symlink_to(outside)
    (roles / "own.md").write_text("own")
    result = o.backup_state()
    assert sorted(p.name for p in (Path(result["path"]) / "roles").iterdir()) == ["own.md"]
    assert result["skipped"] == [str(roles / "linked.md")]


# -- list --------------------------------------------------------------------------------------


def test_list_shows_backup_directories_and_hand_made_database_files_newest_first(
    o: Orchestrator,
) -> None:
    folder = config.backups_dir()
    config.run_dir(1).mkdir(parents=True)
    o.backup_state(folder / "20260102T100000Z")
    o.backup_state(folder / "20260301T100000Z", logs=True)
    hand_made = folder / "orch-20260201-120000.db"
    o.store.backup(hand_made)
    when = time.mktime((2026, 2, 1, 12, 0, 0, 0, 0, -1))
    os.utime(hand_made, (when, when))
    (folder / "notes.txt").write_text("not a backup")
    (folder / "empty").mkdir()

    listed = o.list_backups()
    assert [item["name"] for item in listed] == [
        "20260301T100000Z", "orch-20260201-120000.db", "20260102T100000Z",
    ]  # fmt: skip
    assert [item["logs"] for item in listed] == [True, False, False]
    assert [item["kind"] for item in listed] == ["directory", "file", "directory"]
    assert listed[0]["time"] == "2026-03-01T10:00:00+00:00"
    assert all(item["size"] > 0 for item in listed)

    shown = invoke("state", "list")
    assert shown.exit_code == 0
    lines = shown.stdout.splitlines()
    assert lines[0].startswith("20260301T100000Z") and lines[0].endswith("logs")
    assert lines[1].startswith("orch-20260201-120000.db")
    assert invoke("state", "list", "--json").stdout.count('"name"') == 3


def test_list_without_backups(o: Orchestrator) -> None:
    assert o.list_backups() == []
    assert invoke("state", "list").stdout == "no backups\n"
    assert not config.backups_dir().exists()  # listing creates nothing


# -- restore: refusals -------------------------------------------------------------------------


def test_restore_without_the_flag_changes_nothing_and_exits_non_zero(o: Orchestrator) -> None:
    finished_run(o, "fix-login-3f2a", "kept")
    name = o.backup_state()["name"]
    finished_run(o, "fix-login-3f2a", "later")
    before = backups()

    result = invoke("state", "restore", name)
    assert result.exit_code == 1
    assert f"would restore {config.backups_dir() / name}" in result.stdout
    assert f"`{config.CLI_NAME} state restore {name} --yes`" in result.stderr
    assert prompts() == ["kept", "later"] and backups() == before
    with pytest.raises(NeedsConfirmation):
        o.restore_state(name)


@pytest.mark.parametrize("status", [RunStatus.RUNNING, RunStatus.WAITING])
def test_restore_refuses_while_a_turn_is_running_or_queued(
    o: Orchestrator, status: RunStatus
) -> None:
    finished_run(o, "fix-login-3f2a", "kept")
    name = o.backup_state()["name"]
    run = o.store.add_run("fix-login-3f2a", "busy")
    o.store.update_run(run.id, status=status, pid=os.getpid() if status == "running" else None)
    before = backups()

    result = invoke("state", "restore", name, "--yes")
    assert result.exit_code == 1
    assert f"fix-login-3f2a run {run.id} {status.value}" in result.stderr
    assert prompts() == ["kept", "busy"] and backups() == before


def test_a_turn_whose_runner_is_gone_does_not_block_a_restore(o: Orchestrator) -> None:
    name = o.backup_state()["name"]
    run = o.store.add_run("fix-login-3f2a", "orphan")
    gone = subprocess.Popen([sys.executable, "-c", "pass"])
    gone.wait()
    o.store.update_run(run.id, status=RunStatus.RUNNING, pid=gone.pid)
    assert o.restore_state(name, confirm=True)["restored"]


def test_restore_refuses_what_is_not_an_intact_state_database(
    o: Orchestrator, tmp_path: Path
) -> None:
    finished_run(o, "fix-login-3f2a", "kept")
    text = tmp_path / "notes.db"
    text.write_text("not a database at all " * 500)
    other = tmp_path / "other.db"
    with sqlite3.connect(other) as conn:
        conn.execute("CREATE TABLE things (x)")
    conn.close()
    corrupt = Path(o.backup_state(tmp_path / "corrupt")["database"])
    data = bytearray(corrupt.read_bytes())
    page = 4096
    data[2 * page : 2 * page + 512] = b"\xa5" * 512  # a b-tree page's header and cells
    corrupt.write_bytes(bytes(data))
    before = backups()

    for path, reason in (
        (text, "not a readable SQLite database"),
        (other, "not a state database"),
        (corrupt, "integrity check|not a readable SQLite database"),
        (tmp_path / "missing", "no backup"),
        (config.db_path(), "is the live database"),
    ):
        with pytest.raises(OrchestratorError, match=reason):
            o.restore_state(str(path), confirm=True)
    result = invoke("state", "restore", str(text), "--yes")
    assert result.exit_code == 1 and "not restored" in result.stderr
    assert prompts() == ["kept"] and backups() == before


# -- restore -----------------------------------------------------------------------------------


def test_a_restore_brings_the_rows_back_and_keeps_the_replaced_state_restorable(
    o: Orchestrator,
) -> None:
    home = config.home()
    (home / "roles").mkdir()
    (home / "roles" / "docs-writer.md").write_text("first")
    (home / "loadout.toml").write_text("# first\n")
    finished_run(o, "fix-login-3f2a", "one")
    first = o.backup_state()["name"]
    finished_run(o, "fix-login-3f2a", "two")
    add_agent(o, "add-search-9c1d")
    (home / "roles" / "docs-writer.md").write_text("second")
    (home / "roles" / "reviewer-strict.md").write_text("new since")
    (home / "loadout.toml").write_text("# second\n")

    result = invoke("state", "restore", first, "--yes")
    assert result.exit_code == 0, result.output
    kept = Path(result.stdout.split("kept in ")[1].split(" (")[0])
    assert kept.name.endswith("-before-restore") and kept.parent == config.backups_dir()
    assert f"`{config.CLI_NAME} state restore {kept.name} --yes`" in result.stdout
    assert "restart it" in result.stderr

    assert prompts() == ["one"]
    assert o.store.get_agent("add-search-9c1d") is None
    assert (home / "roles" / "docs-writer.md").read_text() == "first"
    assert (home / "roles" / "reviewer-strict.md").read_text() == "new since"  # never deleted
    assert (home / "loadout.toml").read_text() == "# first\n"
    assert prompts(kept / "orch.db") == ["one", "two"]
    assert (kept / "roles" / "docs-writer.md").read_text() == "second"

    again = o.restore_state(kept.name, confirm=True)
    assert prompts() == ["one", "two"]
    assert o.store.get_agent("add-search-9c1d") is not None
    assert (home / "roles" / "docs-writer.md").read_text() == "second"
    assert Path(again["kept"]).exists() and len(backups()) == 3


def test_logs_are_restored_only_with_the_flag(o: Orchestrator) -> None:
    log = config.run_dir(1) / "result.md"
    log.parent.mkdir(parents=True)
    log.write_text("from the backup")
    name = o.backup_state(logs=True)["name"]
    log.write_text("since then")

    kept = Path(o.restore_state(name, confirm=True)["kept"])
    assert log.read_text() == "since then" and not (kept / "logs").exists()
    kept = Path(o.restore_state(name, confirm=True, logs=True)["kept"])
    assert log.read_text() == "from the backup"
    assert (kept / "logs" / "run-1" / "result.md").read_text() == "since then"


def test_a_restore_reaches_a_connection_another_process_holds_open(o: Orchestrator) -> None:
    finished_run(o, "fix-login-3f2a", "one")
    name = o.backup_state()["name"]
    finished_run(o, "fix-login-3f2a", "two")
    dashboard = Store()  # like a running dashboard's
    held = sqlite3.connect(config.db_path())
    assert [row[0] for row in held.execute("SELECT prompt FROM runs")] == ["one", "two"]

    o.restore_state(name, confirm=True)
    assert [row[0] for row in held.execute("SELECT prompt FROM runs")] == ["one"]
    assert [run.prompt for run in dashboard.runs_of("fix-login-3f2a")] == ["one"]
    held.close()
    conn = sqlite3.connect(config.db_path())
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    conn.close()


def test_turns_active_in_the_backup_end_stopped_and_run_ids_are_not_reused(
    o: Orchestrator,
) -> None:
    active = o.store.add_run("fix-login-3f2a", "was running")
    o.store.update_run(active.id, status=RunStatus.RUNNING, pid=os.getpid())
    queued = o.store.add_run("fix-login-3f2a", "was queued")
    name = o.backup_state()["name"]
    for run in (active, queued):
        o.store.finish_run(run.id, RunStatus.DONE)
    later = [finished_run(o, "fix-login-3f2a", f"later {n}") for n in range(3)]

    o.restore_state(name, confirm=True)
    for run in (active, queued):
        restored = o.store.get_run(run.id)
        assert restored.status == RunStatus.STOPPED and restored.error == state.STOPPED_NOTE
        assert o.store.meta(run.id).checked == 1  # wakes no parent
    assert o.store.add_run("fix-login-3f2a", "next").id == max(later) + 1


def test_a_backup_of_an_older_version_restores_and_opens(o: Orchestrator) -> None:
    finished_run(o, "fix-login-3f2a", "old")
    old = Path(o.backup_state()["database"])
    conn = sqlite3.connect(old)
    conn.execute("ALTER TABLE run_usage DROP COLUMN context_tokens")
    conn.execute("DROP TABLE board_blockers")
    conn.execute("DROP TABLE run_meta")
    conn.commit()
    conn.close()
    finished_run(o, "fix-login-3f2a", "new")

    result = o.restore_state(str(old.parent), confirm=True)
    assert result["newer"] is None
    store = Store()
    assert [run.prompt for run in store.runs_of("fix-login-3f2a")] == ["old"]
    conn = sqlite3.connect(config.db_path())
    columns = {row[1] for row in conn.execute("PRAGMA table_info(run_usage)")}
    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master")}
    conn.close()
    assert "context_tokens" in columns and {"board_blockers", "run_meta"} <= tables


def test_a_backup_of_a_newer_version_is_restored_with_a_note(o: Orchestrator) -> None:
    finished_run(o, "fix-login-3f2a", "from the future")
    newer = Path(o.backup_state()["database"])
    conn = sqlite3.connect(newer)
    conn.execute("CREATE TABLE future_things (id INTEGER PRIMARY KEY)")
    conn.execute("ALTER TABLE runs ADD COLUMN future_flag INTEGER")
    conn.commit()
    conn.close()

    with pytest.raises(NeedsConfirmation, match="future_things, runs.future_flag"):
        o.restore_state(str(newer))
    result = invoke("state", "restore", str(newer), "--yes")
    assert result.exit_code == 0 and "a newer version made it" in result.stderr
    assert prompts() == ["from the future"]
    assert Store().get_agent("fix-login-3f2a") is not None
    finished_run(o, "fix-login-3f2a", "after")  # this version keeps writing to it


# -- boundaries --------------------------------------------------------------------------------

AUDITED = {
    "open", "sqlite3.connect", "os.mkdir", "os.chmod", "os.rename", "os.remove", "os.rmdir",
    "os.listdir", "os.scandir", "os.symlink", "os.utime", "shutil.copyfile", "shutil.copytree",
}  # fmt: skip
recorded: list[tuple[str, str]] | None = None
recording_thread: int | None = None  # threads other tests left running are not this test's


def audit(event: str, args: tuple) -> None:
    if recorded is None or event not in AUDITED or threading.get_ident() != recording_thread:
        return
    for arg in args[:2] if event.startswith("shutil") else args[:1]:
        if isinstance(arg, str | bytes | os.PathLike) and os.fspath(arg) != ":memory:":
            recorded.append((event, os.fsdecode(arg)))


sys.addaudithook(audit)  # inert unless a test records


def test_backup_list_and_restore_touch_nothing_outside_the_home(o: Orchestrator) -> None:
    global recorded, recording_thread
    home = config.home().resolve()
    (home / "roles").mkdir()
    (home / "roles" / "docs-writer.md").write_text("role")
    (home / "loadout.toml").write_text("[default]\n")
    config.run_dir(1).mkdir(parents=True)
    finished_run(o, "fix-login-3f2a", "one")
    recorded, recording_thread = [], threading.get_ident()
    try:
        name = o.backup_state(logs=True)["name"]
        o.list_backups()
        o.restore_state(name, confirm=True, logs=True)
    finally:
        seen, recorded = recorded, None
    python = {Path(p).resolve() for p in (sys.prefix, sys.base_prefix, sys.exec_prefix)}
    outside = [
        (event, path)
        for event, path in seen
        if not Path(path).resolve().is_relative_to(home)
        and not any(Path(path).resolve().is_relative_to(p) for p in python | {ROOT / "src"})
    ]
    assert seen and outside == [], "\n".join(f"{event} {path}" for event, path in outside)
    assert {"sqlite3.connect", "shutil.copytree", "os.chmod"} <= {event for event, _ in seen}


def test_the_commands_name_the_configured_command(o: Orchestrator) -> None:
    for path in (["state"], ["state", "backup"], ["state", "list"], ["state", "restore"]):
        shown = invoke(*path, "--help")
        assert shown.exit_code == 0 and f"Usage: {config.CLI_NAME} state" in shown.output
        assert "{cli}" not in shown.output
    assert f"{config.CLI_NAME} state list" in invoke("state", "backup", "--help").output
    made = invoke("state", "backup")
    assert made.exit_code == 0 and made.stdout.startswith(f"backup: {config.backups_dir()}")
    assert "can include secrets" in made.stderr
    missing = invoke("state", "restore", "no-such-backup")
    assert missing.exit_code == 1
    assert f"`{config.CLI_NAME} state list`" in missing.stderr
