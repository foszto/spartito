"""Run the dashboard server: what `orch web` and `orch app` share.

Once it listens, the server records itself in `$ORCH_HOME/run/web.json` (pid, host, port,
start time) so `orch app` can find it, and removes the record at shutdown if it is still its
own (a later server of the same home may have replaced it).
"""

from __future__ import annotations

import json
import os
import socket
import sys
from datetime import UTC, datetime
from pathlib import Path

import uvicorn

from .. import config

STARTUP_FAILURE = 3  # uvicorn's exit code when the server could not start


def record_path() -> Path:
    return config.runtime_dir() / "web.json"


class Server(uvicorn.Server):
    """A uvicorn server that keeps `run/web.json` while it listens."""

    record: dict | None = None  # what it wrote to `run/web.json`, once it did

    async def startup(self, sockets: list[socket.socket] | None = None) -> None:
        await super().startup(sockets)
        if self.started:
            port = self.servers[0].sockets[0].getsockname()[1]
            record = {
                "pid": os.getpid(),
                "host": self.config.host,
                "port": port,
                "started": datetime.now(UTC).isoformat(timespec="seconds"),
            }
            path = record_path()
            temp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
            temp.write_text(json.dumps(record))
            temp.replace(path)
            self.record = record

    @property
    def ready(self) -> bool:
        """Listening and recorded in `run/web.json`."""
        return self.record is not None

    async def shutdown(self, sockets: list[socket.socket] | None = None) -> None:
        # Here, not only after `run`: on SIGTERM uvicorn raises the signal again once it has
        # shut down, which ends the process before any `finally` of the caller.
        self.forget()
        await super().shutdown(sockets)

    def forget(self) -> None:
        """Remove `run/web.json` if it is still this server's."""
        if self.record is None:
            return
        path = record_path()
        try:
            current = json.loads(path.read_text())
        except OSError, ValueError:
            return
        if current == self.record:
            path.unlink(missing_ok=True)


def make(host: str, port: int) -> Server:
    """A server for the dashboard on host:port. Only loopback Host headers are accepted when
    the host is a loopback address; any when it is not (the caller warns about that)."""
    from .app import LOOPBACK_HOSTS
    from .app import app as web_app

    web_app.state.allowed_hosts = LOOPBACK_HOSTS if host in LOOPBACK_HOSTS else None
    return Server(uvicorn.Config(web_app, host=host, port=port, log_level="warning"))


def serve(server: Server, sock: socket.socket | None = None) -> None:
    """Run `server` until it is told to exit (Ctrl-C, or `should_exit` from another thread),
    on `sock` when given (already bound) or on its own host and port. Exits with uvicorn's
    code when it could not start, as `uvicorn.run` does."""
    try:
        server.run(sockets=[sock] if sock is not None else None)
    except KeyboardInterrupt:
        pass
    finally:
        server.forget()
    if not server.started:
        sys.exit(STARTUP_FAILURE)
