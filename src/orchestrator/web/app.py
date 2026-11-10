"""Web UI backend: one page plus a small JSON API over the Orchestrator facade.

The API can start agents that run code, so writes are guarded against other web pages
open in the same browser: the Host header must be a loopback name (DNS rebinding), a
write must come from this very origin, and its body must be JSON (which a cross-site
form cannot send).
"""

from __future__ import annotations

from dataclasses import asdict
from importlib import resources
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import anyio
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, Response
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from .. import providers, roles
from ..service import ORCHESTRATOR_ID, Orchestrator, OrchestratorError
from ..settings import TIERS

LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


class LocalOnly(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: Any) -> Response:
        allowed = getattr(request.app.state, "allowed_hosts", LOOPBACK_HOSTS)
        host = request.headers.get("host", "")
        if allowed is not None and urlsplit(f"//{host}").hostname not in allowed:
            return JSONResponse({"error": "unexpected Host header"}, status_code=403)
        if request.method not in ("GET", "HEAD"):
            origin = urlsplit(request.headers.get("origin", ""))
            if origin.netloc != host:
                return JSONResponse({"error": "cross-origin request refused"}, status_code=403)
            if request.headers.get("content-type", "").split(";")[0] != "application/json":
                return JSONResponse({"error": "JSON body required"}, status_code=415)
        return await call_next(request)


class NoCacheStatic(StaticFiles):
    """Always revalidate, so an upgraded orchestrator never runs a stale page script."""

    async def get_response(self, path: str, scope: Any) -> Response:
        response = await super().get_response(path, scope)
        response.headers["Cache-Control"] = "no-cache"
        return response


def api(handler: Any) -> Any:
    """Run a blocking handler(orch, request, body) in a thread; map errors to JSON."""

    async def endpoint(request: Request) -> JSONResponse:
        body = await request.json() if request.method == "POST" else {}
        try:
            result = await anyio.to_thread.run_sync(handler, Orchestrator(), request, body)
        except (OrchestratorError, KeyError) as exc:
            message = exc.args[0] if exc.args else str(exc)
            return JSONResponse({"error": str(message)}, status_code=400)
        return JSONResponse(result)

    return endpoint


def required(body: dict[str, Any], key: str) -> str:
    value = str(body.get(key) or "").strip()
    if not value:
        raise OrchestratorError(f"'{key}' is required")
    return value


def optional(body: dict[str, Any], key: str) -> Any:
    value = body.get(key)
    return value.strip() or None if isinstance(value, str) else value


def queued(agent_id: str, run_id: int) -> dict[str, Any]:
    return {"agent_id": agent_id, "run_id": run_id}


def index(request: Request) -> HTMLResponse:
    page = resources.files("orchestrator.web") / "index.html"
    return HTMLResponse(page.read_text(encoding="utf-8"))


def state(orch: Orchestrator, request: Request, body: dict) -> dict[str, Any]:
    agents = orch.overview()
    return {
        "orchestrator": next((a for a in agents if a["id"] == ORCHESTRATOR_ID), None),
        "projects": [
            {**asdict(project), "agents": [a for a in agents if a["project"] == project.name]}
            for project in orch.store.list_projects()
        ],
        "providers": [
            {"name": name, "models": provider.models(), "efforts": list(provider.efforts)}
            for name, provider in providers.PROVIDERS.items()
        ],
        "tiers": list(TIERS),
        "settings": {
            "global": orch.settings(),
            "projects": {p.name: orch.settings(p.name) for p in orch.store.list_projects()},
        },
        "roles": [
            {"name": r.name, "description": r.description, "worktree": r.worktree, "lead": r.lead}
            for r in roles.available()
            if r.name != ORCHESTRATOR_ID
        ],
    }


def events(orch: Orchestrator, request: Request, body: dict) -> dict[str, Any]:
    agent = orch.agent(request.path_params["agent_id"])
    after = int(request.query_params.get("after", 0))
    rows = orch.store.events(agent.id, after=after, tail=None if after else 400)
    return {
        "agent": {
            **orch.view(agent),
            "changes": orch.changes(agent.id),
            "effective": orch.effective(agent),
        },
        "events": [
            {"id": e.id, "run_id": e.run_id, "ts": e.ts, "kind": e.kind, "text": e.text}
            for e in rows
        ],
    }


def _is_repo(path: Path) -> bool:
    try:
        return (path / ".git").exists()
    except OSError:
        return False


def browse(orch: Orchestrator, request: Request, body: dict) -> dict[str, Any]:
    """List the sub-directories of a directory, for the folder picker."""
    home = Path.home()
    raw = request.query_params.get("path")
    start = home / "Projects" if not raw and (home / "Projects").is_dir() else home
    path = Path(raw).expanduser().resolve() if raw else start
    try:
        children = [child for child in path.iterdir() if not child.name.startswith(".")]
        folders = sorted((c for c in children if c.is_dir()), key=lambda c: c.name.lower())
    except NotADirectoryError, FileNotFoundError:
        raise OrchestratorError(f"{path} is not a directory") from None
    except PermissionError:
        raise OrchestratorError(f"no permission to read {path}") from None
    registered = {project.path: project.name for project in orch.store.list_projects()}
    return {
        "path": str(path),
        "parent": str(path.parent) if path.parent != path else None,
        "home": str(home),
        "git": _is_repo(path),
        "project": registered.get(str(path)),
        "entries": [
            {
                "name": folder.name,
                "path": str(folder),
                "git": _is_repo(folder),
                "project": registered.get(str(folder)),
            }
            for folder in folders
        ],
    }


def add_project(orch: Orchestrator, request: Request, body: dict) -> dict[str, Any]:
    project = orch.add_project(
        required(body, "path"), optional(body, "name"), optional(body, "provider") or "claude"
    )
    return {**asdict(project), "note": orch.project_note(project)}


def remove_project(orch: Orchestrator, request: Request, body: dict) -> dict[str, Any]:
    orch.remove_project(request.path_params["name"])
    return {"removed": request.path_params["name"]}


def spawn_agent(orch: Orchestrator, request: Request, body: dict) -> dict[str, Any]:
    agent, run = orch.spawn(
        required(body, "project"),
        required(body, "task"),
        name=optional(body, "name"),
        role=optional(body, "role") or "worker",
        provider=optional(body, "provider"),
        model=optional(body, "model"),
        effort=optional(body, "effort"),
        worktree=optional(body, "worktree"),
        share_with=optional(body, "share_with"),
        base=optional(body, "base"),
        after=optional(body, "after"),
    )
    return queued(agent.id, run.id)


def message_agent(orch: Orchestrator, request: Request, body: dict) -> dict[str, Any]:
    agent_id = request.path_params["agent_id"]
    return queued(agent_id, orch.send(agent_id, required(body, "message")).id)


def message_orchestrator(orch: Orchestrator, request: Request, body: dict) -> dict[str, Any]:
    agent = orch.orchestrator()
    return queued(agent.id, orch.send(agent.id, required(body, "message")).id)


def message_lead(orch: Orchestrator, request: Request, body: dict) -> dict[str, Any]:
    lead = orch.lead(request.path_params["name"])
    return queued(lead.id, orch.send(lead.id, required(body, "message")).id)


def save_settings(orch: Orchestrator, request: Request, body: dict) -> dict[str, Any]:
    return orch.update_settings(body.get("data") or {}, optional(body, "project"))


def stop_agent(orch: Orchestrator, request: Request, body: dict) -> dict[str, Any]:
    return {"stopped_runs": orch.stop(request.path_params["agent_id"])}


def remove_agent(orch: Orchestrator, request: Request, body: dict) -> dict[str, Any]:
    agent_id = request.path_params["agent_id"]
    orch.remove(agent_id, delete_branch=bool(body.get("delete_branch")))
    return {"removed": agent_id}


app = Starlette(
    routes=[
        Route("/", index),
        Route("/api/state", api(state)),
        Route("/api/browse", api(browse)),
        Mount("/static", NoCacheStatic(directory=Path(__file__).parent / "static")),
        Route("/api/agents/{agent_id}/events", api(events)),
        Route("/api/projects", api(add_project), methods=["POST"]),
        Route("/api/projects/{name}/remove", api(remove_project), methods=["POST"]),
        Route("/api/projects/{name}/lead", api(message_lead), methods=["POST"]),
        Route("/api/orchestrator", api(message_orchestrator), methods=["POST"]),
        Route("/api/settings", api(save_settings), methods=["POST"]),
        Route("/api/agents", api(spawn_agent), methods=["POST"]),
        Route("/api/agents/{agent_id}/messages", api(message_agent), methods=["POST"]),
        Route("/api/agents/{agent_id}/stop", api(stop_agent), methods=["POST"]),
        Route("/api/agents/{agent_id}/remove", api(remove_agent), methods=["POST"]),
    ],
    middleware=[Middleware(LocalOnly)],
)
