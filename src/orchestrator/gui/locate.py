"""Find a dashboard server of this home that is already running.

`run/web.json` says on which port the last one listened; it counts only when `/api/ping` on
that port of the loopback address answers with this tool and this home. A stale file, a dead
port, another program or another home all mean "none". The host in the file is never
contacted: only 127.0.0.1 (::1 when the file names an IPv6 address).
"""

from __future__ import annotations

import json

from .. import autostart
from ..web import serve

PING_TIMEOUT = 1.0  # seconds


def loopback(host: str) -> str:
    """The loopback address to reach a server that listens on `host`."""
    return "::1" if ":" in host else "127.0.0.1"


def url_of(host: str, port: int) -> str:
    """The URL of a loopback address and port."""
    return f"http://{f'[{host}]' if ':' in host else host}:{port}/"


def running_server() -> str | None:
    """The URL of this home's running dashboard server, or None."""
    try:
        record = json.loads(serve.record_path().read_text())
        host, port = str(record["host"]), int(record["port"])
    except OSError, ValueError, TypeError, KeyError:
        return None
    if not 0 < port < 65536:
        return None
    target = loopback(host)
    answer, _ = autostart.ping(target, port, autostart.home(), timeout=PING_TIMEOUT)
    return url_of(target, port) if answer == "ours" else None
