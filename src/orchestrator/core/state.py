"""Backups of the state (docs/state.md): the database, copied with SQLite's online backup API,
with the home's user files (your roles, loadout.toml) and optionally the logs; listing the
backups; restoring one in place. Nothing outside the home is read: symbolic links are left
out. Nothing is deleted: a restore first keeps the state it replaces as a new backup."""

from __future__ import annotations

import os
import shutil
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from .. import config
from ..errors import NeedsConfirmation, OrchestratorError
from ..models import Run
from ..store import Store, check_database, pid_alive

ROLES, LOADOUT, LOGS = "roles", "loadout.toml", "logs"
STAMP = "%Y%m%dT%H%M%SZ"  # a backup directory's name: when it was made, in UTC
BEFORE_RESTORE = "before-restore"  # the name's suffix of the state a restore replaced
WARNING = (
    "a backup holds prompts, answers and tool output, which can include secrets: keep it private"
)
STOPPED_NOTE = "stopped: the state was restored from a backup taken while this turn was active"
RESTART = (
    f"A running dashboard (`{config.CLI_NAME} web`, the background service, `{config.CLI_NAME}"
    " app`) works on the restored database but may still show what it held before: restart"
    f" it (the service: `{config.CLI_NAME} service install` again)."
)


class Facade(Protocol):
    """What these functions use of the facade (they are its methods)."""

    store: Store


def db_name() -> str:
    return config.db_path().name


def size_of(path: Path) -> int:
    if path.is_file():
        return path.stat().st_size
    return sum(item.stat().st_size for item in path.rglob("*") if item.is_file())


# -- backup ------------------------------------------------------------------------------------


def backup(orch: Facade, to: str | Path | None = None, logs: bool = False) -> dict[str, Any]:
    """Back up the state into the new directory `to` (default: a new one named after the UTC
    time in the home's backups/): the database, the home's roles/ and loadout.toml when they
    exist, and with `logs` the logs/ directory. The directory is 0700, its files 0600; the
    copied database must pass `PRAGMA integrity_check`."""
    return make_backup(orch.store, Path(to).expanduser() if to else None, logs)


def make_backup(
    store: Store, dest: Path | None, logs: bool, suffix: str | None = None
) -> dict[str, Any]:
    if dest is None:
        parent = config.backups_dir()
        parent.mkdir(mode=0o700, exist_ok=True)
        dest = fresh_name(parent, suffix)
    dest = dest.absolute()
    if dest.exists() or dest.is_symlink():
        raise OrchestratorError(f"{dest} exists already; a backup goes into a new directory")
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.mkdir(mode=0o700)
    dest.chmod(0o700)
    database = dest / db_name()
    os.close(os.open(database, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600))
    store.backup(database)
    try:
        check_database(database)
    except ValueError as exc:
        raise OrchestratorError(f"the backup in {dest} is not usable: {exc}") from None
    home = config.home()
    skipped: list[str] = []
    for name in (ROLES, LOADOUT, *([LOGS] if logs else [])):
        copy_in(home / name, dest / name, skipped)
    lock_down(dest)
    return {
        "path": str(dest),
        "name": dest.name,
        "database": str(database),
        "files": [name for name in (ROLES, LOADOUT, LOGS) if (dest / name).exists()],
        "logs": (dest / LOGS).is_dir(),
        "size": size_of(dest),
        "skipped": skipped,
        "warning": WARNING,
    }


def fresh_name(parent: Path, suffix: str | None) -> Path:
    """`<UTC time>[-suffix]`, with `-2`, `-3`, … when that is taken."""
    base = datetime.now(UTC).strftime(STAMP) + (f"-{suffix}" if suffix else "")
    path, number = parent / base, 1
    while path.exists() or path.is_symlink():
        number += 1
        path = parent / f"{base}-{number}"
    return path


def copy_in(source: Path, dest: Path, skipped: list[str]) -> None:
    """Copy a file or directory of the home into a backup, leaving out symbolic links (they
    may point outside the home)."""
    if source.is_symlink():
        skipped.append(str(source))
        return

    def links(folder: str, names: list[str]) -> list[str]:
        found = [name for name in names if os.path.islink(os.path.join(folder, name))]
        skipped.extend(os.path.join(folder, name) for name in found)
        return found

    if source.is_dir():
        shutil.copytree(source, dest, ignore=links, copy_function=shutil.copyfile)
    elif source.is_file():
        shutil.copyfile(source, dest)


def lock_down(root: Path) -> None:
    """Directories 0700, files 0600."""
    root.chmod(0o700)
    for folder, dirs, files in os.walk(root):
        for name in dirs:
            os.chmod(os.path.join(folder, name), 0o700)
        for name in files:
            os.chmod(os.path.join(folder, name), 0o600)


# -- list --------------------------------------------------------------------------------------


def listing(orch: Facade | None = None) -> list[dict[str, Any]]:
    """The backups in the home's backups/, newest first: the directories that hold a
    database, and database files put there by hand (`*.db`)."""
    parent = config.backups_dir()
    if not parent.is_dir():
        return []
    found = []
    for entry in parent.iterdir():
        if entry.is_symlink():
            continue
        if entry.is_dir() and (entry / db_name()).is_file():
            database, logs = entry / db_name(), (entry / LOGS).is_dir()
        elif entry.is_file() and entry.suffix == ".db":
            database, logs = entry, False
        else:
            continue
        found.append(
            {
                "name": entry.name,
                "path": str(entry),
                "kind": "directory" if entry.is_dir() else "file",
                "time": made_at(entry, database),
                "size": size_of(entry),
                "logs": logs,
            }
        )
    return sorted(found, key=lambda item: (item["time"], item["name"]), reverse=True)


def made_at(entry: Path, database: Path) -> str:
    """When the backup was made (UTC, ISO): from a name made by `backup`, else the time the
    database file was last written."""
    try:
        when = datetime.strptime(entry.name[:16], STAMP).replace(tzinfo=UTC)
    except ValueError:
        when = datetime.fromtimestamp(database.stat().st_mtime, UTC)
    return when.isoformat(timespec="seconds")


# -- restore -----------------------------------------------------------------------------------


def find(backup: str) -> tuple[Path, Path | None]:
    """The database of a backup named in the home's backups/, or of a path: a backup
    directory or a database file. Returns it and the backup directory (None: a file)."""
    named = config.backups_dir() / backup
    plain = backup not in ("", ".", "..") and "/" not in backup
    path = named if plain and (named.exists() or named.is_symlink()) else Path(backup).expanduser()
    if path.is_dir():
        database = path / db_name()
        if not database.is_file():
            raise OrchestratorError(f"{path} holds no {db_name()}: it is not a backup")
        return database, path
    if path.is_file():
        return path, None
    raise OrchestratorError(
        f"no backup '{backup}': neither a name that `{config.CLI_NAME} state list` shows nor a"
        " directory or database file"
    )


def busy(orch: Facade) -> list[Run]:
    """The queued and running turns whose runner process is alive or not yet known (the
    others ended with their runner)."""
    return [run for run in orch.store.active_runs() if run.pid is None or pid_alive(run.pid)]


def refuse_busy(orch: Facade, kept: str | None = None) -> None:
    if runs := busy(orch):
        what = ", ".join(f"{run.agent_id} run {run.id} {run.status}" for run in runs)
        also = f"; the current state was kept anyway in {kept}" if kept else ""
        raise OrchestratorError(
            f"not restored: turns are running or queued ({what}); wait for them, or"
            f" `{config.CLI_NAME} stop` the agents, then restore again{also}"
        )


def restore(orch: Facade, backup: str, confirm: bool = False, logs: bool = False) -> dict[str, Any]:
    """Make the state that of a backup (see `find`). Refuses a file that is not an intact
    state database, and while any turn is running or queued. Without `confirm` it changes
    nothing and raises NeedsConfirmation saying what it would do. Else it first keeps the
    current state as a new backup (`<time>-before-restore`, with the logs when `logs`), then
    copies the backup's database into the live one in place (Store.replace_from), adds what
    an older version's database lacks, and copies the backup's roles/ and loadout.toml (with
    `logs` also its logs/) over the home's: files the backup lacks stay."""
    database, root = find(backup)
    live = Path(orch.store.path)
    if database.resolve() == live.resolve():
        raise OrchestratorError(f"{database} is the live database")
    try:
        unknown = check_database(database)
    except ValueError as exc:
        raise OrchestratorError(f"not restored: {exc}") from None
    refuse_busy(orch)
    files = [name for name in (ROLES, LOADOUT) if root and (root / name).exists()]
    if logs and root and (root / LOGS).is_dir():
        files.append(LOGS)
    newer = newer_note(unknown)
    if not confirm:
        raise NeedsConfirmation(plan(root or database, live, files, logs, newer))
    kept = make_backup(orch.store, None, logs, BEFORE_RESTORE)
    orch.store.replace_from(database, STOPPED_NOTE, lambda: refuse_busy(orch, kept["path"]))
    Store(live)  # adds the tables and columns an older version's database lacks
    home = config.home()
    for name in files:
        assert root is not None  # files come only from a backup directory
        put_back(root / name, home / name)
    return {
        "restored": str(root or database),
        "kept": kept["path"],
        "kept_name": kept["name"],
        "files": files,
        "newer": newer,
        "restart": RESTART,
    }


def newer_note(unknown: list[str]) -> str | None:
    if not unknown:
        return None
    shown = ", ".join(unknown[:6]) + (", …" if len(unknown) > 6 else "")
    return (
        f"the backup has tables or columns this version does not know ({shown}): a newer"
        " version made it. This version works with it without reading them (rows it writes get"
        " their defaults); a newer version uses them again."
    )


def plan(source: Path, live: Path, files: list[str], logs: bool, newer: str | None) -> str:
    lines = [
        f"would restore {source}:",
        f"- first keep the current state as a new backup in {config.backups_dir()}"
        f" (`<time>-{BEFORE_RESTORE}`{', with the logs' if logs else ''})",
        f"- replace the content of {live} with the backup's database, in place; turns that"
        " were queued or running in the backup end as stopped",
    ]
    if files:
        lines.append(
            f"- copy the backup's {', '.join(files)} over the home's (files the backup lacks stay)"
        )
    if logs and LOGS not in files:
        lines.append("- the backup has no logs: the home's logs/ stays as it is")
    if newer:
        lines.append(f"- note: {newer}")
    return "\n".join(lines)


def put_back(source: Path, dest: Path) -> None:
    if source.is_dir():
        shutil.copytree(source, dest, dirs_exist_ok=True, copy_function=shutil.copyfile)
    else:
        shutil.copyfile(source, dest)
