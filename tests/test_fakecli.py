"""The fake CLI (tests/fakecli): every built-in provider has one, and Codex's provider runs
turns through the runner on it (FakeCodex), as Claude's does in test_mcp_wiring."""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import fakecli
import pytest
from conftest import PROVIDER_CLIS
from fakecli import codex as codex_dialect
from fakeprovider import FakeCodex, codex_runs

from orchestrator import config, providers
from orchestrator.models import RunStatus
from orchestrator.service import Orchestrator


def test_every_builtin_provider_has_a_fake_cli(fake_provider_clis: Path) -> None:
    for cls in providers.BUILTIN:
        assert cls.executable in PROVIDER_CLIS
        assert Path(shutil.which(cls.executable)).parent == fake_provider_clis
        assert cls.executable in fakecli.DIALECTS, f"no dialect in tests/fakecli for {cls.name}"


@pytest.fixture
def codex_turns(orch: Orchestrator, project: str, monkeypatch: pytest.MonkeyPatch) -> str:
    """FakeCodex as `codex`, here and in runners. Returns the project."""
    monkeypatch.setenv(
        "ORCH_PROVIDERS", "fake=fakeprovider:FakeProvider,codex=fakeprovider:FakeCodex"
    )
    monkeypatch.setitem(providers.PROVIDERS, "codex", FakeCodex())
    monkeypatch.setenv("FAKE_CODEX_ENV", "ORCH_HOME")
    return project


def finished(orch: Orchestrator, agent_id: str) -> None:
    idle, _ = orch.wait([agent_id], timeout=60)
    assert idle


def test_a_codex_turn_and_its_resumed_turn_on_the_fake_cli(
    orch: Orchestrator, codex_turns: str
) -> None:
    agent, first = orch.spawn(codex_turns, "hello TOKENS 1000", provider="codex", worktree=False)
    finished(orch, agent.id)

    run = orch.store.get_run(first.id)
    assert run.status is RunStatus.DONE
    assert run.result.startswith("done: ") and run.result.endswith("hello TOKENS 1000")
    assert orch.store.get_agent(agent.id).session_id == codex_dialect.SESSION
    (seen,) = codex_runs(config.run_dir(first.id))
    assert seen["argv"][:2] == ["exec", "--json"] and seen["argv"][-1] == "-"
    assert seen["prompt"].endswith("\n\n---\n\nhello TOKENS 1000")  # the role opens it
    assert seen["env"] == {"ORCH_HOME": os.environ["ORCH_HOME"]}
    usage = orch.store.usage_of(agent.id)[first.id]
    tokens = (usage.input_tokens, usage.output_tokens, usage.cache_read_tokens)
    assert tokens == (1000, 100, 2000) and usage.cache_creation_tokens == 500
    assert run.cost_usd is None  # Codex reports no cost

    second = orch.send(agent.id, "FAIL")
    finished(orch, agent.id)

    run = orch.store.get_run(second.id)
    assert run.status is RunStatus.FAILED and "boom" in (run.error or "")
    (seen,) = codex_runs(config.run_dir(second.id))
    assert seen["argv"][:3] == ["exec", "resume", codex_dialect.SESSION]
    assert seen["prompt"] == "FAIL"  # a resumed thread has its role already
