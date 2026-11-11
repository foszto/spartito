"""The task-board batch keeps what was there: the default `list_board` reply, the MCP tool
listing a lead pays for in every turn, and the actor each change is made by."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from typing import Any

import anyio
import pytest
from test_notifications import lead

from orchestrator import board, mcp_server
from orchestrator.service import ORCHESTRATOR_ID, Orchestrator

# Measured on master before the batch: the JSON of every tool in the listing (name,
# description, inputSchema, outputSchema), summed.
MASTER_LISTING_CHARS = 15342
# What the orchestrator (and the interactive session) sees beyond a lead: dispatch_board,
# 440 chars as listed, of which 377 are its description and input schema.
ORCHESTRATOR_EXTRA_CHARS = 440
ORCHESTRATOR_TEXT_CHARS = 380


def seed(orch: Orchestrator, project: str) -> None:
    orch.add_board_item(project, "Login page", "with OAuth\nand tests ✓ " + "d" * 300)
    orch.add_board_item(project, "Fix CI", state="ready")
    orch.add_board_item(project, "Search " + "t" * 150, "", "in_progress", None, "orch/search")
    orch.add_board_item(project, "Ship it", "done long ago", "done")
    orch.add_board_item(project, "Ünïcode “quotes”", state="ready")


# The replies of master's list_board for `seed`, byte for byte.
GOLDEN_ALL = (
    '{"project": "demo", "counts": {"in_progress": 1, "ready": 2, "idea": 1, "done": 1},'
    ' "items": [{"id": 3, "state": "in_progress", "title": "Search '
    + "t"
    * 92
    + '\\u2026", "branch": "orch/search"}, {"id": 2, "state": "ready", "title": "Fix CI"},'
    ' {"id": 5, "state": "ready", "title": "\\u00dcn\\u00efcode \\u201cquotes\\u201d"},'
    ' {"id": 1, "state": "idea", "title": "Login page", "description": "with OAuth\\nand tests'
    " \\u2713 " + "d" * 136 + '\\u2026"}, {"id": 4, "state": "done", "title": "Ship it",'
    ' "description": "done long ago"}]}'
)
GOLDEN_READY = (
    '{"project": "demo", "counts": {"ready": 2}, "items": [{"id": 2, "state": "ready",'
    ' "title": "Fix CI"}, {"id": 5, "state": "ready", "title":'
    ' "\\u00dcn\\u00efcode \\u201cquotes\\u201d"}]}'
)
GOLDEN_FULL_SHA256 = "aced286cac1dbe7b845a6ef153c028ab7d87fbac428e104fe473d3662ef1cff7"
GOLDEN_FULL_TAIL = '"note": "26 not shown (idea 25, done 1); state= lists one state"}'


def test_the_default_list_board_reply_is_unchanged(orch: Orchestrator, repo) -> None:
    project = orch.add_project(str(repo), "demo", "fake").name
    seed(orch, project)

    assert json.dumps(mcp_server.list_board(project)) == GOLDEN_ALL
    assert json.dumps(mcp_server.list_board(project, "ready")) == GOLDEN_READY
    for n in range(40):
        orch.add_board_item(project, f"idea {n} " + "t" * 190, "d" * 4000)
    full = json.dumps(mcp_server.list_board(project))
    assert full.endswith(GOLDEN_FULL_TAIL)
    assert hashlib.sha256(full.encode()).hexdigest() == GOLDEN_FULL_SHA256


def listing() -> list[dict[str, Any]]:
    tools = anyio.run(mcp_server.server.list_tools)
    return [tool.model_dump(by_alias=True, exclude_none=True) for tool in tools]


def removed_for(agent_id: str | None, monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """The tools `main` takes away for a caller (the server is not run)."""
    removed: list[str] = []
    monkeypatch.setattr(mcp_server.server, "run", lambda *args, **kwargs: None)
    monkeypatch.setattr(mcp_server.server, "remove_tool", removed.append)
    if agent_id:
        monkeypatch.setenv("ORCH_AGENT_ID", agent_id)
    else:
        monkeypatch.delenv("ORCH_AGENT_ID", raising=False)
    mcp_server.main()
    return removed


def chars(tools: list[dict[str, Any]]) -> int:
    return sum(len(json.dumps(tool)) for tool in tools)


@pytest.mark.parametrize(
    ("caller", "extra"),
    [("lead-1234", 0), ("w-5678", 0), ("orchestrator", ORCHESTRATOR_EXTRA_CHARS)],
)
def test_the_tool_listing_costs_a_lead_nothing_more(
    caller: str, extra: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    removed = removed_for(caller, monkeypatch)
    seen = [tool for tool in listing() if tool["name"] not in removed]
    assert chars(seen) <= MASTER_LISTING_CHARS + extra
    if not extra:
        assert chars(seen) == MASTER_LISTING_CHARS
        assert removed == list(mcp_server.ORCHESTRATOR_TOOLS)
    else:
        assert removed == []
        only = [tool for tool in seen if tool["name"] in mcp_server.ORCHESTRATOR_TOOLS]
        text = sum(len(t["description"]) + len(json.dumps(t["inputSchema"])) for t in only)
        assert text <= ORCHESTRATOR_TEXT_CHARS and "outputSchema" not in only[0]


def test_the_interactive_session_sees_what_the_orchestrator_sees(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert removed_for(None, monkeypatch) == removed_for("orchestrator", monkeypatch)


# -- who makes a change --------------------------------------------------------


@pytest.fixture
def actors(monkeypatch: pytest.MonkeyPatch) -> list[board.Actor]:
    """The actor of every board.add/update call, in order."""
    seen: list[board.Actor] = []
    for name in ("add", "update"):
        original = getattr(board, name)

        def spy(*args: Any, original: Callable = original, **kwargs: Any) -> Any:
            seen.append(kwargs.get("actor", args[-1]))
            return original(*args, **kwargs)

        monkeypatch.setattr(board, name, spy)
    return seen


def test_the_owner_and_agents_are_told_apart(
    orch: Orchestrator, project: str, actors: list[board.Actor], monkeypatch: pytest.MonkeyPatch
) -> None:
    item = orch.add_board_item(project, "by the owner")
    orch.update_board_item(item.id, {"state": "ready"})
    lead_id = lead(orch, project)
    monkeypatch.setenv("ORCH_AGENT_ID", lead_id)
    added = mcp_server.add_board_item(project, "by the lead")
    mcp_server.update_board_item(added["id"], state="done")
    monkeypatch.delenv("ORCH_AGENT_ID")
    mcp_server.update_board_item(added["id"], title="by the chat session")

    agent = board.Actor(board.AGENT_KIND, lead_id)
    chat = board.Actor(board.AGENT_KIND, None)
    assert actors == [board.OWNER, board.OWNER, agent, agent, chat]
    assert board.Actor("owner", None) == board.OWNER


def test_every_change_reaches_the_record_hook(
    orch: Orchestrator, project: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(Orchestrator, "_launch", lambda self, run: None)
    seen: list[tuple[int, str, dict, int | None]] = []

    def spy(conn, item_id, actor, action, changes, stamp, run_id=None) -> None:
        assert actor == board.OWNER and stamp
        seen.append((item_id, action, changes, run_id))

    monkeypatch.setattr(board, "record", spy)
    first = orch.add_board_item(project, "first", "d").id
    second = orch.add_board_item(project, "second").id
    orch.update_board_item(first, {"description": "e", "title": "1st"})
    orch.update_board_item(first, {"state": "ready"})
    orch.update_board_item(second, {"state": "ready"})
    orch.move_board_item(second, "up")
    orch.move_board_item(second, "up")  # at the top already: no change
    result = orch.dispatch_board(project, [first])

    assert seen == [
        (first, "add", {"state": [None, "idea"]}, None),
        (second, "add", {"state": [None, "idea"]}, None),
        (first, "edit", {"title": ["first", "1st"], "description": "changed"}, None),
        (first, "state", {"state": ["idea", "ready"]}, None),
        (second, "state", {"state": ["idea", "ready"]}, None),
        (second, "move", {"position": [1, 0]}, None),
        (
            first,
            "dispatch",
            {"state": ["ready", "in_progress"], "agent_id": [None, result["agent_id"]]},
            result["run_id"],
        ),
    ]


def test_an_agents_dispatch_comes_from_that_agent(
    orch: Orchestrator, project: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(Orchestrator, "_launch", lambda self, run: None)
    orch.add_board_item(project, "x", state="ready")
    old_lead = lead(orch, project)  # made before the orchestrator agent: adopted
    orch.orchestrator()

    actor = board.Actor(board.AGENT_KIND, ORCHESTRATOR_ID)
    result = orch.dispatch_board(project, actor=actor)

    assert result["agent_id"] == old_lead
    assert orch.agent(old_lead).parent_id == ORCHESTRATOR_ID
    assert orch.store.meta(result["run_id"]).sender == ORCHESTRATOR_ID
    assert "dispatched by orchestrator:" in orch.store.runs_of(old_lead)[0].prompt
