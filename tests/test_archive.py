"""Archiving agents: hidden from the default views, restored by a message or by hand,
deleted for good only from the archive, and archived automatically when idle and safe."""

from __future__ import annotations

import re
import sqlite3
import subprocess
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import click
import pytest
from starlette.testclient import TestClient
from typer.testing import CliRunner

from orchestrator import cli, config, git, mcp_server, service
from orchestrator.models import Agent, RunStatus
from orchestrator.service import NeedsConfirmation, Orchestrator, OrchestratorError
from orchestrator.store import SCHEMA, Store
from orchestrator.web.app import app

ORIGIN = {"origin": "http://localhost"}
GIT_USER = ["-c", "user.name=t", "-c", "user.email=t@example.com"]


def run_git(path: str | Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(path), *GIT_USER, *args], check=True, capture_output=True)


def ids(views: list[dict]) -> set[str]:
    return {view["id"] for view in views}


def age(orch: Orchestrator, agent_id: str, hours: float) -> None:
    """Make every timestamp of the agent and its runs `hours` old."""
    then = (datetime.now(UTC) - timedelta(hours=hours)).isoformat(timespec="seconds")
    with orch.store.connection() as conn:
        conn.execute("UPDATE agents SET created_at = ? WHERE id = ?", (then, agent_id))
        conn.execute(
            "UPDATE runs SET created_at = ?, started_at = ?, ended_at = ? WHERE agent_id = ?",
            (then, then, then, agent_id),
        )


def finished_turn(orch: Orchestrator, agent_id: str, hours: float = 48) -> None:
    """A done turn `hours` ago, without running anything."""
    run = orch.store.add_run(agent_id, "work")
    orch.store.finish_run(run.id, RunStatus.DONE, result="done")
    age(orch, agent_id, hours)


def with_worktree(orch: Orchestrator, project: str, repo: Path, **options) -> Agent:
    """A worker that owns a worktree on its own branch, created here (no runner)."""
    agent = orch.create_agent(project, **options)
    git.add_worktree(repo, agent.cwd, agent.branch, "main")
    return agent


def commit(path: str | Path, name: str = "file.txt") -> None:
    (Path(path) / name).write_text("x")
    run_git(path, "add", name)
    run_git(path, "commit", "-q", "-m", f"add {name}")


@pytest.fixture
def client(orch: Orchestrator) -> TestClient:
    return TestClient(app, base_url="http://localhost")


# -- archive, restore and the views ------------------------------------------


def test_an_archived_agent_is_hidden_from_the_default_views_and_listed_in_the_archive(
    orch: Orchestrator, project: str, client: TestClient
) -> None:
    kept = orch.create_agent(project, name="kept", worktree=False)
    gone = orch.create_agent(project, name="gone", worktree=False)
    finished_turn(orch, gone.id, hours=0)

    assert orch.archive(gone.id) == {"archived": gone.id, "unsaved": None}

    assert ids(orch.overview()) == {kept.id}
    assert ids(mcp_server.list_agents()) == {kept.id}
    archived = mcp_server.list_agents(archived=True)
    assert ids(archived) == {gone.id}
    assert archived[0]["archived_how"] == "manual" and archived[0]["archived_at"]
    state = client.get("/api/state").json()
    assert ids(state["projects"][0]["agents"]) == {kept.id}
    assert state["archived"] == [gone.id]
    assert ids(client.get("/api/archive").json()["agents"]) == {gone.id}
    listing = click.unstyle(CliRunner().invoke(cli.app, ["ps"]).output)
    assert kept.id in listing and gone.id not in listing
    archive = click.unstyle(CliRunner().invoke(cli.app, ["ps", "--archived"]).output)
    assert gone.id in archive and kept.id not in archive and "manual" in archive
    # Everything is kept and still readable.
    assert orch.store.get_agent(gone.id) and len(orch.store.runs_of(gone.id)) == 1
    detail = client.get(f"/api/agents/{gone.id}/events").json()["agent"]
    assert detail["archived"]["how"] == "manual"
    status = click.unstyle(CliRunner().invoke(cli.app, ["status", gone.id]).output)
    assert "archived" in status and f"orch restore {gone.id}" in status

    with pytest.raises(OrchestratorError, match="archived already"):
        orch.archive(gone.id)
    assert orch.restore(gone.id) == {"restored": gone.id}
    assert ids(orch.overview()) == {kept.id, gone.id}
    assert orch.overview(archived=True) == []
    with pytest.raises(OrchestratorError, match="not archived"):
        orch.restore(gone.id)


def test_restore_from_the_cli_the_dashboard_and_mcp(
    orch: Orchestrator, project: str, client: TestClient
) -> None:
    agents = [orch.create_agent(project, name=f"a{n}", worktree=False) for n in range(3)]
    for agent in agents:
        orch.archive(agent.id)

    by_cli = CliRunner().invoke(cli.app, ["restore", agents[0].id])
    by_web = client.post(f"/api/agents/{agents[1].id}/restore", json={}, headers=ORIGIN)
    by_mcp = mcp_server.archive_agent(agents[2].id, restore=True)

    assert by_cli.exit_code == 0 and by_web.json() == {"restored": agents[1].id}
    assert by_mcp == {"restored": agents[2].id}
    assert orch.store.archived() == {}


def test_a_message_restores_an_archived_agent_on_every_path(
    orch: Orchestrator, project: str, client: TestClient
) -> None:
    agents = [orch.create_agent(project, name=f"a{n}", worktree=False) for n in range(3)]
    lead = orch.lead(project)
    for agent in (*agents, lead):
        orch.archive(agent.id)
    assert len(orch.store.archived()) == 4

    CliRunner().invoke(cli.app, ["send", agents[0].id, "hello"])
    client.post(f"/api/agents/{agents[1].id}/messages", json={"message": "hi"}, headers=ORIGIN)
    mcp_server.send_message(agents[2].id, "hey", freeform=True)
    assert mcp_server.ask_project_lead(project, "and you")["id"] == lead.id

    assert orch.store.archived() == {}
    assert orch.wait_idle([agent.id for agent in (*agents, lead)], timeout=30)


def test_a_turn_that_starts_restores_an_archived_agent(orch: Orchestrator, project: str) -> None:
    agent = orch.create_agent(project, worktree=False)
    orch.archive(agent.id)
    run_id = 0
    with orch.store.transaction() as conn:  # queued as a notification is (no restore yet)
        run_id = orch.store.insert_run(conn, agent.id, "pending", kind="notification")
    assert agent.id in orch.store.archived()

    with orch.store.transaction() as conn:
        orch.store.start_run(conn, run_id)

    assert orch.store.archived() == {}


def test_an_agent_that_works_cannot_be_archived(orch: Orchestrator, project: str) -> None:
    parent = orch.create_agent(project, name="parent", worktree=False)
    child = orch.create_agent(project, name="child", worktree=False, parent_id=parent.id)
    queued = orch.store.add_run(child.id, "queued, never launched")

    with pytest.raises(OrchestratorError, match="queued or running"):
        orch.archive(child.id)
    with pytest.raises(OrchestratorError, match="queued or running"):
        orch.archive(parent.id, confirm=True)
    orch.store.finish_run(queued.id, RunStatus.STOPPED)
    orch.archive(parent.id)
    with pytest.raises(OrchestratorError, match="orchestrator is not archived"):
        orch.archive(orch.orchestrator().id)


# -- deleting -------------------------------------------------------------------


def test_only_an_archived_agent_can_be_deleted(
    orch: Orchestrator, project: str, repo: Path, client: TestClient
) -> None:
    by_service = with_worktree(orch, project, repo, name="one")
    by_cli = orch.create_agent(project, name="two", worktree=False)
    by_web = orch.create_agent(project, name="three", worktree=False)

    with pytest.raises(OrchestratorError, match="not archived"):
        orch.remove(by_service.id)
    refused_cli = CliRunner().invoke(cli.app, ["rm", by_cli.id])
    refused_web = client.post(f"/api/agents/{by_web.id}/remove", json={}, headers=ORIGIN)
    assert refused_cli.exit_code == 1 and "not archived" in refused_cli.output
    assert refused_web.status_code == 400 and "not archived" in refused_web.json()["error"]
    assert len(orch.store.list_agents(project)) == 3

    for agent in (by_service, by_cli, by_web):
        orch.archive(agent.id)
    orch.remove(by_service.id, delete_branch=True)
    assert CliRunner().invoke(cli.app, ["rm", by_cli.id]).exit_code == 0
    removed = client.post(f"/api/agents/{by_web.id}/remove", json={}, headers=ORIGIN)

    assert removed.json() == {"removed": by_web.id}
    assert orch.store.list_agents(project) == [] and orch.store.archived() == {}
    assert not Path(by_service.cwd).exists() and not git.branch_exists(repo, by_service.branch)


def test_mcp_has_archive_agent_instead_of_remove_agent() -> None:
    assert not hasattr(mcp_server, "remove_agent")
    doc = mcp_server.archive_agent.__doc__ or ""
    assert len(doc.strip().splitlines()) <= 3


# -- manual archiving: confirmation and "all safe" ----------------------------------


def test_unmerged_work_is_archived_by_hand_only_after_a_confirmation(
    orch: Orchestrator, project: str, repo: Path, client: TestClient
) -> None:
    agents = [with_worktree(orch, project, repo, name=f"w{n}") for n in range(5)]
    for agent in agents:
        commit(agent.cwd)
        (Path(agent.cwd) / "draft.txt").write_text("not committed")

    with pytest.raises(NeedsConfirmation) as refused:
        orch.archive(agents[0].id)
    assert f"1 commit on {agents[0].branch} not merged into main" in str(refused.value)
    assert "1 uncommitted change" in str(refused.value)
    assert orch.archive(agents[0].id, confirm=True)["unsaved"]

    declined = CliRunner().invoke(cli.app, ["archive", agents[1].id], input="n\n")
    accepted = CliRunner().invoke(cli.app, ["archive", agents[2].id], input="y\n")
    unasked = CliRunner().invoke(cli.app, ["archive", agents[3].id, "--yes"])
    assert declined.exit_code == 1 and "not merged" in declined.output
    assert (
        accepted.exit_code == 0
        and unasked.exit_code == 0
        and "Archive anyway" not in unasked.output
    )

    asked = client.post(f"/api/agents/{agents[4].id}/archive", json={}, headers=ORIGIN).json()
    assert "not merged into main" in asked["confirm"]
    assert agents[4].id not in orch.store.archived()
    confirmed = client.post(
        f"/api/agents/{agents[4].id}/archive", json={"confirm": True}, headers=ORIGIN
    )
    assert confirmed.json()["archived"] == agents[4].id

    assert set(orch.store.archived()) == {agents[n].id for n in (0, 2, 3, 4)}
    assert mcp_server.archive_agent(agents[1].id, confirm=True)["archived"] == agents[1].id


def test_mcp_archive_agent_refuses_unmerged_work_without_confirm(
    orch: Orchestrator, project: str, repo: Path
) -> None:
    agent = with_worktree(orch, project, repo)
    commit(agent.cwd)

    with pytest.raises(NeedsConfirmation, match="not merged"):
        mcp_server.archive_agent(agent.id)
    assert orch.store.archived() == {}


def test_archive_all_safe_in_a_project(
    orch: Orchestrator, project: str, repo: Path, client: TestClient
) -> None:
    planner = orch.create_agent(project, name="plan", role="planner")
    clean = with_worktree(orch, project, repo, name="clean")
    unmerged = with_worktree(orch, project, repo, name="unmerged")
    commit(unmerged.cwd)
    busy = orch.create_agent(project, name="busy", worktree=False)
    orch.store.add_run(busy.id, "queued")
    lead = orch.lead(project)

    result = orch.archive_safe(project)

    assert set(result["archived"]) == {planner.id, clean.id}
    assert list(result["kept"]) == [unmerged.id] and "not merged" in result["kept"][unmerged.id]
    assert lead.id not in orch.store.archived()

    orch.restore(planner.id)
    by_cli = CliRunner().invoke(cli.app, ["archive", "--safe", "-p", project])
    assert by_cli.exit_code == 0 and f"archived {planner.id}" in by_cli.output
    assert f"kept {unmerged.id}" in by_cli.output
    orch.restore(clean.id)
    by_web = client.post(f"/api/projects/{project}/archive", json={}, headers=ORIGIN).json()
    assert by_web["archived"] == [clean.id]
    assert CliRunner().invoke(cli.app, ["archive"]).exit_code == 1  # neither id nor --safe


# -- automatic archiving ----------------------------------------------------------


def test_automatic_archiving_case_by_case(orch: Orchestrator, project: str, repo: Path) -> None:
    no_worktree = orch.create_agent(project, name="plain", worktree=False)
    finished_turn(orch, no_worktree.id)
    shares = with_worktree(orch, project, repo, name="owner")
    commit(shares.cwd)
    finished_turn(orch, shares.id)
    sharer = orch.create_agent(project, name="sharer", role="reviewer", share_with=shares.id)
    finished_turn(orch, sharer.id)
    merged = with_worktree(orch, project, repo, name="merged")
    commit(merged.cwd, "merged.txt")
    run_git(repo, "merge", "-q", "--ff-only", merged.branch)
    finished_turn(orch, merged.id)
    gone = with_worktree(orch, project, repo, name="gone")  # merged, worktree removed
    git.remove_worktree(repo, gone.cwd)
    finished_turn(orch, gone.id)
    dirty = with_worktree(orch, project, repo, name="dirty")
    (Path(dirty.cwd) / "draft.txt").write_text("x")
    finished_turn(orch, dirty.id)
    parent = orch.create_agent(project, name="parent", worktree=False)
    finished_turn(orch, parent.id)
    child = orch.create_agent(project, name="child", worktree=False, parent_id=parent.id)
    running = orch.store.add_run(child.id, "running")
    orch.store.update_run(running.id, status=RunStatus.RUNNING)  # no pid: never reconciled
    queued = orch.create_agent(project, name="queued", worktree=False)
    finished_turn(orch, queued.id)
    orch.store.add_run(queued.id, "queued")
    age(orch, queued.id, 48)
    lead = orch.lead(project)
    finished_turn(orch, lead.id)
    top = orch.orchestrator()
    finished_turn(orch, top.id)
    young = orch.create_agent(project, name="young", worktree=False)
    finished_turn(orch, young.id, hours=23)
    never_ran = orch.create_agent(project, name="never", worktree=False)
    age(orch, never_ran.id, 30)

    archived = orch.auto_archive()

    assert set(archived) == {no_worktree.id, sharer.id, merged.id, gone.id, never_ran.id}
    assert all(mark["how"] == "auto" for mark in orch.store.archived().values())
    # shares: unmerged commits; dirty: uncommitted changes; parent: a child runs; queued:
    # a turn waits; lead, orchestrator: never; young: within 24 hours.
    assert shares.id not in archived and dirty.id not in archived
    assert orch.unsaved(shares) and orch.unsaved(dirty)
    assert parent.id not in archived and child.id not in archived and queued.id not in archived
    assert lead.id not in archived and top.id not in archived and young.id not in archived
    # A restored agent is not archived again at once: restoring counts as activity.
    orch.restore(no_worktree.id)
    assert orch.auto_archive() == []


def test_a_git_error_means_not_safe(orch: Orchestrator, project: str, repo: Path) -> None:
    agent = with_worktree(orch, project, repo)
    finished_turn(orch, agent.id)
    with orch.store.connection() as conn:
        conn.execute("UPDATE projects SET base_branch = 'no-such-branch'")

    assert orch.auto_archive() == []
    assert "could not be checked" in (orch.unsaved(orch.agent(agent.id)) or "")


def test_the_archive_time_is_set_globally_and_per_project(
    orch: Orchestrator, project: str, repo: Path
) -> None:
    agent = orch.create_agent(project, worktree=False)
    finished_turn(orch, agent.id, hours=3)

    assert orch.auto_archive() == []  # default: 24 hours
    orch.update_settings({"archive_after_hours": 2})
    orch.update_settings({"archive_after_hours": "off"}, project)
    assert orch.auto_archive() == []  # the project switched it off
    assert orch.settings(project) == {"archive_after_hours": 0}
    result = CliRunner().invoke(cli.app, ["config", "archive-after", "2.5", "-p", project])
    assert result.exit_code == 0 and "2.5 idle hours" in result.output
    assert orch.auto_archive() == [agent.id]

    orch.restore(agent.id)
    assert orch.auto_archive() == []  # restoring counts as activity
    long_ago = (datetime.now(UTC) - timedelta(hours=100)).isoformat(timespec="seconds")
    with orch.store.connection() as conn:
        conn.execute("UPDATE archived_agents SET restored_at = ?", (long_ago,))
    CliRunner().invoke(cli.app, ["config", "archive-after", "", "-p", project])
    CliRunner().invoke(cli.app, ["config", "archive-after", "0"])
    assert orch.settings(project) == {} and orch.settings() == {"archive_after_hours": 0}
    assert orch.auto_archive() == []  # switched off globally
    orch.update_settings({"archive_after_hours": 1})  # the project inherits it
    assert orch.auto_archive() == [agent.id]


def test_settings_keep_the_archive_time_next_to_the_tiers(
    orch: Orchestrator, project: str, client: TestClient
) -> None:
    saved = client.post(
        "/api/settings",
        json={"data": {"worker": {"fake": {"model": "m"}}, "archive_after_hours": "12"}},
        headers=ORIGIN,
    )
    bad = client.post(
        "/api/settings", json={"data": {"archive_after_hours": "soon"}}, headers=ORIGIN
    )
    negative = client.post(
        "/api/settings", json={"data": {"archive_after_hours": -1}}, headers=ORIGIN
    )

    assert saved.json() == {"worker": {"fake": {"model": "m"}}, "archive_after_hours": 12}
    assert bad.status_code == 400 and "number of hours" in bad.json()["error"]
    assert negative.status_code == 400
    shown = click.unstyle(CliRunner().invoke(cli.app, ["config", "show"]).output)
    assert "archive after 12 idle hours" in shown
    CliRunner().invoke(cli.app, ["config", "set", "lead", "--provider", "fake", "--model", "x"])
    assert orch.settings()["archive_after_hours"] == 12  # kept by other changes


def test_the_automatic_check_runs_from_the_listings_at_most_every_few_minutes(
    orch: Orchestrator, project: str, client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    agent = orch.create_agent(project, worktree=False)
    finished_turn(orch, agent.id)
    calls = []
    real = Orchestrator.auto_archive

    def counted(self: Orchestrator) -> list[str]:
        calls.append(1)
        return real(self)

    monkeypatch.setattr(Orchestrator, "auto_archive", counted)

    first = client.get("/api/state").json()
    for _ in range(3):
        client.get("/api/state")
        orch.overview()
        mcp_server.list_agents()
    assert len(calls) == 1
    assert first["projects"][0]["agents"] == [] and first["archived"] == [agent.id]

    orch.overview(archived=True)  # the archive listing never runs it
    assert len(calls) == 1
    stamp = config.home() / "auto-archive.stamp"
    stamp.write_text(f"{time.time() - service.AUTO_ARCHIVE_EVERY - 1:.0f}\n")
    CliRunner().invoke(cli.app, ["ps"])
    assert len(calls) == 2
    stamp.write_text("garbage")  # an unreadable stamp does not switch the check off
    orch.overview()
    assert len(calls) == 3 and float(stamp.read_text())


def test_a_failing_automatic_check_never_breaks_a_listing(
    orch: Orchestrator, project: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    orch.create_agent(project, worktree=False)

    def broken(self: Orchestrator) -> list[str]:
        raise RuntimeError("boom")

    monkeypatch.setattr(Orchestrator, "auto_archive", broken)

    assert len(orch.overview()) == 1


def test_the_automatic_check_stops_after_its_time_budget(
    orch: Orchestrator, project: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    for n in range(3):
        finished_turn(orch, orch.create_agent(project, name=f"a{n}", worktree=False).id)
    monkeypatch.setattr(service, "AUTO_ARCHIVE_BUDGET", -1.0)

    assert orch.auto_archive() == []


# -- schema ------------------------------------------------------------------------


def test_a_database_without_the_archive_table_opens(tmp_path: Path) -> None:
    path = tmp_path / "before.db"
    old = re.sub(r"-- Archived agents.*?\);\n", "", SCHEMA, flags=re.S)
    with sqlite3.connect(path) as conn:
        conn.executescript(old)
        conn.execute(
            "INSERT INTO agents (id, name, role, provider, cwd, created_at)"
            " VALUES ('a-1', 'a', 'worker', 'claude', '/w', 't')"
        )
    conn.close()
    tables = {row[0] for row in sqlite3.connect(path).execute("SELECT name FROM sqlite_master")}
    assert "archived_agents" not in tables and "agents" in tables

    store = Store(path)

    assert store.archived() == {}
    assert store.archive("a-1") and set(store.archived()) == {"a-1"}
