"""Safe upgrades (docs/usage.md#upgrading): the version and the kind of install, an explicit
check for a newer release on PyPI, and the preparation of an upgrade.

Nothing here runs on its own: the check is the only network call Spartito makes, and only
when someone types `spartito version --check`. `prepare` never runs a package manager or git:
it refuses while turns are active, backs the state up (core/state.py), names the command for
this kind of install and lists what must be restarted afterwards.
"""

from __future__ import annotations

import contextlib
import fcntl
import http.client
import importlib.util
import json
import re
import shlex
import sys
import tomllib
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass
from importlib import metadata
from pathlib import Path
from typing import Any, Protocol

from .. import __version__, autostart, config
from ..errors import OrchestratorError
from ..store import Store, pid_alive
from . import state

DISTRIBUTION = "spartito"  # pyproject's [project] name
INDEX = "https://pypi.org/pypi"  # PyPI's JSON API: <INDEX>/<name>/json
USER_AGENT = "spartito"  # a fixed word: no version, platform or anything else
TIMEOUT = 10.0  # seconds
READ_LIMIT = 512 * 1024  # bytes of PyPI's answer at most
EXTRAS = "app,menubar"  # the extras the docs install
# Files of config.runtime_dir() that live processes keep: web/serve.py (the dashboard server's
# record), gui/window.py (the lock of `spartito app` and the port of its own server) and
# gui/menubar.py (its lock). tests/test_upgrade.py checks that the names match.
WEB_RECORD = "web.json"
APP_LOCK = "app.lock"
APP_PORT = "app-port"
MENUBAR_LOCK = "menubar.lock"

# The index answers with no redirect (another host or not): one is an error, never followed.
OPENER = urllib.request.build_opener(autostart.NoRedirect)


class Facade(Protocol):
    store: Store


# -- the install ---------------------------------------------------------------------------------


@dataclass(frozen=True)
class Install:
    kind: str  # "editable", "package", or "unknown" (not installed as DISTRIBUTION)
    checkout: str | None  # editable: the checkout's directory
    source: str | None  # package: the URL it was installed from, None: an index (PyPI)
    tool: bool  # a `uv tool install` (its environment holds uv-receipt.toml)
    extras: tuple[str, ...]  # the extras of a uv tool install, from its receipt

    @property
    def label(self) -> str:
        return {
            "editable": "editable checkout",
            "package": "installed package",
            "unknown": f"not installed as the distribution {DISTRIBUTION}",
        }[self.kind]


def distribution() -> metadata.Distribution:
    return metadata.distribution(DISTRIBUTION)


def environment() -> Path:
    """The Python environment this runs in."""
    return Path(sys.prefix)


def python() -> str:
    """The Python interpreter this runs in."""
    return sys.executable


def has_pip() -> bool:
    return importlib.util.find_spec("pip") is not None


def direct_url(dist: metadata.Distribution) -> dict[str, Any]:
    """The PEP 610 record of an install from a URL or directory ({}: from an index)."""
    try:
        data = json.loads(dist.read_text("direct_url.json") or "{}")
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def file_path(url: str) -> str | None:
    parsed = urllib.parse.urlparse(url)
    return urllib.request.url2pathname(parsed.path) if parsed.scheme == "file" else None


def receipt_extras(prefix: Path) -> tuple[bool, tuple[str, ...]]:
    """Whether `prefix` is a uv tool environment, and the extras its receipt names."""
    receipt = prefix / "uv-receipt.toml"
    if not receipt.is_file():
        return False, ()
    try:
        requirements = tomllib.loads(receipt.read_text()).get("tool", {}).get("requirements", [])
    except OSError, ValueError:
        return True, ()
    for requirement in requirements if isinstance(requirements, list) else []:
        if isinstance(requirement, dict) and requirement.get("name") == DISTRIBUTION:
            extras = requirement.get("extras") or []
            return True, tuple(str(e) for e in extras if isinstance(e, str))
    return True, ()


def install() -> Install:
    """What kind of install runs this code."""
    try:
        dist = distribution()
    except metadata.PackageNotFoundError:
        return Install("unknown", None, None, False, ())
    tool, extras = receipt_extras(environment())
    record = direct_url(dist)
    url = record.get("url") if isinstance(record.get("url"), str) else None
    dir_info = record.get("dir_info")
    if url and isinstance(dir_info, dict) and dir_info.get("editable"):
        return Install("editable", file_path(url) or url, None, tool, extras)
    return Install("package", None, url, tool, extras)


def version_info() -> dict[str, Any]:
    """The version and the kind of install (for the terminal: the editable checkout's path)."""
    found = install()
    return {"version": __version__, "label": found.label, **asdict(found)}


def requirement(extras: tuple[str, ...], base: str = DISTRIBUTION) -> str:
    return f"'{base}[{','.join(extras)}]'" if extras else base


def own_environment(checkout: str) -> bool:
    """Whether this runs in the checkout's own environment, `<checkout>/.venv` (`uv run`)."""
    try:
        return environment().resolve() == (Path(checkout) / ".venv").resolve()
    except OSError:
        return False


def commands(found: Install) -> tuple[list[str], list[str]]:
    """The commands that upgrade this install, and notes on them. Paths in a command are
    quoted for the shell (shlex.quote)."""
    cli = config.CLI_NAME
    if found.kind == "editable" and found.checkout:
        where = f"cd {shlex.quote(found.checkout)}"
        update = "git pull    # or update the checkout's branch as you do"
        if found.tool:
            return [
                where,
                update,
                f"uv tool install --editable {requirement(found.extras, '.')}",
            ], ["the `uv tool install` line reinstalls the checkout with the extras it has now"]
        if own_environment(found.checkout):
            return [where, update, "uv sync"], [
                "add the extras you synced with, for example `uv sync --extra app --extra"
                " menubar` (CONTRIBUTING.md)"
            ]
        # Another environment: reinstall the checkout into the Python that runs this.
        install = (
            f"{shlex.quote(python())} -m pip install -e ."
            if has_pip()
            else f"uv pip install --python {shlex.quote(python())} -e ."
        )
        return [where, update, install], [
            f"with the extras you installed, for example `-e '.[{EXTRAS}]'`"
        ]
    if found.kind == "package" and found.source:
        return [], [
            f"installed from {found.source}, not from PyPI: install it again from there,"
            " with the same command and extras you used"
        ]
    if found.kind == "package" and found.tool:
        extras = f" (with the extras {', '.join(found.extras)})" if found.extras else ""
        return ["uv tool upgrade spartito"], [
            f"`uv tool upgrade` keeps the install's extras and Python{extras}"
        ]
    if found.kind == "package":
        return [f"{shlex.quote(python())} -m pip install --upgrade spartito"], [
            f"with the extras you installed, for example 'spartito[{EXTRAS}]'"
        ]
    return [], [
        f"this environment has no distribution {DISTRIBUTION}: install it again"
        " (docs/usage.md#upgrading-to-the-distribution-name-spartito), then"
        f" `{cli} upgrade` names the command"
    ]


# -- the check for a newer release ----------------------------------------------------------------

# The plain forms of PEP 440: release numbers, then optionally a pre-release, a post-release,
# a development release and a local part (`1.2.0rc1.post2.dev3+abc`), in its spellings.
RELEASE = re.compile(
    r"v?(?P<release>\d+(?:\.\d+)*)"
    r"(?:[-_.]?(?P<pre>alpha|beta|preview|pre|rc|a|b|c)[-_.]?(?P<pre_n>\d*))?"
    r"(?:-(?P<post_implicit>\d+)|[-_.]?(?:post|rev|r)[-_.]?(?P<post_n>\d*))?"
    r"(?:[-_.]?dev[-_.]?(?P<dev_n>\d*))?"
    r"(?:\+[a-z0-9]+(?:[-_.][a-z0-9]+)*)?",
    re.IGNORECASE,
)
PRE_RANK = {"a": 0, "alpha": 0, "b": 1, "beta": 1, "rc": 2, "c": 2, "pre": 2, "preview": 2}
FINAL = 3  # above every pre-release phase


def release_key(version: str) -> tuple | None:
    """How versions are compared, as PEP 440 orders the plain cases: by the release numbers
    (`0.10.0` > `0.9.1`; trailing zeros do not count), then dev < a < b < rc < final < post,
    each with its number (`rc1` < `rc2`, `post1` < `post2`); a `.devN` of a pre- or
    post-release comes just before it. A `+local` part is ignored. None: not such a version."""
    match = RELEASE.fullmatch(version.strip())
    if not match:
        return None
    numbers = [int(n) for n in match["release"].split(".")]
    while len(numbers) > 1 and numbers[-1] == 0:
        numbers.pop()
    post_n = match["post_implicit"] or match["post_n"]
    has_post = match["post_implicit"] is not None or match["post_n"] is not None
    has_dev = match["dev_n"] is not None
    if match["pre"]:
        pre = (PRE_RANK[match["pre"].lower()], int(match["pre_n"] or 0))
    elif has_dev and not has_post:
        pre = (-1, 0)  # `1.0.dev1` comes before `1.0a1`
    else:
        pre = (FINAL, 0)
    post = int(post_n or 0) if has_post else -1
    dev = (0, int(match["dev_n"] or 0)) if has_dev else (1, 0)
    return tuple(numbers), pre, post, dev


def fetch(url: str) -> bytes:
    """GET `url` with nothing but a fixed User-Agent; HTTPError for anything but 2xx."""
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with OPENER.open(request, timeout=TIMEOUT) as answer:
        body = answer.read(READ_LIMIT + 1)
    if len(body) > READ_LIMIT:
        raise ValueError(f"it is larger than {READ_LIMIT} bytes")
    return body


def check(installed: str | None = None) -> dict[str, Any]:
    """Ask PyPI for the latest release of DISTRIBUTION. `status`: "newer" (a newer release
    exists), "current", "ahead" (this one is newer than the latest release), "unknown" (this
    install's version is not known), or "unpublished" (the index answers 404). A failure is an
    OrchestratorError with a one-line reason."""
    installed = installed or __version__
    url, name = f"{INDEX}/{DISTRIBUTION}/json", {"name": DISTRIBUTION}
    try:
        body = fetch(url)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return {"status": "unpublished", "installed": installed, "latest": None, **name}
        if 300 <= exc.code < 400:
            raise OrchestratorError(f"PyPI answered with a redirect (HTTP {exc.code})") from None
        raise OrchestratorError(f"PyPI answered HTTP {exc.code}") from None
    except (urllib.error.URLError, OSError, http.client.HTTPException) as exc:
        reason = exc.reason if isinstance(exc, urllib.error.URLError) else exc
        raise OrchestratorError(f"could not reach PyPI: {reason or type(exc).__name__}") from None
    except ValueError as exc:
        raise OrchestratorError(f"PyPI's answer was not understood: {exc}") from None
    try:
        latest = json.loads(body)["info"]["version"]
        key = release_key(latest) if isinstance(latest, str) else None
    except ValueError, KeyError, TypeError:
        key = None
    if key is None:
        raise OrchestratorError("PyPI's answer was not understood: no release version in it")
    mine = release_key(installed)
    if mine is None or installed.endswith("+unknown"):
        status = "unknown"
    else:
        status = "newer" if key > mine else "current" if key == mine else "ahead"
    return {"status": status, "installed": installed, "latest": latest, **name}


# -- what runs and must be restarted --------------------------------------------------------------


def lock_held(name: str) -> bool:
    """Whether a process holds the lock file `name` of this home's run/ (never creates it)."""
    path = config.home() / "run" / name
    try:
        handle = path.open("rb")
    except OSError:
        return False
    with handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        except OSError:
            return False
        fcntl.flock(handle, fcntl.LOCK_UN)
    return False


def read_port(name: str) -> int:
    with contextlib.suppress(OSError, ValueError):
        port = int((config.home() / "run" / name).read_text().strip())
        return port if 0 < port < 65536 else 0
    return 0


def live_server() -> tuple[int, int] | None:
    """(pid, port) of the dashboard server recorded in run/web.json, when its process lives."""
    try:
        record = json.loads((config.home() / "run" / WEB_RECORD).read_text())
        pid, port = int(record["pid"]), int(record["port"])
    except OSError, ValueError, TypeError, KeyError:
        return None
    return (pid, port) if pid_alive(pid) else None


def installed_service() -> dict[str, Any] | None:
    """This home's background service when its unit file exists: label, host and port."""
    try:
        kind = autostart.platform_kind()
    except autostart.AutostartError:
        return None
    label = autostart.default_label(autostart.home())
    unit = autostart.unit_path(label, kind)
    if not unit.exists():
        return None
    host, port = autostart.address_of(autostart.argv_of(unit, kind))
    return {"label": label, "host": host, "port": port}


def restarts(launcher: bool) -> tuple[list[str], list[str]]:
    """What must be restarted after the upgrade, as seen on this machine and home, and what
    cannot be seen from here. `launcher`: this home's launcher is installed (gui/launcher.py)."""
    cli = config.CLI_NAME
    items: list[str] = []
    service = installed_service()
    server = live_server()
    window = lock_held(APP_LOCK)
    if service:
        options = (
            f" --host {service['host']} --port {service['port']}"
            if service["host"] and service["port"]
            else ""
        )
        items.append(
            f"the background service ({service['label']}): `{cli} service install{options}`"
            " restarts it with the new code"
        )
    if window:
        items.append(f"the `{cli} app` window: quit it and open it again")
    if lock_held(MENUBAR_LOCK):
        items.append(f"the menu-bar item: quit it and start `{cli} menubar` again")
    if server:
        pid, port = server
        if service and service["port"] == port:
            pass  # the service's own server: restarted with it
        elif window and read_port(APP_PORT) == port:
            pass  # the window's own server: it stops when the window is quit
        else:
            items.append(
                f"the dashboard server on port {port} (pid {pid}), a `{cli} web` started by"
                " hand: stop it (Ctrl-C) and start it again"
            )
    if launcher:
        items.append(
            f"the launcher: run `{cli} app install` again (it records the version and the"
            " Python of the install)"
        )
    unseen = [
        f"an open `{cli} chat`: leave it, and continue with `{cli} chat --continue` afterwards",
        f"a `{cli} web` started by hand that is not listed here (only the last dashboard server"
        " started is recorded): stop it and start it again",
    ]
    return items, unseen


# -- prepare --------------------------------------------------------------------------------------


def prepare(orch: Facade, launcher: bool = False, dry_run: bool = False) -> dict[str, Any]:
    """Prepare an upgrade: refuse while any turn is queued or running (as a restore does;
    with `dry_run` they are listed instead), back the state up (not with `dry_run`), and say
    which install this is, the command that upgrades it and what to restart afterwards."""
    busy = [f"{run.agent_id} run {run.id} {run.status}" for run in state.busy(orch)]
    if busy and not dry_run:
        raise OrchestratorError(
            f"not prepared: turns are running or queued ({', '.join(busy)}); wait for them, or"
            f" `{config.CLI_NAME} stop` the agents, then run `{config.CLI_NAME} upgrade` again"
        )
    backup = None if dry_run else state.backup(orch)
    found = install()
    lines, notes = commands(found)
    items, unseen = restarts(launcher)
    return {
        "version": __version__,
        "busy": busy,
        "backup": backup,
        "backups_dir": str(config.backups_dir()),
        "install": {"label": found.label, **asdict(found)},
        "commands": lines,
        "notes": notes,
        "restart": items,
        "unseen": unseen,
    }
