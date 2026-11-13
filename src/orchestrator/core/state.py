"""Backups of the state (docs/state.md): the database, copied with SQLite's online backup API,
with the home's user files (your roles, loadout.toml) and optionally the logs; listing the
backups; restoring one in place. Nothing outside the home is read or written: symbolic links
are left out of a backup, and a restore leaves them as they are. Nothing is deleted: a restore
keeps the state it replaces as a new backup. A backup is built under a hidden name
(`.<name>.incomplete`) and renamed into place once it is complete."""

from __future__ import annotations

import contextlib
import os
import shutil
import sqlite3
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
INCOMPLETE = ".incomplete"  # `.<name>.incomplete`: a backup being built, never listed
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
    final, work = start(dest, logs, suffix)
    try:
        skipped = copy_files(work, logs)
        database = new_file(work / db_name())
        store.backup(database)
        try:
            check_database(database)
        except ValueError as exc:
            raise OrchestratorError(f"the backup is not usable: {exc}") from None
        return finish(final, work, skipped)
    except OSError as exc:
        abandon(final, work)
        raise OrchestratorError(f"the backup failed: {exc}") from None
    except BaseException:
        abandon(final, work)
        raise


def stamp() -> str:
    return datetime.now(UTC).strftime(STAMP)


def start(dest: Path | None, logs: bool, suffix: str | None) -> tuple[Path, Path]:
    """Reserve the backup's directory `final` (an empty one: `list` and `restore` do not take
    it) and create the hidden directory `work` the backup is built in, both 0700. Without
    `dest`: the first free name `<UTC time>[-suffix][-2, -3, …]` in the home's backups/."""
    if dest is None:
        parent = config.backups_dir()
        parent.mkdir(mode=0o700, exist_ok=True)
        base, number = stamp() + (f"-{suffix}" if suffix else ""), 1
        while True:
            final = parent / (base if number == 1 else f"{base}-{number}")
            try:
                final.mkdir(mode=0o700)
                break
            except FileExistsError:
                number += 1
    else:
        final = dest.absolute()
        refuse_inside(final, logs)
        final.parent.mkdir(parents=True, exist_ok=True)
        try:
            final.mkdir(mode=0o700)
        except FileExistsError:
            raise OrchestratorError(
                f"{final} exists already; a backup goes into a new directory"
            ) from None
    work = final.parent / f".{final.name}{INCOMPLETE}"
    try:
        work.mkdir(mode=0o700)
    except OSError:
        final.rmdir()
        raise
    work.chmod(0o700)
    return final, work


def refuse_inside(dest: Path, logs: bool) -> None:
    """A backup must not go into a directory it copies (it would copy itself)."""
    home = config.home()
    target = dest.resolve()
    for name in (ROLES, *([LOGS] if logs else [])):
        if target.is_relative_to((home / name).resolve()):
            raise OrchestratorError(
                f"{dest} is inside {home / name}, which the backup copies; choose another place"
            )


def new_file(path: Path) -> Path:
    os.close(os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600))
    return path


def copy_files(work: Path, logs: bool) -> list[str]:
    """Copy the home's roles/, loadout.toml and with `logs` logs/ into `work`; returns the
    symbolic links left out."""
    home = config.home()
    skipped: list[str] = []
    for name in (ROLES, LOADOUT, *([LOGS] if logs else [])):
        copy_in(home / name, work / name, skipped)
    return skipped


def finish(final: Path, work: Path, skipped: list[str]) -> dict[str, Any]:
    """Lock the complete backup down and rename it into its reserved place."""
    lock_down(work)
    os.rename(work, final)  # replaces the empty reserved directory
    return {
        "path": str(final),
        "name": final.name,
        "database": str(final / db_name()),
        "files": [name for name in (ROLES, LOADOUT, LOGS) if (final / name).exists()],
        "logs": (final / LOGS).is_dir(),
        "size": size_of(final),
        "skipped": skipped,
        "warning": WARNING,
    }


def abandon(final: Path, work: Path) -> None:
    """Remove what a backup that failed made: its hidden directory (copies only) and the
    reservation of its name."""
    shutil.rmtree(work, ignore_errors=True)
    with contextlib.suppress(OSError):
        final.rmdir()


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
        if entry.is_symlink() or entry.name.startswith("."):  # links; backups being made
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
    if path.name.endswith(INCOMPLETE):
        raise OrchestratorError(f"{path} is a backup that was never completed")
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


def refuse_busy(orch: Facade) -> None:
    if runs := busy(orch):
        what = ", ".join(f"{run.agent_id} run {run.id} {run.status}" for run in runs)
        raise OrchestratorError(
            f"not restored: turns are running or queued ({what}); wait for them, or"
            f" `{config.CLI_NAME} stop` the agents, then restore again"
        )


def restore(orch: Facade, backup: str, confirm: bool = False, logs: bool = False) -> dict[str, Any]:
    """Make the state that of a backup (see `find`). Refuses a file that is not an intact
    state database, and while any turn is running or queued. Without `confirm` it changes
    nothing and raises NeedsConfirmation saying what it would do. Else it keeps the current
    state as a new backup (`<time>-before-restore`): first the user files (with `logs` the
    logs), then, under one write lock with the busy check repeated and the replace, the
    database (Store.replace_from). Then it copies the backup's roles/ and loadout.toml (with
    `logs` also its logs/) over the home's, never through a symbolic link: files the backup
    lacks stay."""
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
    if not confirm:
        raise NeedsConfirmation(plan(root or database, live, files, logs, newer_note(unknown)))
    final, work = start(None, logs, BEFORE_RESTORE)
    try:
        skipped = copy_files(work, logs)
        snapshot = new_file(work / db_name())
        left_out = orch.store.replace_from(
            database, snapshot, STOPPED_NOTE, lambda: refuse_busy(orch)
        )
    except ValueError as exc:
        abandon(final, work)
        raise OrchestratorError(
            f"not restored: the current state could not be kept: {exc}"
        ) from None
    except (OSError, sqlite3.Error) as exc:
        abandon(final, work)
        raise OrchestratorError(f"not restored, nothing changed: {exc}") from None
    except BaseException:
        abandon(final, work)
        raise
    kept = finish(final, work, skipped)
    home, left = config.home(), []
    for name in files:
        assert root is not None  # files come only from a backup directory
        put_back(root / name, home, home / name, left)
    return {
        "restored": str(root or database),
        "kept": kept["path"],
        "kept_name": kept["name"],
        "kept_skipped": skipped,
        "files": files,
        "left": left,
        "newer": newer_note(left_out, root or database),
        "restart": RESTART,
    }


def newer_note(unknown: list[str], kept_in: Path | None = None) -> str | None:
    if not unknown:
        return None
    shown = ", ".join(unknown[:6]) + (", …" if len(unknown) > 6 else "")
    where = f" {kept_in}" if kept_in else ""
    return (
        f"the backup has tables or columns this version does not have ({shown}): a newer"
        f" version made it. They are not restored; they stay in the backup{where}, for the"
        " version that knows them."
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


def put_back(source: Path, home: Path, dest: Path, left: list[str]) -> None:
    """Copy a file or directory of a backup to `dest` in the home, never through a symbolic
    link: a destination that is one, or lies below one (from the home down), is left as it
    is and noted in `left`; so is a source entry that is one."""

    def through_link(path: Path) -> bool:
        parts = path.relative_to(home).parts
        return any((home.joinpath(*parts[: n + 1])).is_symlink() for n in range(len(parts)))

    def put(src: Path, dst: Path) -> None:
        if src.is_symlink():
            left.append(f"{src}: a symbolic link in the backup, not restored")
        elif through_link(dst):
            left.append(f"{dst}: a symbolic link (or below one), left as it is")
        elif src.is_dir():
            if dst.exists() and not dst.is_dir():
                left.append(f"{dst}: not a directory, left as it is")
                return
            dst.mkdir(exist_ok=True)
            for child in sorted(src.iterdir()):
                put(child, dst / child.name)
        elif dst.is_dir():
            left.append(f"{dst}: a directory, left as it is")
        else:
            flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW
            with open(os.open(dst, flags, 0o666), "wb") as out, src.open("rb") as data:
                shutil.copyfileobj(data, out)

    put(source, dest)
