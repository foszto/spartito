"""`scripts/smoke_parallel.py`'s scenario-2 verdict, on recorded facts (no CLI)."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from orchestrator import config

SCRIPTS = Path(__file__).parent.parent / "scripts"
W1 = SimpleNamespace(id="w1-0000", role="worker", branch="orch/w1-0000", base_ref="main")


@pytest.fixture
def smoke(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.syspath_prepend(str(SCRIPTS))
    import smoke_parallel

    return smoke_parallel.Parallel


def agent(agent_id: str, role: str = "worker", base: str = "orch/w1-0000") -> SimpleNamespace:
    return SimpleNamespace(id=agent_id, role=role, branch=f"orch/{agent_id}", base_ref=base)


def call(run: int, tool: str, target: str, **args: object) -> dict[str, object]:
    return {"run": run, "tool": tool, "input": args, "agent": target}


def test_a_message_counts_only_from_the_second_instruction_on_and_with_two(smoke) -> None:
    early = [call(1, "send_message", W1.id, agent_id=W1.id, message="hi")]
    assert smoke.judge(W1, [], early, 3, {W1.id: "one\ntwo\n"}).startswith("other")

    sent = [call(3, "send_message", W1.id, agent_id=W1.id, message="append two")]
    assert smoke.judge(W1, [], sent, 3, {W1.id: "one\n"}).startswith("other")
    assert smoke.judge(W1, [], sent, 3, {W1.id: "one\ntwo\n"}).startswith("acceptable")


def test_the_spawn_call_is_matched_to_the_worker_it_created(smoke) -> None:
    w2 = agent("w2-0000")
    reviewer = agent("rev-0000", role="reviewer", base="main")
    calls = [
        call(3, "spawn_agent", w2.id, task="append", base=W1.id, after=[W1.id]),
        call(5, "spawn_agent", reviewer.id, task="review", role="reviewer"),
    ]
    content = {W1.id: "one\n", w2.id: "one\ntwo\n", reviewer.id: None}

    verdict = smoke.judge(W1, [reviewer, w2], calls, 3, content)
    assert verdict.startswith("acceptable") and w2.id in verdict

    at_once = [call(3, "spawn_agent", "w3-0000", task="append")]
    w3 = agent("w3-0000", base="main")
    assert smoke.judge(W1, [w3], at_once, 3, {w3.id: "two\n"}).startswith("not acceptable")


def test_dispatches_take_the_agent_id_from_the_tool_reply(smoke) -> None:
    run_dir = config.run_dir(3)
    run_dir.mkdir(parents=True)
    use = {"type": "tool_use", "id": "t1", "name": "mcp__orch__spawn_agent", "input": {"task": "x"}}
    reply = {"type": "text", "text": json.dumps({"next": "...", "id": "w2-0000"})}
    result = {"type": "tool_result", "tool_use_id": "t1", "content": [reply]}
    lines = [
        {"type": "assistant", "message": {"content": [use]}},
        {"type": "user", "message": {"content": [result]}},
    ]
    (run_dir / "stream.jsonl").write_text("\n".join(json.dumps(x) for x in lines) + "\n")

    assert smoke.dispatches([3]) == [
        {"run": 3, "tool": "spawn_agent", "input": {"task": "x"}, "agent": "w2-0000"}
    ]
