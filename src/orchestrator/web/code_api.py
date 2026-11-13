"""The code view's JSON API, read-only: an agent's changed files against its base branch, one
file's patch, the worktree's files and one file's content (core/code_view.py). `ROUTES` is
mounted by app.py, behind its LocalOnly middleware (the Host check) like every other route.

GET only. File contents are more than the rest of the API shows, so these routes also refuse
a request another site's page makes: an `Origin` other than this server's, or a
`Sec-Fetch-Site` of `cross-site` or `same-site` (another port of the same host is the same
site). The page's own requests are same-origin; a typed address is `none`.

The handlers run as app.py's do: blocking, in a thread, an OrchestratorError answered with
400 `{"error": …}`. Nothing is computed unless one of these routes is asked.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit

import anyio
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from ..service import Orchestrator, OrchestratorError

ROUTES: list[Route] = []
FOREIGN_FETCH = frozenset({"cross-site", "same-site"})


def foreign(request: Request) -> bool:
    """Whether another site's page made the request."""
    origin = request.headers.get("origin")
    if origin is not None and urlsplit(origin).netloc != request.headers.get("host", ""):
        return True
    return request.headers.get("sec-fetch-site", "").lower() in FOREIGN_FETCH


def route(path: str) -> Any:
    """Add the handler below, handler(orch, agent_id, request), to ROUTES at `path` (GET)."""

    def add(handler: Any) -> Any:
        async def endpoint(request: Request) -> JSONResponse:
            if foreign(request):
                return JSONResponse({"error": "cross-origin request refused"}, status_code=403)
            agent_id = request.path_params["agent_id"]
            try:
                result = await anyio.to_thread.run_sync(handler, Orchestrator(), agent_id, request)
            except OrchestratorError as exc:
                message = exc.args[0] if exc.args else str(exc)
                return JSONResponse({"error": str(message)}, status_code=400)
            return JSONResponse(result, headers={"Cache-Control": "no-store"})

        ROUTES.append(Route(path, endpoint, methods=["GET"]))
        return handler

    return add


@route("/api/agents/{agent_id}/code/changes")
def changes(orch: Orchestrator, agent_id: str, request: Request) -> dict[str, Any]:
    """The changed files against the base branch with their line counts."""
    return orch.code_changes(agent_id)


@route("/api/agents/{agent_id}/code/patch")
def patch(orch: Orchestrator, agent_id: str, request: Request) -> dict[str, Any]:
    """`?path=` one changed file's patch."""
    return orch.code_patch(agent_id, request.query_params.get("path"))


@route("/api/agents/{agent_id}/code/tree")
def tree(orch: Orchestrator, agent_id: str, request: Request) -> dict[str, Any]:
    """The worktree's files that git knows or would track."""
    return orch.code_tree(agent_id)


@route("/api/agents/{agent_id}/code/file")
def file(orch: Orchestrator, agent_id: str, request: Request) -> dict[str, Any]:
    """`?path=` one file's content, or a link's target."""
    return orch.code_file(agent_id, request.query_params.get("path"))
