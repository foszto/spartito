"""The providers' shared building blocks (providers/kit.py), and a plugin that knows nothing
of them."""

from __future__ import annotations

import json
import os
import subprocess
import time
from dataclasses import replace
from pathlib import Path

import pytest
from minimalprovider import MinimalProvider

from orchestrator import autostart, providers, runner
from orchestrator.core import setup
from orchestrator.models import Agent, Event, RunStatus
from orchestrator.providers import McpServer, RunContext, claude, kit, locate
from orchestrator.providers.base import Wiring
from orchestrator.service import Orchestrator


def gone(pid: int) -> bool:
    """The process is gone (reaped; an orphan is reaped by init within moments)."""
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        time.sleep(0.02)
    return False


def script(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "cli"
    path.write_text(f"#!/bin/sh\n{body}\n")
    path.chmod(0o755)
    return path


# -- run_cli ----------------------------------------------------------------------------------


def test_run_cli_gives_the_exit_code_and_both_outputs_and_takes_stdin(tmp_path: Path) -> None:
    cli = script(tmp_path, 'read line\necho "got $line"\necho oops >&2\nexit 3')

    assert kit.run_cli([str(cli)], 5, stdin="hello\n") == (3, "got hello\n", "oops\n")


def test_run_cli_has_no_input_by_default(tmp_path: Path) -> None:
    cli = script(tmp_path, 'cat\necho "end"')

    assert kit.run_cli([str(cli)], 5) == (0, "end\n", "")


def test_run_cli_appends_the_usual_install_dirs_to_the_given_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    extra = tmp_path / "extra"
    extra.mkdir()
    monkeypatch.setattr(locate, "EXTRA_DIRS", (str(extra),))
    cli = script(tmp_path, 'echo "$PATH"\necho "$ONLY_HERE"')

    code, out, _ = kit.run_cli([str(cli)], 5, env={"PATH": "/usr/bin:/bin", "ONLY_HERE": "yes"})

    assert code == 0 and out.splitlines() == [f"/usr/bin:/bin:{extra}", "yes"]


def test_run_cli_kills_its_process_group_on_a_timeout(tmp_path: Path) -> None:
    pids = tmp_path / "pids"
    cli = script(tmp_path, f"sleep 30 &\necho $$ $! > {pids}\nexec sleep 30")
    started = time.monotonic()

    with pytest.raises(subprocess.TimeoutExpired):
        kit.run_cli([str(cli)], 0.5)

    assert time.monotonic() - started < 0.5 + locate.PIPES_SECONDS + 2
    cli_pid, child_pid = map(int, pids.read_text().split())
    assert gone(cli_pid) and gone(child_pid)  # its children too


# -- ModelCatalog -----------------------------------------------------------------------------


def catalog_of(*answers: kit.Models, retry_when=lambda models, note: False):
    """A provider object whose class has a ModelCatalog, and its answers in order."""
    pending = list(answers)

    class Provider:
        catalog = kit.ModelCatalog(lambda: pending.pop(0), retry_when=retry_when)

        def models(self) -> list:
            return self.catalog.models(self)

    return Provider, pending


def test_a_model_catalog_looks_up_once_and_keeps_an_answer_it_need_not_retry() -> None:
    provider_class, pending = catalog_of((["m"], None))
    provider = provider_class()

    assert provider.models() == provider.models() == ["m"] and provider.models_note is None
    provider._models_at -= kit.MODEL_LIST_RETRY_SECONDS
    assert provider.models() == ["m"] and not pending


def test_a_model_catalog_asks_again_by_its_rule_once_the_answer_is_old() -> None:
    asked: list[tuple] = []

    def retry_when(models: list, note: str | None) -> bool:
        asked.append((models, note))
        return note is not None

    provider = catalog_of(([], "none yet"), (["m"], None), retry_when=retry_when)[0]()

    assert provider.models() == [] and provider.models_note == "none yet"
    assert provider.models() == []  # too young to ask again
    provider._models_at -= kit.MODEL_LIST_RETRY_SECONDS
    assert provider.models() == ["m"] and provider.models_note is None
    assert asked[-1] == ([], "none yet")


def test_a_model_catalog_reads_the_kits_interval_when_it_needs_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider_class, pending = catalog_of(
        ([], None), (["m"], None), retry_when=lambda models, note: not models
    )
    provider = provider_class()
    assert provider.models() == []

    monkeypatch.setattr(kit, "MODEL_LIST_RETRY_SECONDS", 0)

    assert provider.models() == ["m"] and not pending


def test_each_provider_object_has_its_own_list() -> None:
    """As before the kit: a new object (each test's provider) looks up afresh."""
    provider_class, pending = catalog_of((["a"], None), (["b"], None))

    assert provider_class().models() == ["a"] and provider_class().models() == ["b"]
    assert not pending


class OwnInit(claude.ClaudeProvider):
    """A plugin's subclass with an `__init__` of its own that does not call super()."""

    def __init__(self) -> None:
        self.models_note = "set by the plugin"


def test_a_subclass_without_super_init_lists_models_and_sets_its_note(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def missing() -> str:
        raise FileNotFoundError("claude")

    monkeypatch.setattr(claude, "request_model_list", missing)
    provider = OwnInit()
    assert provider.models_note == "set by the plugin"

    assert provider.models() == list(claude.FALLBACK_MODELS)
    assert provider.models_note == claude.UNAVAILABLE_NOTE
    provider.models_note = None  # assignable, as before the kit
    assert provider.models_note is None


# -- JsonLines --------------------------------------------------------------------------------


class Objects(kit.JsonLines):
    def on_json(self, data: dict) -> list[Event]:
        return [Event("text", data["say"])]


@pytest.mark.parametrize(
    ("line", "events"),
    [
        ("not json\n", [("info", "not json")]),
        ('{"say": "hi"}\n', [("text", "hi")]),
        ("\n", []),
        ("   \n", []),
        ("[1, 2]\n", [("info", "[1, 2]")]),
    ],
)
def test_json_lines_hand_objects_on_and_report_anything_else(
    line: str, events: list[tuple[str, str]]
) -> None:
    assert [(e.kind, e.text) for e in Objects().feed(line)] == events


def test_json_lines_say_nothing_at_the_end_by_default() -> None:
    assert kit.JsonLines().finish(0) == [] and kit.JsonLines().on_json({}) == []


# -- the turn's MCP servers, schema and role prompt -------------------------------------------


def context(tmp_path: Path, **fields) -> RunContext:
    agent = Agent(id="a-1", project="my-app", name="a", role="worker", provider="x", cwd="/w")
    return RunContext(agent=agent, prompt="do it", system_prompt="ROLE", scratch=tmp_path, **fields)


def test_the_turn_servers_are_the_own_one_first_then_the_wirings_but_its_name(
    tmp_path: Path,
) -> None:
    own = McpServer(command="python", args=["-m", "orchestrator.mcp_server"], env={"A": "1"})
    entry = {"command": "python", "args": ["-m", "orchestrator.mcp_server"], "env": {"A": "1"}}
    docs = {"command": "docs-db", "args": [], "env": {}}
    wirings = (Wiring(mcp_servers={"docs-db": docs, "orch": {"command": "evil"}}),)

    servers = kit.turn_servers(context(tmp_path, mcp=own, wirings=wirings))

    assert list(servers) == ["orch", "docs-db"]
    assert servers["orch"] == kit.own_server_entry(own) == entry
    assert servers["docs-db"] == docs
    assert list(kit.turn_servers(context(tmp_path, wirings=wirings))) == ["docs-db"]


def test_the_schema_file_is_in_the_scratch_directory(tmp_path: Path) -> None:
    schema = {"type": "object"}

    path = kit.schema_file(context(tmp_path, output_schema=schema))

    assert path.parent == tmp_path and json.loads(path.read_text()) == schema


def test_the_role_prefix_opens_only_a_new_session(tmp_path: Path) -> None:
    fresh = context(tmp_path)
    resumed = replace(fresh, agent=replace(fresh.agent, session_id="s"))

    assert kit.with_role_prefix(fresh) == "ROLE\n\n---\n\ndo it"
    assert kit.with_role_prefix(resumed) == "do it"
    assert kit.with_role_prefix(replace(fresh, system_prompt="")) == "do it"


# -- the providers' attributes ------------------------------------------------------------------


@pytest.mark.parametrize("name", ["claude", "codex"])
def test_the_provider_attributes_say_what_the_tables_say(name: str) -> None:
    """What the consumers read instead of the tables keyed by name; the attribution texts
    both produce are pinned in test_attribution."""
    provider = providers.get(name)

    assert provider.cache_effect == setup.cache_effect(name)
    assert set(provider.config_env) <= set(runner.MCP_PASSED_ENV) & set(autostart.env_names())
    assert provider.reports_context is isinstance(provider, providers.BUILTIN)
    assert provider.carries_mcp_servers is (name == "claude")  # capabilities.mcp_capability
    assert not hasattr(provider, "min_version")


# -- a plugin that knows nothing of the kit -----------------------------------------------------


def test_a_plugin_with_only_the_required_members_runs_a_turn(
    orch: Orchestrator, project: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(
        "ORCH_PROVIDERS", "fake=fakeprovider:FakeProvider,minimal=minimalprovider:MinimalProvider"
    )
    monkeypatch.setitem(providers.PROVIDERS, "minimal", MinimalProvider())

    agent, run = orch.spawn(project, "hello", worktree=False, provider="minimal")
    idle, _ = orch.wait([agent.id], timeout=60)

    assert idle
    finished = orch.store.get_run(run.id)
    assert (finished.status, finished.result) == (RunStatus.DONE, "done")
