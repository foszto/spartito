"""Find a dashboard server of this home that is already running.

`run/web.json` says where the last one listened; it counts only when `/api/ping` there
answers with this tool and this home. A stale file, a dead port, another program or another
home all mean "none".
"""

from __future__ import annotations

import json

from .. import autostart
from ..web import serve

PING_TIMEOUT = 1.0  # seconds


def url_of(host: str, port: int) -> str:
    """The URL to open for a server listening on host:port (a wildcard host on loopback)."""
    target = {"0.0.0.0": "127.0.0.1", "::": "::1", "": "127.0.0.1"}.get(host, host)
    return f"http://{f'[{target}]' if ':' in target else target}:{port}/"


def running_server() -> str | None:
    """The URL of this home's running dashboard server, or None."""
    try:
        record = json.loads(serve.record_path().read_text())
        host, port = str(record["host"]), int(record["port"])
    except OSError, ValueError, TypeError, KeyError:
        return None
    if not 0 < port < 65536:
        return None
    answer, _ = autostart.ping(host, port, autostart.home(), timeout=PING_TIMEOUT)
    return url_of(host, port) if answer == "ours" else None
