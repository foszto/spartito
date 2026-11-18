"""Agent capabilities (capabilities.py): the registry, the settings, the resolution for a
turn, the providers' wirings and what the runner records."""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path

import pytest
from fakecapability import FAKE_TOOL

from orchestrator import capabilities, config, providers, settings
from orchestrator.capabilities import Capability
from orchestrator.core import availability
from orchestrator.errors import OrchestratorError
from orchestrator.loadout import Loadout
from orchestrator.models import Agent
from orchestrator.providers import locate
from orchestrator.providers.base import McpServer, RunContext, Support, Wiring
from orchestrator.providers.claude import CHROME as CLAUDE_CHROME
from orchestrator.providers.claude import NO_BACKGROUND_ENV
from orchestrator.service import Orchestrator
from orchestrator.store import Store

CFC_OFF = {"CLAUDE_CODE_ENABLE_CFC": "0"}


@pytest.fixture
def store(tmp_path: Path) -> Store:
    return Store(tmp_path / "orch.db")


@pytest.fixture(params=[frozenset(), frozenset({"chrome"})], ids=["default-off", "default-on"])
def default_on(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> bool:
    """Both values the product default may have; returns whether chrome is on by default."""
    monkeypatch.setattr(capabilities, "DEFAULT_ON", request.param)
    return "chrome" in request.param


def agent(
    provider: str = "claude",
    project: str | None = "my-app",
    role: str = "worker",
    own: object = None,
) -> Agent:
    stored = own if own is None or isinstance(own, str) else json.dumps(own)
    return Agent(
        id="fix-login-3f2a",
        project=project,
        name="fix-login",
        role=role,
        provider=provider,
        cwd="/w",
        capabilities=stored,
    )


def chrome(resolution: capabilities.Resolution) -> capabilities.Item:
    return next(item for item in resolution.items if item.name == "chrome")


def state(resolution: capabilities.Resolution) -> tuple[str, str]:
    item = chrome(resolution)
    return item.state, item.source


# -- the resolution, level by level -----------------------------------------------------


def test_the_default_decides_without_any_setting(store: Store, default_on: bool) -> None:
    found = capabilities.resolve(store, agent())

    assert state(found) == ("on" if default_on else "off", "default")
    assert found.wirings == ((CLAUDE_CHROME.on,) if default_on else (CLAUDE_CHROME.off,))


@pytest.mark.parametrize("value", [True, False])
def test_the_global_setting_wins_over_the_default(
    store: Store, default_on: bool, value: bool
) -> None:
    store.set_settings("", {"capabilities": {"chrome": value}})

    assert state(capabilities.resolve(store, agent())) == ("on" if value else "off", "global")


@pytest.mark.parametrize("value", [True, False])
def test_the_projects_setting_wins_over_the_global_one(
    store: Store, default_on: bool, value: bool
) -> None:
    store.set_settings("", {"capabilities": {"chrome": not value}})
    store.set_settings("my-app", {"capabilities": {"chrome": value}})

    assert state(capabilities.resolve(store, agent())) == ("on" if value else "off", "project")
    other = capabilities.resolve(store, agent(project="other"))
    assert state(other) == ("off" if value else "on", "global")


def test_the_orchestrator_has_no_project_level(store: Store) -> None:
    store.set_settings("", {"capabilities": {"chrome": True}})
    store.set_settings("my-app", {"capabilities": {"chrome": False}})

    assert state(capabilities.resolve(store, agent(project=None, role="orchestrator"))) == (
        "on",
        "global",
    )


def test_the_roles_setting_wins_over_the_projects(store: Store, default_on: bool) -> None:
    store.set_settings(
        "",
        {
            "capabilities": {"chrome": False},
            "role_capabilities": {"tester": {"chrome": True}},
        },
    )
    store.set_settings("my-app", {"capabilities": {"chrome": False}})

    assert state(capabilities.resolve(store, agent(role="tester"))) == ("on", "role")
    assert state(capabilities.resolve(store, agent(role="worker"))) == ("off", "project")


def test_a_projects_role_entry_wins_over_the_global_one(store: Store) -> None:
    store.set_settings("", {"role_capabilities": {"tester": {"chrome": True}}})
    store.set_settings("my-app", {"role_capabilities": {"tester": {"chrome": False}}})

    assert state(capabilities.resolve(store, agent(role="tester"))) == ("off", "role")
    assert state(capabilities.resolve(store, agent(project="other", role="tester"))) == (
        "on",
        "role",
    )


@pytest.mark.parametrize("value", [True, False])
def test_the_agents_own_setting_wins_last(store: Store, default_on: bool, value: bool) -> None:
    store.set_settings("", {"capabilities": {"chrome": not value}})
    store.set_settings("my-app", {"role_capabilities": {"worker": {"chrome": True}}})

    found = capabilities.resolve(store, agent(own={"chrome": value}))

    assert state(found) == ("on" if value else "off", "agent")
    assert chrome(found).override is value and not chrome(found).final


def test_a_roles_off_is_final_for_the_agents_own_setting(store: Store, default_on: bool) -> None:
    store.set_settings("", {"role_capabilities": {"reviewer": {"chrome": False}}})

    found = capabilities.resolve(store, agent(role="reviewer", own={"chrome": True}))

    item = chrome(found)
    assert (item.state, item.source, item.override, item.final) == ("skipped", "role", True, True)
    assert item.note and "role reviewer" in item.note
    assert found.wirings == (CLAUDE_CHROME.off,)  # not on: the "off" wiring applies
    assert found.record() == {"on": [], "skipped": {"chrome": item.note}}


def test_an_agent_may_turn_off_what_its_role_turns_off(store: Store) -> None:
    store.set_settings("", {"role_capabilities": {"reviewer": {"chrome": False}}})

    found = capabilities.resolve(store, agent(role="reviewer", own={"chrome": False}))

    assert state(found) == ("off", "agent") and chrome(found).note is None


def test_a_provider_without_support_skips_it_with_a_note(store: Store) -> None:
    store.set_settings("", {"capabilities": {"chrome": True}})

    found = capabilities.resolve(store, agent(provider="codex"))

    assert state(found) == ("skipped", "global")
    assert chrome(found).note == "codex does not support it"
    assert found.wirings == () and found.on == []


def test_a_provider_without_support_has_it_off_without_a_wiring(store: Store) -> None:
    found = capabilities.resolve(store, agent(provider="codex"))

    assert state(found) == ("off", "default") and found.wirings == ()


def test_an_unknown_name_that_is_on_is_skipped_at_turn_time(store: Store) -> None:
    store.set_settings("", {"capabilities": {"jira": False}})

    found = capabilities.resolve(store, agent(own={"gone-plugin": True}))

    assert [item.name for item in found.items] == ["chrome", "gone-plugin"]
    item = found.items[1]
    assert (item.state, item.source, item.note) == ("skipped", "agent", "not registered here")
    assert found.record()["skipped"] == {"gone-plugin": "not registered here"}


@pytest.mark.parametrize(
    "stored",
    [
        {"capabilities": "yes", "role_capabilities": ["worker"]},
        {"capabilities": {"chrome": "on"}, "role_capabilities": {"worker": "chrome"}},
        {"capabilities": {"Chrome": True}, "role_capabilities": {"worker": {"chrome": 1}}},
    ],
)
def test_unreadable_stored_values_count_as_absent(
    store: Store, default_on: bool, stored: dict
) -> None:
    store.set_settings("", stored)
    store.set_settings("my-app", stored)

    for own in ("not json", "[1]", json.dumps({"chrome": "yes"}), None):
        found = capabilities.resolve(store, agent(own=own))
        assert state(found) == ("on" if default_on else "off", "default")
        assert chrome(found).override is None


def test_the_breakdown_lists_every_item() -> None:
    found = capabilities.Resolution(
        (capabilities.Item("chrome", "Claude in Chrome", "on", "agent", True, False),)
    )

    assert found.breakdown() == {
        "on": ["chrome"],
        "items": [
            {
                "name": "chrome",
                "label": "Claude in Chrome",
                "state": "on",
                "source": "agent",
                "override": True,
                "final": False,
                "note": None,
            }
        ],
        "error": None,
    }


# -- settings -----------------------------------------------------------------------------


def test_settings_accept_both_keys_and_drop_inherited_values() -> None:
    cleaned = settings.clean(
        {
            "capabilities": {"chrome": True, "other": None},
            "role_capabilities": {"reviewer": {"chrome": False}, "tester": {"chrome": None}},
        }
    )

    assert cleaned == {
        "capabilities": {"chrome": True},
        "role_capabilities": {"reviewer": {"chrome": False}},
    }
    assert settings.clean({"capabilities": {}, "role_capabilities": None}) == {}


@pytest.mark.parametrize(
    ("data", "message"),
    [
        ({"capabilities": ["chrome"]}, "must be an object"),
        ({"capabilities": {"Chrome": True}}, "invalid capability name"),
        ({"capabilities": {"chrome": "on"}}, "true or false"),
        ({"role_capabilities": {"reviewer": ["chrome"]}}, "must be an object"),
        ({"role_capabilities": {"": {"chrome": False}}}, "role names"),
        ({"role_capabilities": "reviewer"}, "object of role names"),
    ],
)
def test_settings_refuse_malformed_values(data: dict, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        settings.clean(data)


def test_an_update_without_the_keys_keeps_them(orch: Orchestrator, project: str) -> None:
    for scope in (None, project):
        orch.update_settings(
            {
                "capabilities": {"chrome": True},
                "role_capabilities": {"reviewer": {"chrome": False}},
            },
            scope,
        )
        orch.update_settings({"worker": {"fake": {"model": "fake-small"}}}, scope)

        assert orch.settings(scope) == {
            "worker": {"fake": {"model": "fake-small"}},
            "capabilities": {"chrome": True},
            "role_capabilities": {"reviewer": {"chrome": False}},
        }
        orch.update_settings({"capabilities": None}, scope)  # named: cleared
        assert "capabilities" not in orch.settings(scope)


def test_a_malformed_update_is_refused(orch: Orchestrator) -> None:
    with pytest.raises(OrchestratorError, match="true or false"):
        orch.update_settings({"capabilities": {"chrome": "maybe"}})


# -- the providers' wirings ---------------------------------------------------------------


def context(tmp_path: Path, wirings: tuple[Wiring, ...] = (), **agent_fields) -> RunContext:
    return RunContext(
        agent=Agent(
            id="a-1", project="p", name="a", role="worker", provider="x", cwd="/w", **agent_fields
        ),
        prompt="do it",
        system_prompt="ROLE",
        scratch=tmp_path,
        wirings=wirings,
    )


@pytest.mark.parametrize("session", [None, "sid"], ids=["new", "resumed"])
def test_claude_gets_chrome_exactly_when_it_is_on(tmp_path: Path, session: str | None) -> None:
    claude = providers.get("claude")

    on = claude.invocation(context(tmp_path, (CLAUDE_CHROME.on,), session_id=session))
    off = claude.invocation(context(tmp_path, (CLAUDE_CHROME.off,), session_id=session))
    plain = claude.invocation(context(tmp_path, session_id=session))

    assert on.argv.count("--chrome") == 1 and "CLAUDE_CODE_ENABLE_CFC" not in on.env
    assert on.argv.index("--chrome") < on.argv.index("--append-system-prompt")
    assert "--chrome" not in off.argv and off.env == CFC_OFF
    assert ("--resume" in on.argv) is bool(session)
    assert "--no-chrome" not in on.argv + off.argv
    # Nothing else of the command line changes (a new session's id is random).
    assert tail(on) == tail(off) == tail(plain)


def tail(invocation) -> list[str]:
    """The command line from the permission mode on, without `--chrome`."""
    argv = invocation.argv
    return [arg for arg in argv[argv.index("--permission-mode") :] if arg != "--chrome"]


def test_a_leads_turn_keeps_its_flags_and_environment(tmp_path: Path) -> None:
    claude = providers.get("claude")

    def lead(wirings: tuple[Wiring, ...]):
        ctx = dataclasses.replace(context(tmp_path, wirings, session_id="sid"), lead=True)
        ctx.mcp = McpServer("python", ["-m", "orchestrator.mcp_server"], {})
        ctx.loadout = Loadout(tools=("Read",), strict_mcp=True)
        return claude.invocation(ctx)

    on, off, plain = lead((CLAUDE_CHROME.on,)), lead((CLAUDE_CHROME.off,)), lead(())

    assert [arg for arg in on.argv if arg != "--chrome"] == plain.argv == off.argv
    for flag in ("--strict-mcp-config", "--tools", "--allowedTools", "--permission-mode"):
        assert on.argv.count(flag) == plain.argv.count(flag) == 1
    assert on.env == plain.env == NO_BACKGROUND_ENV
    assert off.env == {**CFC_OFF, **NO_BACKGROUND_ENV}


def test_codex_is_unchanged_without_wirings_and_places_them_before_the_prompt(
    tmp_path: Path,
) -> None:
    codex = providers.get("codex")
    plain = codex.invocation(context(tmp_path, session_id="sid"))
    wiring = Wiring(argv=("-c", "tools.example=true"), env={"EXAMPLE": "1"})

    found = codex.invocation(context(tmp_path, (wiring,), session_id="sid"))

    assert plain.argv[-1] == "-" and plain.env == {}
    assert found.argv == [*plain.argv[:-1], "-c", "tools.example=true", "-"]
    assert found.env == {"EXAMPLE": "1"}


# -- the registry and plugins -------------------------------------------------------------


@pytest.fixture
def registry(monkeypatch: pytest.MonkeyPatch) -> dict[str, Capability]:
    """A copy of the registry this test may change."""
    found = dict(capabilities.REGISTRY)
    monkeypatch.setattr(capabilities, "REGISTRY", found)
    monkeypatch.setattr(capabilities, "PLUGINS", set())
    return found


def test_chrome_is_built_in_and_only_claude_supports_it() -> None:
    entry = capabilities.REGISTRY["chrome"]

    assert list(entry.providers) == ["claude"]
    assert entry.providers["claude"] == Support(
        on=Wiring(argv=("--chrome",)), off=Wiring(env=CFC_OFF)
    )
    assert "chrome" not in capabilities.PLUGINS


def test_plugins_load_one_entry_or_a_sequence(
    registry: dict[str, Capability], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(providers.PROVIDERS, "fake", object())

    capabilities._load_plugins("fakecapability:BOTH")

    assert list(registry) == ["chrome", "fake-tool", "fake-other"]
    assert {"fake-tool", "fake-other"} == capabilities.PLUGINS
    with pytest.raises(ValueError, match="registered twice"):
        capabilities._load_plugins("fakecapability:FAKE_TOOL")
    with pytest.raises(ModuleNotFoundError):
        capabilities._load_plugins("no_such_module:X")


@pytest.mark.parametrize("name", ["Chrome", "1x", "with space", "a" * 33, ""])
def test_invalid_names_are_refused(registry: dict[str, Capability], name: str) -> None:
    with pytest.raises(ValueError, match="invalid capability name"):
        capabilities.register(Capability(name, "X", "x", "x", {}))
    assert name not in registry


def test_a_provider_that_is_not_registered_is_warned_about(
    registry: dict[str, Capability], capsys: pytest.CaptureFixture[str]
) -> None:
    entry = Capability("later", "Later", "x", "x", {"not-here": Support(on=Wiring())})

    capabilities.register(entry)

    assert registry["later"] is entry
    assert "names provider 'not-here', which is not registered here" in capsys.readouterr().err


def test_names_are_checked_when_assigned() -> None:
    assert capabilities.known("chrome") is capabilities.CHROME
    with pytest.raises(ValueError, match=r"unknown capability 'jira' \(known: chrome"):
        capabilities.known("jira")


# -- the status check ---------------------------------------------------------------------

CLAUDE_USABLE = {"name": "claude", "label": "Claude Code", "usable": True, "reason": None}


@pytest.fixture
def no_login_variables(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in capabilities.NOT_CLAUDE_AI:
        monkeypatch.delenv(name, raising=False)


def test_chrome_is_unknown_when_nothing_can_be_seen(no_login_variables: None) -> None:
    assert capabilities.status(capabilities.CHROME, [CLAUDE_USABLE]) == (
        "unknown",
        capabilities.CHROME_UNSEEN,
    )


@pytest.mark.parametrize("name", capabilities.NOT_CLAUDE_AI)
def test_chrome_is_unavailable_without_a_claude_ai_login(
    no_login_variables: None, monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    monkeypatch.setenv(name, "1")  # a stand-in: only whether it is set counts

    found = capabilities.status(capabilities.CHROME, [CLAUDE_USABLE])

    assert found == ("unavailable", f"{name} is set: Claude in Chrome needs a claude.ai login")


def test_a_variable_set_to_nothing_or_zero_is_not_set(
    no_login_variables: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    monkeypatch.setenv("CLAUDE_CODE_USE_BEDROCK", "0")

    assert capabilities.status(capabilities.CHROME, [CLAUDE_USABLE])[0] == "unknown"


def test_chrome_is_unavailable_without_a_usable_claude(no_login_variables: None) -> None:
    missing = {**CLAUDE_USABLE, "usable": False, "reason": "not installed (not on PATH)"}

    found = capabilities.status(capabilities.CHROME, [missing])

    assert found == ("unavailable", "needs Claude Code: not installed (not on PATH)")


def test_the_status_reads_the_providers_listing(
    no_login_variables: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    listing = availability.listing(models=False)  # the suite's claude: available
    assert capabilities.status(capabilities.CHROME, listing)[0] == "unknown"
    monkeypatch.setattr(
        availability,
        "probe",
        lambda provider: locate.Probe(locate.NOT_INSTALLED, None, None, "not installed"),
    )
    availability.reset()

    found = capabilities.status(capabilities.CHROME, availability.listing(models=False))

    assert found == ("unavailable", "needs Claude Code: not installed")


def test_a_plugins_check_decides_and_may_not_break_the_listing() -> None:
    fine = Capability(
        "x", "X", "x", "x", {"claude": Support(on=Wiring())}, check=lambda: ("available", "ok")
    )

    def broken() -> tuple[str, str]:
        raise RuntimeError("boom")

    assert capabilities.status(fine, [CLAUDE_USABLE]) == ("available", "ok")
    assert capabilities.status(
        Capability("x", "X", "x", "x", {"claude": Support(on=Wiring())}, check=broken),
        [CLAUDE_USABLE],
    ) == ("unknown", "its check failed: boom")
    assert capabilities.status(Capability("x", "X", "x", "x", {}), [CLAUDE_USABLE])[0] == (
        "unavailable"
    )


# -- turns --------------------------------------------------------------------------------


@pytest.fixture
def fake_tool(monkeypatch: pytest.MonkeyPatch, registry: dict[str, Capability]) -> None:
    """`fake-tool` registered here and in every runner (ORCH_CAPABILITIES)."""
    monkeypatch.setenv(capabilities.PLUGINS_ENV, "fakecapability:FAKE_TOOL")
    registry["fake-tool"] = FAKE_TOOL


def seen(run_id: int) -> dict:
    return json.loads((config.run_dir(run_id) / "ctx.json").read_text())


def notes(orch: Orchestrator, agent_id: str) -> list[str]:
    return [
        e.text
        for e in orch.store.events(agent_id)
        if e.kind == "info" and e.text.startswith("capabilities for this turn")
    ]


def finish(orch: Orchestrator, agent_id: str) -> None:
    done, _ = orch.wait([agent_id], timeout=30)
    assert done, "agent did not finish in time"


def set_global(orch: Orchestrator, values: dict) -> None:
    data = orch.settings()
    data["capabilities"] = values
    orch.update_settings(data)


def test_a_turn_gets_its_wirings_and_records_them(
    orch: Orchestrator, project: str, fake_tool: None
) -> None:
    set_global(orch, {"fake-tool": True})
    agent, first = orch.spawn(project, "hello", worktree=False)
    finish(orch, agent.id)
    second = orch.send(agent.id, "again")
    finish(orch, agent.id)

    on = [{"argv": ["--fake-tool"], "env": {"FAKE_TOOL": "1"}}]
    assert seen(first.id)["wirings"] == seen(second.id)["wirings"] == on
    for run in (first, second):
        assert json.loads(orch.store.meta(run.id).capabilities) == {
            "on": ["fake-tool"],
            "skipped": {},
        }
    assert notes(orch, agent.id) == ["capabilities for this turn: fake-tool"]  # once

    set_global(orch, {"fake-tool": False})
    third = orch.send(agent.id, "and again")
    finish(orch, agent.id)

    assert seen(third.id)["wirings"] == [{"argv": [], "env": {"FAKE_TOOL": "0"}}]
    assert notes(orch, agent.id) == [
        "capabilities for this turn: fake-tool",
        "capabilities for this turn: none",
    ]


def test_a_first_turn_without_any_says_nothing(orch: Orchestrator, project: str) -> None:
    agent, run = orch.spawn(project, "hello", worktree=False)
    finish(orch, agent.id)

    assert seen(run.id)["wirings"] == []
    assert json.loads(orch.store.meta(run.id).capabilities) == {"on": [], "skipped": {}}
    assert notes(orch, agent.id) == []


def test_a_skipped_capability_is_noted_on_the_first_turn(orch: Orchestrator, project: str) -> None:
    set_global(orch, {"chrome": True})  # the fake provider does not support it
    agent, run = orch.spawn(project, "hello", worktree=False)
    finish(orch, agent.id)

    assert seen(run.id)["wirings"] == []
    assert notes(orch, agent.id) == [
        "capabilities for this turn: none; skipped: chrome (fake does not support it)"
    ]


def test_a_compaction_turn_gets_the_same_wirings(
    orch: Orchestrator, project: str, fake_tool: None
) -> None:
    set_global(orch, {"fake-tool": True})
    agent, _ = orch.spawn(project, "hello", worktree=False)
    finish(orch, agent.id)

    compaction = orch.compact(agent.id)
    finish(orch, agent.id)

    assert seen(compaction.id)["wirings"] == [{"argv": ["--fake-tool"], "env": {"FAKE_TOOL": "1"}}]
    assert json.loads(orch.store.meta(compaction.id).capabilities)["on"] == ["fake-tool"]
    assert len(notes(orch, agent.id)) == 1


def test_the_agents_own_setting_reaches_its_next_turn(
    orch: Orchestrator, project: str, fake_tool: None
) -> None:
    agent, first = orch.spawn(project, "hello", worktree=False)
    finish(orch, agent.id)
    orch.store.set_agent_capabilities(agent.id, {"fake-tool": True})
    second = orch.send(agent.id, "again")
    finish(orch, agent.id)

    assert seen(first.id)["wirings"] == [{"argv": [], "env": {"FAKE_TOOL": "0"}}]
    assert seen(second.id)["wirings"] == [{"argv": ["--fake-tool"], "env": {"FAKE_TOOL": "1"}}]
    assert notes(orch, agent.id) == ["capabilities for this turn: fake-tool"]
