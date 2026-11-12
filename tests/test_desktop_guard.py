"""A Spartito started from an agent's turn on another home, or with the switch off, shows no
desktop notification by any path (desktop.quiet_reason, decided in desktop.route): the
notice is recorded, claimed at once and traced with the reason. The same home, or no marker,
notifies as before. The runner gives the turn's home to everything of the turn.

Nothing real is shown or started: osascript is the fake of conftest.py and the detached
helper is noted, not started (`spawned`).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest
from test_decisions import calls
from test_desktop_paths import fake_bundle, left_to, steps_of
from test_notifications import lead, until
from typer.testing import CliRunner

from orchestrator import autostart, cli, config, desktop, runner
from orchestrator.models import RunStatus
from orchestrator.service import Orchestrator

BODY = "a-1 finished"
QUESTION = "Decision needed: which?"
NESTED = "started by an agent of another home"
NOT_SHOWN = [("record", None), ("route", "not-shown"), ("drop", "not-shown")]


@pytest.fixture
def macos(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(desktop, "PLATFORM", "darwin")


@pytest.fixture
def spawned(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    noted: list[list[str]] = []
    monkeypatch.setattr(desktop, "spawn", lambda args: noted.append(args) or True)
    return noted


@pytest.fixture
def nested(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """This process descends from an agent's turn of another home (the live one, say)."""
    outer = tmp_path / "outer-home"
    monkeypatch.setenv(config.TURN_HOME_ENV, str(outer))
    return outer


def everything_present(tmp_path: Path) -> None:
    """A granted launcher, a window that may post and a page, all in this (nested) home."""
    desktop.record_grant("local.x.app", fake_bundle(tmp_path))
    desktop.app_present("allowed")
    desktop.page_present()


# -- the decision ----------------------------------------------------------------------------


def test_the_reason(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    home = config.home()
    assert desktop.quiet_reason({}) == ""
    assert desktop.quiet_reason({config.TURN_HOME_ENV: str(home)}) == ""
    assert desktop.quiet_reason({config.TURN_HOME_ENV: f"{home}/"}) == ""
    assert desktop.quiet_reason({config.TURN_HOME_ENV: str(home / "x" / "..")}) == ""
    monkeypatch.chdir(home.parent)
    assert desktop.quiet_reason({config.TURN_HOME_ENV: home.name}) == ""  # relative, same
    other = desktop.quiet_reason({config.TURN_HOME_ENV: str(tmp_path / "outer")})
    assert other.startswith(NESTED) and other.endswith(": not shown")
    for word in ("off", "OFF", "0", "false", "no"):
        why = desktop.quiet_reason({config.NOTIFICATIONS_ENV: word})
        assert why == f"ORCH_DESKTOP_NOTIFICATIONS={word}: not shown"
    for word in ("on", "1", ""):
        assert desktop.quiet_reason({config.NOTIFICATIONS_ENV: word}) == ""


@pytest.mark.parametrize("platform", ["darwin", "linux", "win32"])
def test_the_route_is_quiet_before_anything_else(platform: str):
    found = desktop.Presence(
        platform, desktop.Window(1, True, 0.5, "allowed"), 1.0, Path("/X.app"), "", "/x/send"
    )
    quiet = desktop.Presence(**{**vars(found), "quiet": "why"})
    assert desktop.route(found).taker != desktop.QUIET
    choice = desktop.route(quiet)
    assert choice.taker == desktop.QUIET and choice.at_once and choice.reason == "why"
    assert choice.path == "not-shown" and desktop.route(quiet, ("window",)) == choice
    assert quiet.describe() == "why"


def test_presence_says_why_and_looks_no_further(orch: Orchestrator, macos, nested, tmp_path):
    everything_present(tmp_path)
    found = desktop.presence()
    assert found.quiet.startswith(NESTED) and found.window is None and found.bundle is None


# -- nothing is posted -----------------------------------------------------------------------


@pytest.mark.parametrize("kind", ["finished", "decision"])
@pytest.mark.parametrize("platform", ["darwin", "linux"])
@pytest.mark.parametrize("present", [False, True])
def test_a_nested_instance_posts_nothing_by_any_route(
    orch: Orchestrator,
    fake_osascript,
    spawned,
    nested,
    tmp_path,
    monkeypatch,
    kind: str,
    platform: str,
    present: bool,
):
    monkeypatch.setattr(desktop, "PLATFORM", platform)
    monkeypatch.setattr(desktop, "NOTIFY_SEND", "sh")  # any binary: notify-send is there
    if present:
        everything_present(tmp_path)
    desktop.show(orch.store, *((QUESTION, "a-1", 3) if kind == "decision" else (BODY, "a-1")))
    desktop.show(orch.store, "no agent")  # unrecorded: nothing either
    time.sleep(0.3)
    assert calls(fake_osascript) == [] and spawned == []
    assert orch.notices() == []  # claimed at once: no window, page or launcher takes it
    (traced,) = orch.notice_trace(5)
    assert traced["route"] == desktop.QUIET and traced["claimed_by"]
    assert steps_of(orch, traced["id"]) == NOT_SHOWN
    assert traced["summary"]["path"] == "not-shown"
    assert traced["summary"]["reason"].startswith(NESTED)
    assert not orch.claim_notice(traced["id"], desktop.PAGE)
    log = desktop.notice_log().read_text()
    assert "drop not-shown: no agent, not recorded; started by an agent" in log
    out = CliRunner().invoke(cli.app, ["notices", "-v"]).output
    assert " not-shown " in out and f"{NESTED} ({nested}): not shown" in out


def test_a_notice_left_to_a_taker_is_escalated_to_nothing(
    orch: Orchestrator, fake_osascript, macos, nested, monkeypatch
):
    """The detached helper (desktop.deliver) of a nested home: the route it asks again is
    quiet, so neither the launcher nor osascript."""
    monkeypatch.setattr(desktop, "ESCALATE_SECONDS", 0.1)
    notice = left_to(orch, desktop.WINDOW)
    desktop.deliver(orch.store, notice)
    assert calls(fake_osascript) == [] and orch.notices() == []
    assert steps_of(orch, notice)[-2:] == [("escalate", "not-shown"), ("drop", "not-shown")]


def test_a_window_handing_over_in_a_nested_home_hands_to_nothing(
    orch: Orchestrator, fake_osascript, macos, nested
):
    notice = left_to(orch, desktop.WINDOW)
    orch.hand_over_notice(orch.store.notice(notice))
    assert calls(fake_osascript) == [] and orch.notices() == []
    assert steps_of(orch, notice)[-1] == ("drop", "not-shown")


def test_a_spooled_notice_of_a_nested_home_is_recorded_not_shown(
    orch: Orchestrator, fake_osascript, macos, nested, spawned, monkeypatch
):
    def busy(*args, **kwargs):
        raise RuntimeError("database is locked")

    with monkeypatch.context() as patch:
        patch.setattr(orch.store, "add_notice", busy)
        desktop.show(orch.store, BODY, "a-1")
    ((_, path, _),) = spawned
    desktop.spool_helper(Path(path), store=orch.store)
    assert calls(fake_osascript) == [] and orch.notices() == []
    (traced,) = orch.notice_trace(1)
    assert traced["summary"]["path"] == "not-shown"


def test_the_switch(orch: Orchestrator, fake_osascript, macos, spawned, tmp_path, monkeypatch):
    monkeypatch.setenv(config.NOTIFICATIONS_ENV, "off")
    everything_present(tmp_path)
    desktop.show(orch.store, BODY, "a-1")
    time.sleep(0.3)
    assert calls(fake_osascript) == [] and spawned == [] and orch.notices() == []
    (traced,) = orch.notice_trace(1)
    assert steps_of(orch, traced["id"]) == NOT_SHOWN
    assert traced["summary"]["reason"] == "ORCH_DESKTOP_NOTIFICATIONS=off: not shown"


# -- the live case is unchanged --------------------------------------------------------------


@pytest.mark.parametrize("marker", ["none", "same home", "same home, unresolved"])
def test_the_same_home_or_no_marker_posts_as_before(
    orch: Orchestrator, fake_osascript, macos, spawned, monkeypatch, marker: str
):
    if marker != "none":
        suffix = "/x/.." if "unresolved" in marker else ""
        monkeypatch.setenv(config.TURN_HOME_ENV, f"{config.home()}{suffix}")
    desktop.show(orch.store, BODY, "a-1")
    until(lambda: calls(fake_osascript), what="the notification")
    assert calls(fake_osascript)[0][-1] == f"{BODY}{desktop.CLAUSE_FINISHED}"
    (traced,) = orch.notice_trace(1)
    assert traced["route"] == desktop.OSASCRIPT


# -- the marker ------------------------------------------------------------------------------


def child_env(orch: Orchestrator, agent_id: str) -> dict[str, str]:
    agent = orch.store.get_agent(agent_id)
    found = Path(agent.cwd) / ".fake-env.json"
    until(found.exists, what="the child's environment")
    return json.loads(found.read_text())


def test_a_command_of_a_turn_gets_the_turn_and_its_home(orch: Orchestrator, project: str):
    agent, run = orch.spawn(project, "ENVCHILD", worktree=False)
    env = child_env(orch, agent.id)
    assert env["ORCH_TURN"].startswith(f"{run.id}-")
    assert env[config.TURN_HOME_ENV] == str(config.home().resolve())
    assert env["ORCH_HOME"] == str(config.home())


def test_the_outermost_home_is_kept_and_the_nested_turn_ends_quiet(
    orch: Orchestrator, fake_osascript, nested: Path
):
    """`ORCH_HOME=<temporary> spartito …` from an agent's shell: the runner (a process of its
    own, which drops ORCH_TURN) and the turn's commands keep the outer home; its finished
    notice is recorded, not shown."""
    config.user_roles_dir().mkdir(parents=True)
    role = "---\nlead: true\nworktree: false\nprovider: fake\n---\nYou orchestrate."
    (config.user_roles_dir() / "orchestrator.md").write_text(role)
    top = orch.orchestrator().id
    brief = orch.send(top, "ENVCHILD from the user")
    assert child_env(orch, top)[config.TURN_HOME_ENV] == str(nested)
    until(lambda: orch.store.get_run(brief.id).status is RunStatus.DONE, what="the turn")
    until(lambda: orch.notice_trace(1), what="the finished notice")
    (traced,) = orch.notice_trace(1)
    assert traced["kind"] == "finished" and traced["route"] == desktop.QUIET
    assert traced["summary"]["reason"].startswith(NESTED)  # its route step, then its drop
    until(lambda: steps_of(orch, traced["id"])[-1] == ("drop", "not-shown"), what="the drop")
    time.sleep(0.3)
    assert calls(fake_osascript) == []


def test_the_mcp_server_gets_the_turns_home(orch: Orchestrator, project: str, monkeypatch):
    agent, _ = orch.spawn(project, "x", worktree=False)
    env = runner.mcp_server(orch.store.get_agent(agent.id)).env
    assert env[config.TURN_HOME_ENV] == str(config.home().resolve())
    monkeypatch.setenv(config.TURN_HOME_ENV, "/elsewhere")
    assert runner.mcp_server(orch.store.get_agent(agent.id)).env[config.TURN_HOME_ENV] == (
        "/elsewhere"
    )


# As Codex starts an MCP server: a few variables of its own, and the env it was given.
CODEX_MCP_BASE = ("HOME", "PATH", "USER", "LOGNAME", "SHELL", "TMPDIR", "LANG")
RAISE = "from orchestrator import mcp_server; mcp_server.request_decision('Go ahead?')"


def raise_decision(env: dict[str, str], fake_osascript: Path) -> None:
    base = {k: os.environ[k] for k in CODEX_MCP_BASE if k in os.environ}
    base["FAKE_OSASCRIPT_LOG"] = str(fake_osascript)  # only for the fake to note a call
    subprocess.run([sys.executable, "-c", RAISE], env={**base, **env}, check=True, timeout=60)


def test_the_switch_reaches_an_mcp_server_given_only_its_own_env(
    orch: Orchestrator, project: str, fake_osascript: Path, monkeypatch
):
    agent = orch.store.get_agent(lead(orch, project))
    monkeypatch.setenv(config.NOTIFICATIONS_ENV, "off")
    env = runner.mcp_server(agent).env
    assert env[config.NOTIFICATIONS_ENV] == "off"
    raise_decision(env, fake_osascript)
    time.sleep(0.3)
    assert calls(fake_osascript) == [] and orch.notices() == []
    (traced,) = orch.notice_trace(1)
    assert traced["kind"] == "decision" and traced["route"] == desktop.QUIET
    assert steps_of(orch, traced["id"]) == NOT_SHOWN
    assert traced["summary"]["reason"] == "ORCH_DESKTOP_NOTIFICATIONS=off: not shown"
    monkeypatch.delenv(config.NOTIFICATIONS_ENV)
    assert config.NOTIFICATIONS_ENV not in runner.mcp_server(agent).env
    if sys.platform == "darwin":  # without it, the same process posts (to the fake osascript)
        raise_decision(runner.mcp_server(agent).env, fake_osascript)
        until(lambda: calls(fake_osascript), what="the notification")


def test_a_service_or_launcher_installed_from_an_agents_shell_has_neither(tmp_path: Path):
    environ = {
        "PATH": "/usr/bin",
        config.TURN_HOME_ENV: "/elsewhere",
        config.NOTIFICATIONS_ENV: "off",
        "ORCH_PROVIDERS": "fake=mod:Cls",
    }
    assert {config.TURN_HOME_ENV, config.NOTIFICATIONS_ENV} <= set(autostart.ENV_EXCLUDED)
    env = autostart.captured_env(tmp_path, environ)
    assert env == {"PATH": "/usr/bin", "ORCH_PROVIDERS": "fake=mod:Cls", "ORCH_HOME": str(tmp_path)}


def test_a_quiet_notice_before_its_drop_step_reads_as_its_route(orch: Orchestrator):
    item = desktop.step("route", "not-shown", f"{NESTED} (/elsewhere): not shown")
    notice = orch.store.add_notice(
        "finished", "a-1", BODY, route=desktop.QUIET, claimed_by="runner:1", steps=[item]
    )
    (traced,) = [n for n in orch.notice_trace(5) if n["id"] == notice]
    assert traced["summary"]["path"] == "not-shown"
    assert traced["summary"]["reason"].startswith(NESTED)
