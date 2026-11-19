"""Where a provider's CLI is, and whether it starts: no network, no tokens, no credentials.

A process started from a background service, a launcher or a desktop app may have a thinner
PATH than a terminal (the service keeps the PATH of its install). The CLIs are often node
scripts behind `#!/usr/bin/env node`, and npm, Homebrew, Volta and bun put `node` next to
the CLI's link; so the usual per-user and Homebrew directories are searched after PATH, and
a CLI is started with them appended to its PATH.
"""

from __future__ import annotations

import contextlib
import os
import re
import shutil
import signal
import subprocess
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from .. import config

# Searched after PATH, in this order; only the ones that exist.
EXTRA_DIRS: tuple[str, ...] = (
    "~/.local/bin",
    "/opt/homebrew/bin",
    "/usr/local/bin",
    "~/.npm-global/bin",
    "~/.volta/bin",
    "~/.bun/bin",
)
PROBE_SECONDS = 5.0
PIPES_SECONDS = 1.0  # after a timeout's kill: how long the output may still take to close
SHEBANG_BYTES = 256
VERSION = re.compile(r"\d+(?:\.\d+)+")

AVAILABLE = "available"
NOT_INSTALLED = "not_installed"
NOT_LOGGED_IN = "not_logged_in"
UNKNOWN = "unknown"
STATUSES = (AVAILABLE, NOT_INSTALLED, NOT_LOGGED_IN, UNKNOWN)
LOGIN_NOT_CHECKED = "login not checked"


@dataclass(frozen=True)
class Probe:
    status: str  # one of STATUSES
    version: str | None
    path: str | None
    reason: str  # one line


def tilde(path: str | Path) -> str:
    """`path` with the home directory written as `~`."""
    text, home = str(path), str(Path.home())
    if home not in ("", "/") and (text == home or text.startswith(home + os.sep)):
        return "~" + text[len(home) :]
    return text


def extra_dirs() -> list[str]:
    found = []
    for entry in EXTRA_DIRS:
        path = os.path.expanduser(entry)
        if os.path.isdir(path) and path not in found:
            found.append(path)
    return found


def search_path(path: str | None = None) -> str:
    """`path` (default: this process's PATH) with the existing EXTRA_DIRS appended that it
    does not hold yet: the user's own order wins."""
    if path is None:
        path = os.environ.get("PATH", "")
    parts = [part for part in path.split(os.pathsep) if part]
    added = [extra for extra in extra_dirs() if extra not in parts]
    return os.pathsep.join([*parts, *added])


def find(executable: str) -> tuple[Path | None, bool]:
    """Where `executable` is, and whether it was found on the plain PATH."""
    found = shutil.which(executable)
    if found:
        return Path(found), True
    found = shutil.which(executable, path=search_path())
    return (Path(found), False) if found else (None, False)


def interpreter_problem(path: Path) -> str | None:
    """Why the script at `path` cannot start: its `#!` interpreter is missing. Only the first
    line is read; a binary or a script with a usable interpreter gives None."""
    try:
        with path.open("rb") as file:
            head = file.read(SHEBANG_BYTES)
    except OSError:
        return None
    if not head.startswith(b"#!"):
        return None
    words = head[2:].split(b"\n", 1)[0].decode(errors="replace").split()
    if not words:
        return None
    interpreter = words[0]
    if os.path.basename(interpreter) == "env":
        named = next((word for word in words[1:] if not word.startswith("-")), None)
        if named is None or "=" in named:
            return None
        if shutil.which(named, path=search_path()) is None:
            return f"found {tilde(path)}, but its interpreter `{named}` is not on PATH"
        return None
    if not os.path.exists(interpreter):
        return f"found {tilde(path)}, but its interpreter {interpreter} does not exist"
    return None


def not_found(executable: str) -> str:
    """Why `executable` is not installed, in one short clause (the reason of a probe)."""
    return f"`{executable}` was not found on PATH or in the usual install locations"


def install_advice() -> str:
    """What to do after installing a CLI that a background service or launcher does not
    find (`{cli} providers` prints it below the table)."""
    cli = config.CLI_NAME
    return (
        "A background service or launcher keeps the PATH of its install: after installing a"
        f" CLI, run `{cli} service install` or `{cli} app install` again."
    )


def says_more() -> str:
    return f"`{config.CLI_NAME} providers` says more"


def run_cli(
    argv: Sequence[str],
    timeout: float,
    stdin: str | None = None,
    env: Mapping[str, str] | None = None,
    cwd: str | Path | None = None,
) -> tuple[int, str, str]:
    """Exit code, stdout and stderr of a brief CLI call (no shell). `stdin`: its input, none
    when None. `env`: its environment (default: this process's), with the usual install
    directories appended to its PATH (search_path).

    It runs in a process group of its own, which is killed as a whole on a timeout
    (subprocess.TimeoutExpired is raised). A child that left the group may keep the output
    open: it is waited for PIPES_SECONDS at most.
    """
    env = dict(os.environ if env is None else env)
    env["PATH"] = search_path(env.get("PATH"))
    proc = subprocess.Popen(
        list(argv),
        cwd=cwd,
        env=env,
        stdin=subprocess.DEVNULL if stdin is None else subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        errors="replace",
        start_new_session=True,
    )
    try:
        out, err = proc.communicate(stdin, timeout=timeout)
    except subprocess.TimeoutExpired:
        # macOS answers EPERM for a group that has just exited but is not reaped yet; the
        # CLI must be reaped either way.
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(proc.pid, signal.SIGKILL)
        try:
            proc.communicate(timeout=PIPES_SECONDS)
        except subprocess.TimeoutExpired:  # held open by a child outside the group
            for pipe in (proc.stdout, proc.stderr):
                if pipe is not None:
                    pipe.close()
            proc.wait()  # the CLI itself was killed with its group
        raise
    return proc.returncode, out, err


def _last_line(text: str) -> str:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    return lines[-1][:200] if lines else ""


def probe(
    executable: str,
    version_args: tuple[str, ...] = ("--version",),
    login_args: tuple[str, ...] | None = None,
    timeout: float = PROBE_SECONDS,
    logged_in: Callable[[str], bool | None] | None = None,
) -> Probe:
    """Whether `executable` is there and answers `version_args`; with `login_args`, whether
    that command exits 0 (logged in). `logged_in`: reads that command's output as well
    (False: not logged in; None: it cannot tell, the login counts as not checked). Runs
    nothing else."""
    path, on_path = find(executable)
    if path is None:
        return Probe(NOT_INSTALLED, None, None, not_found(executable))
    if problem := interpreter_problem(path):
        return Probe(NOT_INSTALLED, None, str(path), problem)
    asked = " ".join([executable, *version_args])
    try:
        code, out, err = run_cli([str(path), *version_args], timeout)
    except subprocess.TimeoutExpired:
        reason = f"`{asked}` did not answer within {timeout:g} s"
        return Probe(UNKNOWN, None, str(path), reason)
    except OSError as exc:
        reason = f"found {tilde(path)}, but it could not be started: {exc.strerror or exc}"
        return Probe(NOT_INSTALLED, None, str(path), reason)
    if code:
        said = _last_line(err) or _last_line(out)
        reason = f"`{asked}` exited with code {code}" + (f": {said}" if said else "")
        return Probe(UNKNOWN, None, str(path), reason)
    match = VERSION.search(out) or VERSION.search(err)
    version = match.group() if match else None
    where = "found on PATH" if on_path else f"found in {tilde(path.parent)}, not on PATH"
    if not login_args:
        return Probe(AVAILABLE, version, str(path), f"{where}; {LOGIN_NOT_CHECKED}")
    try:
        code, out, err = run_cli([str(path), *login_args], timeout)
    except subprocess.TimeoutExpired, OSError:
        return Probe(UNKNOWN, version, str(path), f"{where}; the login check did not answer")
    if code:
        said = _last_line(err) or _last_line(out)
        reason = f"{where}; not logged in" + (f": {said}" if said else "")
        return Probe(NOT_LOGGED_IN, version, str(path), reason)
    said = True if logged_in is None else logged_in(out)
    if said is None:
        return Probe(AVAILABLE, version, str(path), f"{where}; {LOGIN_NOT_CHECKED}")
    if not said:
        reason = f"{where}; not logged in: `{' '.join([executable, *login_args])}` lists none"
        return Probe(NOT_LOGGED_IN, version, str(path), reason)
    return Probe(AVAILABLE, version, str(path), f"{where}; logged in")
