"""The supported harnesses and their availability: providers.locate (finding and probing a
CLI), core/availability (the per-process check), the facade, `/api/state`, `/api/providers`,
`spartito providers`.

No real `claude` or `codex` runs: the probe tests use `#!/bin/sh` fakes in a temporary
directory used as PATH, and everything else gets conftest's faked check."""

from __future__ import annotations

import json
import os
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import click
import pytest
from conftest import FAKED_PROBE
from fakeprovider import FakeProvider
from starlette.testclient import TestClient
from typer.testing import CliRunner

from orchestrator import cli, config, providers
from orchestrator.core import availability
from orchestrator.providers import codex, locate
from orchestrator.service import Orchestrator
from orchestrator.web.app import app

CONTRACT = {
    "name",
    "label",
    "description",
    "executable",
    "efforts",
    "models",
    "models_note",
    "features",
    "limits",
    "status",
    "usable",
    "version",
    "path",
    "reason",
    "checked_at",
    "default",
}
FEATURES = {
    "cost",
    "compaction",
    "compaction_instructions",
    "context_size",
    "role_prompt",
    "attribution",
}


def until(predicate: Callable[[], Any], timeout: float = 30) -> Any:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if found := predicate():
            return found
        time.sleep(0.05)
    raise AssertionError("timed out")


def script(directory: Path, name: str, body: str, shebang: str = "#!/bin/sh") -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_text(f"{shebang}\n{body}\n")
    path.chmod(0o755)
    return path


@pytest.fixture
def bare_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """PATH is one empty temporary directory: only the fakes a test puts there are found."""
    directory = tmp_path / "bin"
    directory.mkdir()
    monkeypatch.setenv("PATH", str(directory))
    return directory


# -- providers.locate --------------------------------------------------------------------


def test_a_cli_on_path_is_available_with_its_version(bare_path: Path) -> None:
    path = script(bare_path, "codex", 'echo "codex-cli 0.155.1"')

    found = locate.probe("codex")

    assert found == locate.Probe(
        "available", "0.155.1", str(path), "found on PATH; login not checked"
    )


def test_a_cli_in_a_usual_install_location_is_found_off_path(
    tmp_path: Path, bare_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    extra = tmp_path / "usual"
    path = script(extra, "claude", 'echo "2.1.322 (Claude Code)"')
    monkeypatch.setattr(locate, "EXTRA_DIRS", (str(tmp_path / "missing"), str(extra)))

    found = locate.probe("claude")

    assert (found.status, found.version, found.path) == ("available", "2.1.322", str(path))
    assert found.reason == f"found in {extra}, not on PATH; login not checked"
    assert locate.find("claude") == (path, False)


def test_the_search_path_appends_existing_usual_dirs_after_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first, second = tmp_path / "a", tmp_path / "b"
    first.mkdir()
    second.mkdir()
    monkeypatch.setattr(locate, "EXTRA_DIRS", (str(second), str(tmp_path / "gone"), str(first)))

    assert locate.search_path(f"/x{os.pathsep}{first}") == os.pathsep.join(
        ["/x", str(first), str(second)]
    )


def test_a_missing_cli_is_not_installed_with_the_reinstall_hint(bare_path: Path) -> None:
    found = locate.probe("codex")

    assert (found.status, found.version, found.path) == ("not_installed", None, None)
    assert "`codex` was not found on PATH or in the usual install locations" in found.reason
    assert f"`{config.CLI_NAME} service install`" in found.reason


def test_a_script_whose_env_interpreter_is_missing_is_not_installed(
    tmp_path: Path, bare_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    script(bare_path, "codex", 'echo "0.1.0"', shebang="#!/usr/bin/env node")

    found = locate.probe("codex")

    assert found.status == "not_installed"
    assert found.reason == f"found {bare_path / 'codex'}, but its interpreter `node` is not on PATH"


def test_node_next_to_the_cli_in_a_usual_location_is_found(
    tmp_path: Path, bare_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """npm, Homebrew and Volta put `node` next to the CLI's link: both are found there."""
    extra = tmp_path / "usual"
    script(extra, "codex", 'echo "codex-cli 0.155.1"', shebang="#!/usr/bin/env node")
    script(extra, "node", 'exec /bin/sh "$@"')  # runs the "node script" as a shell script
    monkeypatch.setattr(locate, "EXTRA_DIRS", (str(extra),))

    found = locate.probe("codex")

    assert (found.status, found.version) == ("available", "0.155.1")


def test_a_missing_absolute_interpreter_is_not_installed(bare_path: Path) -> None:
    script(bare_path, "claude", "echo 1.0", shebang="#!/nonexistent/interpreter")

    found = locate.probe("claude")

    assert found.status == "not_installed"
    assert "its interpreter /nonexistent/interpreter does not exist" in found.reason


def test_a_cli_that_does_not_answer_is_unknown(bare_path: Path) -> None:
    script(bare_path, "codex", "exec /bin/sleep 30")

    started = time.monotonic()
    found = locate.probe("codex", timeout=0.5)

    assert time.monotonic() - started < 5
    assert found.status == "unknown" and found.version is None
    assert found.reason == "`codex --version` did not answer within 0.5 s"


def test_a_failing_version_command_is_unknown_with_its_last_line(bare_path: Path) -> None:
    script(bare_path, "claude", "echo starting >&2; echo 'something broke' >&2; exit 3")

    found = locate.probe("claude")

    assert found.status == "unknown"
    assert found.reason == "`claude --version` exited with code 3: something broke"


def test_the_version_command_gets_no_input_and_only_its_arguments(bare_path: Path) -> None:
    script(bare_path, "codex", 'read -r line && echo "got input"; echo "args: $*" >&2; exit 4')

    found = locate.probe("codex", ("-V",))

    assert found.reason == "`codex -V` exited with code 4: args: -V"


def test_a_declared_login_check_reports_not_logged_in(bare_path: Path) -> None:
    body = 'if [ "$1" = login ]; then echo "Not logged in" >&2; exit 1; fi; echo 9.9.9'
    script(bare_path, "mycli", body)

    found = locate.probe("mycli", login_args=("login", "status"))

    assert (found.status, found.version) == ("not_logged_in", "9.9.9")
    assert found.reason == "found on PATH; not logged in: Not logged in"


# -- the self-description ------------------------------------------------------------------


def test_the_builtin_providers_describe_themselves() -> None:
    assert [cls.name for cls in providers.BUILTIN] == ["claude", "codex"]
    assert list(providers.PROVIDERS)[:2] == ["claude", "codex"]
    by_name = {cls.name: cls for cls in providers.BUILTIN}
    assert by_name["claude"].label == "Claude Code" and by_name["codex"].label == "Codex"
    assert by_name["claude"].executable == "claude" and by_name["codex"].executable == "codex"
    assert by_name["claude"].login_args is None and by_name["codex"].login_args is None
    assert by_name["claude"].limits == () and len(by_name["codex"].limits) == 4


def test_a_provider_without_the_new_attributes_still_lists(orch: Orchestrator) -> None:
    """The fake provider (as an ORCH_PROVIDERS plugin) declares none of them."""
    entry = next(e for e in orch.provider_list() if e["name"] == "fake")

    assert set(entry) == CONTRACT and set(entry["features"]) == FEATURES
    assert entry["label"] == "fake" and entry["description"] == "" and entry["limits"] == []
    assert entry["executable"] is None
    assert (entry["status"], entry["usable"], entry["reason"]) == (
        "unknown",
        True,
        availability.NO_EXECUTABLE,
    )
    assert entry["features"] == {
        "cost": None,
        "compaction": True,
        "compaction_instructions": True,
        "context_size": None,
        "role_prompt": None,
        "attribution": False,
    }
    assert entry["models"] == [
        {"value": "fake-small", "label": "fake-small"},
        {"value": "fake-large", "label": "fake-large"},
    ]


def test_the_listing_follows_the_contract(orch: Orchestrator) -> None:
    listed = {entry["name"]: entry for entry in orch.provider_list()}

    claude, codex_entry = listed["claude"], listed["codex"]
    assert set(codex_entry) == CONTRACT
    assert codex_entry["features"] == {
        "cost": False,
        "compaction": True,
        "compaction_instructions": False,
        "context_size": True,
        "role_prompt": "first_turn",
        "attribution": True,
    }
    assert claude["features"]["cost"] is True and claude["features"]["compaction_instructions"]
    assert codex_entry["models"] == [
        {"value": "gpt-test-codex", "label": "gpt-test-codex"},
        {"value": "gpt-test-mini", "label": "gpt-test-mini"},
    ]
    assert (codex_entry["status"], codex_entry["version"], codex_entry["reason"]) == (
        "available",
        "test",
        FAKED_PROBE,
    )
    assert codex_entry["checked_at"] and codex_entry["efforts"] == list(codex.CodexProvider.efforts)
    assert [e["name"] for e in listed.values() if e["default"]] == ["claude"]


# -- core/availability -----------------------------------------------------------------------


def counting_probe(
    monkeypatch: pytest.MonkeyPatch, statuses: dict[str, str] | None = None, sleep: float = 0
) -> list[str]:
    """Replace the check: each call noted, the status from `statuses` (default available)."""
    calls: list[str] = []

    def probe(provider: Any) -> locate.Probe:
        calls.append(provider.name)
        time.sleep(sleep)
        status = (statuses or {}).get(provider.name, "available")
        return locate.Probe(status, "1.0", None, f"{status} in this test")

    monkeypatch.setattr(availability, "probe", probe)
    return calls


def test_checks_are_kept_until_they_are_old_or_refreshed(
    orch: Orchestrator, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = counting_probe(monkeypatch)

    orch.provider_list()
    orch.provider_list()
    assert calls.count("codex") == 1
    orch.provider_list(refresh=True)
    assert calls.count("codex") == 2
    monkeypatch.setattr(availability, "TTL_SECONDS", 0)
    orch.provider_list()
    assert calls.count("codex") == 3


def test_without_waiting_nothing_blocks_and_one_background_check_fills_in(
    orch: Orchestrator, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = counting_probe(monkeypatch, sleep=0.3)

    started = time.monotonic()
    first = orch.provider_list(wait=False)
    again = orch.provider_list(wait=False)
    assert time.monotonic() - started < 0.25

    assert first == again
    for entry in first:
        assert (entry["status"], entry["reason"], entry["checked_at"]) == (
            "unknown",
            availability.NOT_CHECKED,
            None,
        )
        assert (entry["models"], entry["models_note"]) == ([], availability.MODELS_LOADING)
    assert first[0]["default"] is True  # unknown counts as usable
    until(lambda: all(e["checked_at"] for e in orch.provider_list(wait=False)))
    assert sorted(calls) == sorted(providers.PROVIDERS)  # each once: one thread


def test_models_are_asked_only_of_usable_providers(
    orch: Orchestrator, monkeypatch: pytest.MonkeyPatch
) -> None:
    counting_probe(monkeypatch, {"claude": "not_installed", "codex": "not_installed"})

    def no_list() -> str:
        raise AssertionError("asked a provider that is not installed")

    monkeypatch.setattr(codex, "request_model_list", no_list)
    monkeypatch.setattr(providers.claude, "request_model_list", no_list)

    listed = {entry["name"]: entry for entry in orch.provider_list()}

    assert listed["codex"]["usable"] is False and listed["codex"]["models"] == []
    assert listed["codex"]["models_note"] == availability.MODELS_UNAVAILABLE
    assert [e["name"] for e in listed.values() if e["default"]] == ["fake"]
    assert availability.status("claude")["reason"] == "not_installed in this test"


def test_the_default_is_the_first_usable_in_registry_order(
    orch: Orchestrator, monkeypatch: pytest.MonkeyPatch
) -> None:
    counting_probe(monkeypatch, {"claude": "not_installed"})

    listed = orch.provider_list()

    assert [(e["name"], e["default"]) for e in listed][:2] == [("claude", False), ("codex", True)]


def test_the_table_check_does_not_ask_for_models(
    orch: Orchestrator, monkeypatch: pytest.MonkeyPatch
) -> None:
    asked: list[int] = []
    monkeypatch.setattr(codex, "request_model_list", lambda: asked.append(1) or "[]")

    entry = next(e for e in orch.provider_list(models=False) if e["name"] == "codex")
    assert asked == [] and entry["models_note"] == availability.MODELS_LOADING
    entry = next(e for e in orch.provider_list() if e["name"] == "codex")
    assert asked == [1]


def test_an_empty_codex_model_list_is_asked_for_again_later(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    answers = iter(["", '{"models":[{"slug":"gpt-y","visibility":"list"}]}'])
    asked: list[int] = []

    def request() -> str:
        asked.append(1)
        return next(answers)

    monkeypatch.setattr(codex, "request_model_list", request)
    provider = codex.CodexProvider()

    assert provider.models() == [] and provider.models() == []
    assert len(asked) == 1
    monkeypatch.setattr(codex, "MODEL_LIST_RETRY_SECONDS", 0)
    assert provider.models() == ["gpt-y"]
    assert provider.models() == ["gpt-y"] and len(asked) == 2  # a full list is kept


# -- the dashboard's API ---------------------------------------------------------------------


def test_state_never_waits_for_a_model_list(
    orch: Orchestrator, monkeypatch: pytest.MonkeyPatch
) -> None:
    release = threading.Event()

    def slow_models(self: FakeProvider) -> list[str]:
        release.wait(10)
        return ["fake-small"]

    monkeypatch.setattr(FakeProvider, "models", slow_models)
    client = TestClient(app, base_url="http://localhost")
    try:
        started = time.monotonic()
        first = client.get("/api/state").json()["providers"]
        second = client.get("/api/state").json()["providers"]
        assert time.monotonic() - started < 1
    finally:
        release.set()
    fake = next(e for e in second if e["name"] == "fake")
    assert fake["models_note"] == availability.MODELS_LOADING
    assert {"name", "efforts", "models"} <= set(first[0])  # what the page reads today
    until(lambda: availability._refresher is None or not availability._refresher.is_alive())
    fake = next(e for e in client.get("/api/state").json()["providers"] if e["name"] == "fake")
    assert fake["models"] == [{"value": "fake-small", "label": "fake-small"}]


def test_the_providers_endpoint_waits_and_refreshes(
    orch: Orchestrator, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = counting_probe(monkeypatch)
    client = TestClient(app, base_url="http://localhost")

    listed = client.get("/api/providers").json()["providers"]
    client.get("/api/providers")
    assert calls.count("claude") == 1
    client.get("/api/providers?refresh=1")
    assert calls.count("claude") == 2
    assert [e["name"] for e in listed][:2] == ["claude", "codex"]
    assert listed[0]["status"] == "available" and listed[0]["checked_at"]


def test_the_server_starts_checking_at_once(
    orch: Orchestrator, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = counting_probe(monkeypatch)

    with TestClient(app, base_url="http://localhost"):
        until(lambda: len(calls) == len(providers.PROVIDERS))


# -- spartito providers -----------------------------------------------------------------------


def test_the_providers_command_prints_a_table(
    orch: Orchestrator, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = counting_probe(monkeypatch, {"claude": "not_installed"})
    orch.provider_list()

    result = CliRunner().invoke(cli.app, ["providers"])

    assert result.exit_code == 0, result.output
    lines = click.unstyle(result.output).splitlines()
    assert lines[0].split() == ["NAME", "STATUS", "VERSION", "REASON"]
    rows = {line[2:].split()[0]: line for line in lines[1:]}
    assert rows["claude"].startswith("  claude ") and "not_installed in this test" in rows["claude"]
    assert rows["codex"].startswith("* codex ") and " available " in rows["codex"]
    assert rows["fake"].split()[1:3] == ["available", "1.0"]
    assert calls.count("claude") == 2  # checked again, not taken from the cache


def test_the_providers_command_prints_json(orch: Orchestrator) -> None:
    result = CliRunner().invoke(cli.app, ["providers", "--json"])

    assert result.exit_code == 0, result.output
    listed = json.loads(result.output)
    assert [e["name"] for e in listed][:2] == ["claude", "codex"]
    assert all(set(entry) == CONTRACT for entry in listed)
    assert listed[1]["models"][0] == {"value": "gpt-test-codex", "label": "gpt-test-codex"}
