"""Requests to the open dashboard window of this home (`orch app`), through two files in
`$ORCH_HOME/run/`:

- `app-open.json` `{"id": <increasing number>, "target": "decision:5" | "<agent id>" | ""}`:
  a second `orch app` asks the open window to come to the front and, with a target, to show
  it. The window's page (opened with `?app=<last id>`) gets the request with /api/state
  while it is younger than FRESH_SECONDS and applies each one once; the `orch app` holding
  the window watches the file and raises the window.
- `app-open.done` `{"id": ..., "raised": true | false, "detail": "..."}`: that `orch app`
  says whether it could, so the second one can tell its caller.

A target is checked here (TARGET) wherever it is written or read: an agent id as `orch`
makes them, or `decision:<number>`.
"""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path

from . import config

REQUEST = "app-open.json"
DONE = "app-open.done"
FRESH_SECONDS = 15.0
TARGET = re.compile(r"decision:[0-9]{1,18}|[a-z0-9][a-z0-9-]{0,63}")


def check_target(target: str) -> str:
    """`target` itself when it is "" or valid (TARGET); ValueError otherwise."""
    if target and not TARGET.fullmatch(target):
        raise ValueError(f"not an agent id or decision:<number>: {target!r}")
    return target


def _path(name: str) -> Path:
    return config.runtime_dir() / name


def _write(path: Path, data: dict) -> None:
    temp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    temp.write_text(json.dumps(data))
    temp.replace(path)


def _read(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text())
    except OSError, ValueError:
        return None
    if not isinstance(data, dict):
        return None
    number = data.get("id")
    if isinstance(number, bool) or not isinstance(number, int) or number < 0:
        return None
    return data


def last_id() -> int:
    """The id of the latest request, whatever its age (0: none); a window opened now applies
    only the ones after it."""
    data = _read(_path(REQUEST))
    return data["id"] if data else 0


def request(target: str = "") -> int:
    """Ask the open window to come to the front (and show `target`); returns the request id,
    above every earlier one."""
    check_target(target)
    number = max(last_id() + 1, int(time.time() * 1000))
    _write(_path(REQUEST), {"id": number, "target": target})
    return number


def latest(fresh: float = FRESH_SECONDS) -> dict | None:
    """The latest request `{"id", "target"}` while it is younger than `fresh` seconds (by the
    file's time; one from the future does not count), else None. An invalid target: None."""
    path = _path(REQUEST)
    try:
        age = time.time() - path.stat().st_mtime
    except OSError:
        return None
    data = _read(path)
    if data is None or not 0 <= age < fresh:
        return None
    target = data.get("target")
    if not isinstance(target, str) or (target and not TARGET.fullmatch(target)):
        return None
    return {"id": data["id"], "target": target}


def answer(number: int, raised: bool, detail: str = "") -> None:
    """The window's `orch app` handled request `number`: whether its window came forward."""
    _write(_path(DONE), {"id": number, "raised": raised, "detail": detail})


def answered(number: int) -> dict | None:
    """The answer to request `number` (`{"raised", "detail"}`), None while there is none."""
    data = _read(_path(DONE))
    if data is None or data["id"] != number:
        return None
    return {"raised": data.get("raised") is True, "detail": str(data.get("detail") or "")}
