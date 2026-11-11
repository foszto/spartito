"""`orch web` as a background service that starts at login and restarts when it dies.

- macOS: a launchd agent, `~/Library/LaunchAgents/<label>.plist`, in the `gui/<uid>` domain;
  stdout and stderr go to `$ORCH_HOME/logs/web.log` (cut when above LOG_LIMIT at install).
- Linux: a systemd user unit, `$XDG_CONFIG_HOME/systemd/user/<label>.service`; its output
  goes to the journal.

The label is `local.spartito.web` for the default home and carries a hash of ORCH_HOME
otherwise, so a temporary home never touches the real service. `launchctl` and `systemctl`
are called by bare name through PATH. Of `launchctl print`, whose output is not an API, only
the exit code and the `pid =` line are read.

The unit carries the PATH of the install and a whitelist of variables (ENV_NAMES,
ENV_PREFIXES), never anything else: unit files are world-readable.
"""

from __future__ import annotations

import hashlib
import json
import os
import plistlib
import shlex
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from . import config

DEFAULT_HOME = "~/.orchestrator"  # config.home() without ORCH_HOME
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8787
ENV_NAMES = ("PATH", "LANG", "CLAUDE_CONFIG_DIR", "CODEX_HOME")
ENV_PREFIXES = ("ORCH_", "LC_")
# Markers of the calling process (an agent, a turn), not settings: never carried over.
ENV_EXCLUDED = ("ORCH_AGENT_ID", "ORCH_TURN")
LOG_LIMIT = 5 * 1024 * 1024  # bytes; launchd does not rotate the log
PING_TIMEOUT = 2.0  # seconds
BOOTOUT_WAIT = 5.0  # seconds for launchd to drop the old job before the new one is loaded


class AutostartError(Exception):
    """A clear one-line reason the service cannot be managed here."""


@dataclass(frozen=True)
class Service:
    """Everything a unit file says."""

    label: str
    argv: list[str]
    env: dict[str, str]
    home: Path
    log: Path


@dataclass(frozen=True)
class Status:
    platform: str
    label: str
    unit: Path
    installed: bool  # the unit file exists
    loaded: bool  # launchd: loaded in the gui domain; systemd: active
    pid: int | None
    host: str | None
    port: int | None
    answer: str  # "ours", "foreign" or "none" (or "unknown": no host and port to ask)
    detail: str  # why "foreign"
    logs: str  # the log file, or the journalctl command


def platform_kind(platform: str = sys.platform) -> str:
    """`launchd` or `systemd`; AutostartError elsewhere."""
    if platform == "darwin":
        return "launchd"
    if platform.startswith("linux"):
        return "systemd"
    raise AutostartError(f"the background service is supported on macOS and Linux, not {platform}")


def home() -> Path:
    return config.home().resolve()


def home_hash(path: Path) -> str:
    """sha256 of the resolved home: the first 16 hex characters are what /api/ping reports."""
    return hashlib.sha256(str(path).encode()).hexdigest()


def default_label(path: Path) -> str:
    if path == Path(DEFAULT_HOME).expanduser().resolve():
        return "local.spartito.web"
    return f"local.spartito-{home_hash(path)[:8]}.web"


def web_argv(host: str, port: int) -> list[str]:
    return [sys.executable, "-m", "orchestrator", "web", "--host", host, "--port", str(port)]


def captured_env(path: Path, environ: dict[str, str] | None = None) -> dict[str, str]:
    """The whitelisted variables of the install, with ORCH_HOME set to the resolved home."""
    environ = dict(os.environ if environ is None else environ)
    env = {
        name: value
        for name, value in environ.items()
        if (name in ENV_NAMES or name.startswith(ENV_PREFIXES)) and name not in ENV_EXCLUDED
    }
    env["ORCH_HOME"] = str(path)
    return dict(sorted(env.items()))


def service(
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    label: str | None = None,
    environ: dict[str, str] | None = None,
) -> Service:
    path = home()
    return Service(
        label=label or default_label(path),
        argv=web_argv(host, port),
        env=captured_env(path, environ),
        home=path,
        log=path / "logs" / "web.log",
    )


# -- unit files -----------------------------------------------------------------------------


def render_plist(svc: Service) -> bytes:
    return plistlib.dumps(
        {
            "Label": svc.label,
            "ProgramArguments": svc.argv,
            "EnvironmentVariables": svc.env,
            "WorkingDirectory": str(svc.home),
            "RunAtLoad": True,
            "KeepAlive": True,
            "ProcessType": "Interactive",
            "StandardOutPath": str(svc.log),
            "StandardErrorPath": str(svc.log),
        },
        sort_keys=False,
    )


def systemd_quote(value: str, command: bool = False) -> str:
    """One word for systemd: `%` is a specifier and, in a command line, `$` a variable."""
    value = value.replace("%", "%%")
    if command:
        value = value.replace("$", "$$")
    if value and not any(c in value for c in " \t\n\"\\'"):
        return value
    escaped = value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
    return f'"{escaped}"'


def render_unit(svc: Service) -> str:
    exec_start = " ".join(systemd_quote(arg, command=True) for arg in svc.argv)
    env = "".join(
        f"Environment={systemd_quote(f'{name}={value}')}\n" for name, value in svc.env.items()
    )
    return (
        "[Unit]\n"
        f"Description={config.PROJECT_NAME} dashboard (orch web)\n"
        "\n"
        "[Service]\n"
        "Type=exec\n"
        f"ExecStart={exec_start}\n"
        f"WorkingDirectory={systemd_quote(str(svc.home))}\n"
        f"{env}"
        "Restart=on-failure\n"
        "RestartSec=2\n"
        "\n"
        "[Install]\n"
        "WantedBy=default.target\n"
    )


def unit_path(label: str, kind: str) -> Path:
    if kind == "launchd":
        return Path.home() / "Library" / "LaunchAgents" / f"{label}.plist"
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "systemd" / "user" / f"{label}.service"


def unit_name(label: str) -> str:
    return f"{label}.service"


def logs_hint(svc_log: Path, label: str, kind: str) -> str:
    return str(svc_log) if kind == "launchd" else f"journalctl --user -u {unit_name(label)}"


def argv_of(unit: Path, kind: str) -> list[str]:
    """The command line a written unit file runs; empty when unreadable."""
    try:
        if kind == "launchd":
            return list(plistlib.loads(unit.read_bytes()).get("ProgramArguments", []))
        for line in unit.read_text().splitlines():
            if line.startswith("ExecStart="):
                return shlex.split(line.removeprefix("ExecStart="))
    except OSError, ValueError, plistlib.InvalidFileException:
        pass
    return []


def address_of(argv: list[str]) -> tuple[str | None, int | None]:
    def option(name: str) -> str | None:
        return argv[argv.index(name) + 1] if name in argv[:-1] else None

    port = option("--port")
    return option("--host"), int(port) if port and port.isdigit() else None


# -- system commands -------------------------------------------------------------------------


def run(*argv: str) -> subprocess.CompletedProcess[str]:
    """A system command by bare name, through PATH."""
    try:
        return subprocess.run(argv, capture_output=True, text=True, check=False)
    except FileNotFoundError:
        raise AutostartError(f"`{argv[0]}` is not on PATH") from None


def checked(*argv: str) -> subprocess.CompletedProcess[str]:
    result = run(*argv)
    if result.returncode:
        why = (result.stderr or result.stdout).strip().splitlines()
        raise AutostartError(
            f"`{' '.join(argv)}` failed ({result.returncode})" + (f": {why[-1]}" if why else "")
        )
    return result


def domain() -> str:
    return f"gui/{os.getuid()}"


def launchd_pid(label: str) -> tuple[bool, int | None]:
    """(loaded, pid) from `launchctl print`: its exit code and its `pid =` line."""
    result = run("launchctl", "print", f"{domain()}/{label}")
    if result.returncode:
        return False, None
    for line in result.stdout.splitlines():
        key, _, value = line.strip().partition("=")
        if key.strip() == "pid" and value.strip().isdigit():
            return True, int(value.strip())
    return True, None


def systemd_pid(label: str) -> tuple[bool, int | None]:
    active = run("systemctl", "--user", "is-active", unit_name(label)).returncode == 0
    if not active:
        return False, None
    shown = run("systemctl", "--user", "show", "--property=MainPID", "--value", unit_name(label))
    pid = shown.stdout.strip()
    return True, int(pid) if pid.isdigit() and pid != "0" else None


def cut_log(log: Path, limit: int = LOG_LIMIT) -> bool:
    """Empty the log when it is above `limit` bytes; True when it was cut."""
    try:
        if log.stat().st_size <= limit:
            return False
    except FileNotFoundError:
        return False
    log.write_bytes(b"")
    return True


# -- install / uninstall / status ------------------------------------------------------------


def install(svc: Service, platform: str = sys.platform) -> Path:
    """Write the unit and (re)start the service with it; returns the unit file. Running it
    again replaces the unit and restarts the service."""
    kind = platform_kind(platform)
    unit = unit_path(svc.label, kind)
    unit.parent.mkdir(parents=True, exist_ok=True)
    if kind == "launchd":
        if run("launchctl", "bootout", f"{domain()}/{svc.label}").returncode == 0:
            deadline = time.monotonic() + BOOTOUT_WAIT  # bootout fails when not loaded
            while launchd_pid(svc.label)[0] and time.monotonic() < deadline:
                time.sleep(0.1)
        svc.log.parent.mkdir(parents=True, exist_ok=True)
        cut_log(svc.log)
        unit.write_bytes(render_plist(svc))
        checked("launchctl", "bootstrap", domain(), str(unit))
    else:
        was_active = run("systemctl", "--user", "is-active", unit_name(svc.label)).returncode == 0
        unit.write_text(render_unit(svc))
        checked("systemctl", "--user", "daemon-reload")
        checked("systemctl", "--user", "enable", "--now", unit_name(svc.label))
        if was_active:  # enable --now leaves a running service on its old command line
            checked("systemctl", "--user", "restart", unit_name(svc.label))
    return unit


def uninstall(label: str, platform: str = sys.platform) -> Path | None:
    """Stop the service and remove its unit; returns the removed unit file, None when there
    was nothing to remove."""
    kind = platform_kind(platform)
    unit = unit_path(label, kind)
    if kind == "launchd":
        loaded = run("launchctl", "bootout", f"{domain()}/{label}").returncode == 0
    else:
        loaded = unit.exists()
        if loaded:
            run("systemctl", "--user", "disable", "--now", unit_name(label))
    if not unit.exists():
        return unit if loaded else None
    unit.unlink()
    if kind == "systemd":
        checked("systemctl", "--user", "daemon-reload")
    return unit


def ping(host: str, port: int, path: Path, timeout: float = PING_TIMEOUT) -> tuple[str, str]:
    """Who answers on host:port: ("ours", "") for this tool with this home, ("foreign", why)
    for anything else that answers, ("none", "") when nothing does."""
    target = {"0.0.0.0": "127.0.0.1", "::": "::1", "": "127.0.0.1"}.get(host, host)
    netloc = f"[{target}]" if ":" in target else target
    try:
        with urllib.request.urlopen(f"http://{netloc}:{port}/api/ping", timeout=timeout) as resp:
            body = resp.read(65536)
    except urllib.error.HTTPError as exc:
        return "foreign", f"/api/ping answered HTTP {exc.code}"
    except OSError, ValueError:
        return "none", ""
    try:
        data = json.loads(body)
    except ValueError:
        return "foreign", "/api/ping did not answer JSON"
    if not isinstance(data, dict) or data.get("tool") != "orchestrator":
        return "foreign", "not this tool"
    if data.get("home") != home_hash(path)[:16]:
        return "foreign", "this tool, but for another ORCH_HOME"
    return "ours", ""


def status(label: str, platform: str = sys.platform) -> Status:
    kind = platform_kind(platform)
    unit = unit_path(label, kind)
    loaded, pid = (launchd_pid if kind == "launchd" else systemd_pid)(label)
    host, port = address_of(argv_of(unit, kind))
    answer, detail = ping(host, port, home()) if host and port else ("unknown", "")
    return Status(
        platform=kind,
        label=label,
        unit=unit,
        installed=unit.exists(),
        loaded=loaded,
        pid=pid,
        host=host,
        port=port,
        answer=answer,
        detail=detail,
        logs=logs_hint(home() / "logs" / "web.log", label, kind),
    )
