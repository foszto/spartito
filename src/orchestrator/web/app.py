"""Web UI backend: one page plus a small JSON API over the Orchestrator facade.

The API can start agents that run code, so writes are guarded against other web pages
open in the same browser: the Host header must be a loopback name (DNS rebinding), a
write must come from this very origin, and its body must be JSON (which a cross-site
form cannot send).
"""

from __future__ import annotations

import hashlib
from dataclasses import asdict
from html import escape
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

from .. import __version__, config, providers, roles
from ..providers.base import model_choices
from ..service import ORCHESTRATOR_ID, NeedsConfirmation, Orchestrator, OrchestratorError
from ..settings import ARCHIVE_AFTER, ARCHIVE_AFTER_DEFAULT, COMPACT_AT, COMPACT_AT_DEFAULT, TIERS
from . import board_api, code_api

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
    html = page.read_text(encoding="utf-8")
    html = html.replace("{{project_name}}", escape(config.PROJECT_NAME))
    html = html.replace("{{cli_name}}", escape(config.CLI_NAME))
    return HTMLResponse(html.replace("{{orchestrator_name}}", escape(config.ORCHESTRATOR_NAME)))


# The installable page: Chrome wants a name, start_url, display and 192/512 px icons; Safari's
# "Add to Dock" reads name, display, start_url, scope and id. Colours: the light theme's.
ICONS = [
    {"src": "/static/icon-192.png", "sizes": "192x192", "type": "image/png", "purpose": "any"},
    {"src": "/static/icon-512.png", "sizes": "512x512", "type": "image/png", "purpose": "any"},
    {
        "src": "/static/icon-maskable-512.png",
        "sizes": "512x512",
        "type": "image/png",
        "purpose": "maskable",
    },
]


def manifest(request: Request) -> JSONResponse:
    return JSONResponse(
        {
            "id": "/",
            "name": config.PROJECT_NAME,
            "short_name": config.PROJECT_NAME,
            "start_url": "/",
            "scope": "/",
            "display": "standalone",
            "theme_color": "#f4f5f8",
            "background_color": "#f4f5f8",
            "icons": ICONS,
        },
        media_type="application/manifest+json",
    )


def ping(request: Request) -> JSONResponse:
    """Who serves here, for `spartito app` and `spartito service status`: this tool, its version and
    a digest of the home it serves (never the path itself). No store access."""
    home = hashlib.sha256(str(config.home().resolve()).encode()).hexdigest()[:16]
    return JSONResponse({"tool": "orchestrator", "version": __version__, "home": home})


def state(orch: Orchestrator, request: Request, body: dict) -> dict[str, Any]:
    agents = orch.overview()
    top = next((a for a in agents if a["id"] == ORCHESTRATOR_ID), None)
    return {
        # Display names; the ids, roles and API paths stay "orchestrator".
        "project_name": config.PROJECT_NAME,
        "orchestrator_name": config.ORCHESTRATOR_NAME,
        # The installed version, shown in the settings (the same /api/ping reports).
        "version": __version__,
        "orchestrator": top,
        # Its model and effort before the first message, so they can be chosen up front.
        "orchestrator_setup": None if top else orch.setup(ORCHESTRATOR_ID),
        "projects": [
            {**asdict(project), "agents": [a for a in agents if a["project"] == project.name]}
            for project in orch.projects()
        ],
        "providers": [
            {"name": name, **model_choices(provider), "efforts": list(provider.efforts)}
            for name, provider in providers.PROVIDERS.items()
        ],
        "tiers": list(TIERS),
        # Ids only: the archive view loads the agents themselves (/api/archive).
        "archived": orch.archived_ids(),
        # Tokens and cost of every run ever recorded, archived agents' too: the summary.
        "spent": orch.total_spent(),
        "archive_after_default": ARCHIVE_AFTER_DEFAULT,
        "archive_after_key": ARCHIVE_AFTER,
        "compact_at_default": COMPACT_AT_DEFAULT,
        "compact_at_key": COMPACT_AT,
        "settings": {
            "global": orch.settings(),
            "projects": {p.name: orch.settings(p.name) for p in orch.projects()},
        },
        "roles": [
            {"name": r.name, "description": r.description, "worktree": r.worktree, "lead": r.lead}
            for r in roles.available()
            if r.name != ORCHESTRATOR_ID
        ],
        # The open ones, oldest first: the header count and the decisions dialog.
        "decisions": orch.list_decisions(),
        # A second `spartito app` asks the window's page (`?app=`) to show a target (app_open.py):
        # {id, target} while the request is fresh, else null.
        "app_open": orch.app_open_request(),
    }


def events(orch: Orchestrator, request: Request, body: dict) -> dict[str, Any]:
    agent = orch.agent(request.path_params["agent_id"])
    after = int(request.query_params.get("after", 0))
    rows = orch.events(agent.id, after=after, tail=None if after else 400)
    setup = orch.setup(agent.id)
    return {
        "agent": {
            **orch.view(agent, detail=True),
            "changes": orch.changes(agent.id),
            # model/effort: in effect, inherited without the agent's own values (those are
            # model_override/effort_override) and what switching them does to the cache
            "effective": setup["effective"],
            "inherited": setup["inherited"],
            "cache_effect": setup["cache_effect"],
            "cache_hint": setup["cache_hint"],
            "recent_runs": orch.run_history(agent, limit=10),
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
    registered = {project.path: project.name for project in orch.projects()}
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


def set_agent_model(orch: Orchestrator, request: Request, body: dict) -> dict[str, Any]:
    """Set (a string) or clear (null) the agent's own `model` and/or `effort`; a missing
    key keeps the value. Creates the orchestrator on first use, like a message does."""
    return orch.set_overrides(request.path_params["agent_id"], body)


def save_settings(orch: Orchestrator, request: Request, body: dict) -> dict[str, Any]:
    return orch.update_settings(body.get("data") or {}, optional(body, "project"))


def stop_agent(orch: Orchestrator, request: Request, body: dict) -> dict[str, Any]:
    return {"stopped_runs": orch.stop(request.path_params["agent_id"])}


def compact_agent(orch: Orchestrator, request: Request, body: dict) -> dict[str, Any]:
    """Queue a compaction of the agent's conversation, with optional `instructions`."""
    agent_id = request.path_params["agent_id"]
    return queued(agent_id, orch.compact(agent_id, optional(body, "instructions")).id)


def get_run(orch: Orchestrator, request: Request, body: dict) -> dict[str, Any]:
    """One run with its full text (the queue lists a preview), for editing it."""
    run_id = request.path_params["run_id"]
    run = orch.get_run(run_id)
    if run is None:
        raise OrchestratorError(f"no run {run_id}")
    kind = orch.run_kind(run_id)
    return {
        "id": run.id,
        "agent_id": run.agent_id,
        "status": run.status.value,
        "kind": kind,
        "prompt": run.prompt,
    }


def withdraw_run(orch: Orchestrator, request: Request, body: dict) -> dict[str, Any]:
    run = orch.withdraw(request.path_params["run_id"])
    return {"withdrawn": run.id, "agent_id": run.agent_id}


def edit_run(orch: Orchestrator, request: Request, body: dict) -> dict[str, Any]:
    run = orch.replace_message(request.path_params["run_id"], required(body, "message"))
    return queued(run.agent_id, run.id)


def archived_agents(orch: Orchestrator, request: Request, body: dict) -> dict[str, Any]:
    return {"agents": orch.overview(archived=True)}


def archive_agent(orch: Orchestrator, request: Request, body: dict) -> dict[str, Any]:
    """Archive the agent; work that is not merged comes back as `confirm` (what it is)
    instead, until the request is repeated with `confirm: true`."""
    try:
        return orch.archive(request.path_params["agent_id"], bool(body.get("confirm")))
    except NeedsConfirmation as exc:
        return {"confirm": str(exc)}


def archive_safe(orch: Orchestrator, request: Request, body: dict) -> dict[str, Any]:
    return orch.archive_safe(request.path_params["name"])


def restore_agent(orch: Orchestrator, request: Request, body: dict) -> dict[str, Any]:
    return orch.restore(request.path_params["agent_id"])


def remove_agent(orch: Orchestrator, request: Request, body: dict) -> dict[str, Any]:
    agent_id = request.path_params["agent_id"]
    orch.remove(agent_id, delete_branch=bool(body.get("delete_branch")))
    return {"removed": agent_id}


def list_decisions(orch: Orchestrator, request: Request, body: dict) -> dict[str, Any]:
    """Open decisions; `?all=1` includes the closed ones."""
    return {"decisions": orch.list_decisions(closed=bool(request.query_params.get("all")))}


def answer_decision(orch: Orchestrator, request: Request, body: dict) -> dict[str, Any]:
    """Answer a decision with `answer` (an option's text, its number or free text)."""
    return orch.answer_decision(request.path_params["decision_id"], body.get("answer"))


SQLITE_MAX_INT = 2**63 - 1  # a larger id makes sqlite3 raise OverflowError


def notice_id(value: Any, what: str) -> int:
    """`value` as a notice id (an int SQLite can hold); else OrchestratorError (400)."""
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= SQLITE_MAX_INT:
        raise OrchestratorError(f"'{what}' must be a notice id")
    return value


def poll_notices(orch: Orchestrator, request: Request, body: dict) -> dict[str, Any]:
    """A page that shows desktop notifications itself (desktop.py) polls here: `after` (a
    notice id) gives the unclaimed notices it may take, oldest first: those above it, and
    those escalated to a page or waiting (core/notices.page_notices); without it none (a page
    that just opened replays nothing). `cursor`: the highest notice id so far. The poll also
    marks the page present, so desktop.route counts it for a while: a POST, so that the
    same-origin check keeps other web pages from silencing the notifications."""
    after = body.get("after")
    after = None if after is None else notice_id(after, "after")
    orch.page_present()
    cursor = orch.notice_cursor()  # before the list: a notice added meanwhile is still listed
    return {"notices": [] if after is None else orch.page_notices(after), "cursor": cursor}


def claim_notice(orch: Orchestrator, request: Request, body: dict) -> dict[str, Any]:
    """Take a notice to show it: `claimed` is true for the first caller only, and only for a
    notice desktop.route left to a page (or to a later way)."""
    notice = notice_id(request.path_params["notice_id"], "notice id")
    return {"claimed": orch.claim_notice(notice)}


def list_board(orch: Orchestrator, request: Request, body: dict) -> dict[str, Any]:
    """`?project=` (required) the project's items by state and position; `&state=` one state."""
    project = required(dict(request.query_params), "project")
    items = orch.board_items(project, request.query_params.get("state") or None)
    return {"items": [item.to_dict() for item in items]}


def add_board_item(orch: Orchestrator, request: Request, body: dict) -> dict[str, Any]:
    """One request: the item with its optional `agent`/`branch` links, or nothing."""
    item = orch.add_board_item(
        required(body, "project"),
        body.get("title"),
        body.get("description"),
        body.get("state"),
        body.get("agent"),
        body.get("branch"),
    )
    return item.to_dict()


BOARD_FIELDS = {
    "title": "title",
    "description": "description",
    "state": "state",
    "agent": "agent_id",
    "branch": "branch",
}


def update_board_item(orch: Orchestrator, request: Request, body: dict) -> dict[str, Any]:
    """Changes the fields named in the body (`agent`/`branch` null or "" clear the link)."""
    changes = {field: body[key] for key, field in BOARD_FIELDS.items() if key in body}
    for field in ("agent_id", "branch"):
        if field in changes and changes[field] is None:
            changes[field] = ""
    return orch.update_board_item(request.path_params["item_id"], changes).to_dict()


def cost(orch: Orchestrator, request: Request, body: dict) -> dict[str, Any]:
    """The cost report of the recorded runs (`?by=project|day|agent`, `?since=YYYY-MM-DD`,
    `?project=`) and every budget with what is spent in its current period. Read-only (GET),
    and only asked for when the page's cost view is opened: never part of /api/state."""
    query = request.query_params
    report = orch.cost_report(
        query.get("by") or "project", query.get("since") or None, query.get("project") or None
    )
    return {"report": report, "budgets": orch.budget_list()}


def move_board_item(orch: Orchestrator, request: Request, body: dict) -> dict[str, Any]:
    return orch.move_board_item(request.path_params["item_id"], body.get("direction")).to_dict()


def dispatch_board(orch: Orchestrator, request: Request, body: dict) -> dict[str, Any]:
    """The project's ready items to its lead as one message; `ids`: only those (the ones the
    owner confirmed) that are still ready. None of them ready: nothing is sent."""
    return orch.dispatch_board(required(body, "project"), body.get("ids"))


app = Starlette(
    routes=[
        Route("/", index),
        Route("/manifest.webmanifest", manifest),
        Route("/api/ping", ping),
        Route("/api/state", api(state)),
        Route("/api/browse", api(browse)),
        Mount("/static", NoCacheStatic(directory=Path(__file__).parent / "static")),
        Route("/api/agents/{agent_id}/events", api(events)),
        Route("/api/archive", api(archived_agents)),
        Route("/api/projects", api(add_project), methods=["POST"]),
        Route("/api/projects/{name}/remove", api(remove_project), methods=["POST"]),
        Route("/api/projects/{name}/lead", api(message_lead), methods=["POST"]),
        Route("/api/projects/{name}/archive", api(archive_safe), methods=["POST"]),
        Route("/api/orchestrator", api(message_orchestrator), methods=["POST"]),
        Route("/api/settings", api(save_settings), methods=["POST"]),
        Route("/api/agents", api(spawn_agent), methods=["POST"]),
        Route("/api/agents/{agent_id}/messages", api(message_agent), methods=["POST"]),
        Route("/api/agents/{agent_id}/model", api(set_agent_model), methods=["POST"]),
        Route("/api/agents/{agent_id}/stop", api(stop_agent), methods=["POST"]),
        Route("/api/agents/{agent_id}/compact", api(compact_agent), methods=["POST"]),
        Route("/api/agents/{agent_id}/archive", api(archive_agent), methods=["POST"]),
        Route("/api/agents/{agent_id}/restore", api(restore_agent), methods=["POST"]),
        Route("/api/agents/{agent_id}/remove", api(remove_agent), methods=["POST"]),
        Route("/api/runs/{run_id:int}", api(get_run)),
        Route("/api/runs/{run_id:int}/withdraw", api(withdraw_run), methods=["POST"]),
        Route("/api/runs/{run_id:int}/edit", api(edit_run), methods=["POST"]),
        Route("/api/decisions", api(list_decisions)),
        Route("/api/decisions/{decision_id:int}/answer", api(answer_decision), methods=["POST"]),
        Route("/api/notices/poll", api(poll_notices), methods=["POST"]),
        Route("/api/notices/{notice_id:int}/claim", api(claim_notice), methods=["POST"]),
        Route("/api/board", api(list_board)),
        Route("/api/cost", api(cost)),
        Route("/api/board/add", api(add_board_item), methods=["POST"]),
        Route("/api/board/dispatch", api(dispatch_board), methods=["POST"]),
        Route("/api/board/{item_id:int}", api(update_board_item), methods=["POST"]),
        Route("/api/board/{item_id:int}/move", api(move_board_item), methods=["POST"]),
        *board_api.ROUTES,
        *code_api.ROUTES,
    ],
    middleware=[Middleware(LocalOnly)],
)
