"""The task board's JSON API beyond the per-project dialog's (`/api/board*` in app.py): the
board view across projects. `ROUTES` is mounted by app.py; the writes are guarded by its
LocalOnly middleware like every other route.

The handlers run as app.py's do: blocking, in a thread, an OrchestratorError (or a missing
key) answered with 400 `{"error": …}`. The wrapper is repeated here because app.py imports
this module (importing app.py back would be a cycle).
"""

from __future__ import annotations

from typing import Any

import anyio
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from ..service import Orchestrator, OrchestratorError

ROUTES: list[Route] = []


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


def route(path: str, post: bool = False) -> Any:
    """Add the handler below to ROUTES at `path` (GET; `post`: POST)."""

    def add(handler: Any) -> Any:
        ROUTES.append(Route(path, api(handler), methods=["POST"] if post else None))
        return handler

    return add


# -- the board across projects ---------------------------------------------------------


@route("/api/board/view")
def board_view(orch: Orchestrator, request: Request, body: dict) -> dict[str, Any]:
    """`?project=` one project's items (default: every registered project's); `&state=` one
    state's: {projects, synced_at, items}."""
    query = request.query_params
    return orch.board_view(query.get("project") or None, query.get("state") or None)


# -- placing an item -------------------------------------------------------------------


@route("/api/board/{item_id:int}/place", post=True)
def place_board_item(orch: Orchestrator, request: Request, body: dict) -> dict[str, Any]:
    """`{state, before}`: the item into `state` just before the item `before` (null or
    absent: at the end); the item as it is now."""
    if not body.get("state"):
        raise OrchestratorError("'state' is required")
    item_id = request.path_params["item_id"]
    return orch.place_board_item(item_id, body["state"], body.get("before")).to_dict()


# -- dispatching named items -----------------------------------------------------------


@route("/api/board/dispatch-items", post=True)
def dispatch_items(orch: Orchestrator, request: Request, body: dict) -> dict[str, Any]:
    """`{ids}`: those items (idea or ready, in any project) to their project's lead, one
    message per project: {results: [{project, agent_id, run_id, items}], skipped: [{id,
    reason}]}."""
    return orch.dispatch_items(body.get("ids"))


# -- an item's history -----------------------------------------------------------------
