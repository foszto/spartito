from __future__ import annotations

import os
import signal
import subprocess
import threading
import time
from pathlib import Path

import pytest

from orchestrator import git
from orchestrator.models import Run, Usage
from orchestrator.service import Orchestrator, OrchestratorError


def finished(orch: Orchestrator, *agent_ids: str) -> list[dict]:
    done, views = orch.wait(list(agent_ids), timeout=30)
    assert done, "agents did not finish in time"
    return views


def test_add_project_requires_a_git_repository(orch: Orchestrator, tmp_path: Path) -> None:
    with pytest.raises(OrchestratorError, match="not inside a git repository"):
        orch.add_project(str(tmp_path))


def test_worker_runs_in_its_own_worktree_and_branch(
    orch: Orchestrator, project: str, repo: Path
) -> None:
    agent, _ = orch.spawn(project, "TOUCH made.txt", name="Build It")

    (view,) = finished(orch, agent.id)

    assert view["status"] == "done"
    assert view["last_run"]["result"] == "echo[new]: TOUCH made.txt"
    assert agent.branch == f"orch/{agent.id}" and agent.id.startswith("build-it-")
    assert (Path(agent.cwd) / "made.txt").exists()
    assert not (repo / "made.txt").exists()
    assert git.current_branch(agent.cwd) == agent.branch


def test_agent_without_worktree_works_in_the_checkout(
    orch: Orchestrator, project: str, repo: Path
) -> None:
    agent, _ = orch.spawn(project, "TOUCH here.txt", worktree=False)

    finished(orch, agent.id)

    assert agent.branch is None
    assert (repo / "here.txt").exists()


def test_follow_up_resumes_the_session_and_messages_run_in_order(
    orch: Orchestrator, project: str
) -> None:
    agent, first = orch.spawn(project, "SLEEP 0.5 one")
    second = orch.send(agent.id, "two")

    finished(orch, agent.id)

    runs = orch.store.runs_of(agent.id)
    assert [run.result for run in runs] == ["echo[new]: SLEEP 0.5 one", "echo[resumed]: two"]
    assert runs[0].ended_at <= runs[1].started_at
    assert orch.store.get_agent(agent.id).session_id == "fake-session"
    assert (first.id, second.id) == (runs[0].id, runs[1].id)


def test_dependent_agent_waits_and_receives_the_result(orch: Orchestrator, project: str) -> None:
    planner, _ = orch.spawn(project, "SLEEP 0.5 the-plan", name="plan", worktree=False)
    worker, _ = orch.spawn(
        project, f"implement {{{{result:{planner.id}}}}}", name="impl", after=[planner.id]
    )
    assert orch.view(worker)["status"] == "waiting"

    plan_view, work_view = finished(orch, planner.id, worker.id)

    assert work_view["last_run"]["result"] == "echo[new]: implement echo[new]: SLEEP 0.5 the-plan"
    assert plan_view["last_run"]["ended_at"] <= work_view["last_run"]["started_at"]


def test_dependent_agent_is_skipped_when_the_dependency_fails(
    orch: Orchestrator, project: str
) -> None:
    first, _ = orch.spawn(project, "FAIL", worktree=False)
    second, _ = orch.spawn(project, "TOUCH never.txt", after=[first.id])

    first_view, second_view = finished(orch, first.id, second.id)

    assert first_view["status"] == "failed" and first_view["last_run"]["error"] == "boom"
    assert second_view["status"] == "skipped"
    assert not Path(second.cwd).exists()


def test_follow_on_agent_branches_from_an_earlier_agents_branch(
    orch: Orchestrator, project: str
) -> None:
    first, _ = orch.spawn(project, "one")
    second, _ = orch.spawn(project, "two", base=first.id, after=[first.id])

    finished(orch, second.id)

    assert second.base_ref == first.branch


def test_reviewer_can_share_the_implementers_worktree(orch: Orchestrator, project: str) -> None:
    worker, _ = orch.spawn(project, "TOUCH impl.txt")
    reviewer, _ = orch.spawn(
        project, "TOUCH review.txt", role="reviewer", share_with=worker.id, after=[worker.id]
    )

    finished(orch, reviewer.id)

    assert reviewer.cwd == worker.cwd and reviewer.branch is None
    assert (Path(worker.cwd) / "review.txt").exists()
    orch.archive(worker.id, confirm=True)
    with pytest.raises(OrchestratorError, match="shared with"):
        orch.remove(worker.id)


def test_stop_ends_running_and_queued_turns(orch: Orchestrator, project: str) -> None:
    agent, _ = orch.spawn(project, "SLEEP 60", worktree=False)
    orch.send(agent.id, "queued")
    deadline = time.monotonic() + 10
    while orch.view(agent)["status"] != "running" and time.monotonic() < deadline:
        time.sleep(0.1)

    assert orch.stop(agent.id) == 2

    assert [run.status.value for run in orch.store.runs_of(agent.id)] == ["stopped", "stopped"]
    assert orch.store.usage_of(agent.id) == {}


# Imported by every Python process of the test that has its directory on PYTHONPATH: holds
# the runner `python -m orchestrator.runner <HOLD_RUN>` in its start, before runner.py's
# `__main__` block, until the file HOLD_GO exists (at most a minute).
HELD_START = """\
import os, sys, time
if sys.argv[:1] == ["-m"] and sys.argv[-1] == os.environ.get("HOLD_RUN"):
    deadline = time.monotonic() + 60
    while not os.path.exists(os.environ["HOLD_GO"]) and time.monotonic() < deadline:
        time.sleep(0.01)
"""


def test_a_queued_turn_whose_runner_lost_the_stop_signal_does_not_start(
    orch: Orchestrator, project: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A runner launched by a process that ignores SIGTERM (a runner ending its turn)
    ignores it too until runner.py's `__main__` block: a stop's SIGTERM in that window is
    lost. The stop's mark must still keep the queued turn from starting."""
    if threading.current_thread() is not threading.main_thread():
        pytest.skip("signal handlers are set in the main thread only")
    agent, _ = orch.spawn(project, "SLEEP 60", worktree=False)
    deadline = time.monotonic() + 10
    while orch.view(agent)["status"] != "running" and time.monotonic() < deadline:
        time.sleep(0.1)
    site = tmp_path / "site"
    site.mkdir()
    (site / "sitecustomize.py").write_text(HELD_START)
    go = tmp_path / "go"
    monkeypatch.setenv("PYTHONPATH", f"{site}{os.pathsep}{os.environ['PYTHONPATH']}")
    monkeypatch.setenv("HOLD_GO", str(go))
    monkeypatch.setenv("HOLD_RUN", str(orch.store.last_run(agent.id).id + 1))
    ignored = signal.signal(signal.SIGTERM, signal.SIG_IGN)
    try:
        queued = orch.send(agent.id, "queued")
    finally:
        signal.signal(signal.SIGTERM, ignored)
    kill = orch._kill

    def lost(run: Run, sig: int) -> None:
        kill(run, sig)  # the held runner ignores it: the signal is gone
        if run.id == queued.id:
            go.touch()  # now it goes on: handlers, then waiting for its turn

    monkeypatch.setattr(orch, "_kill", lost)
    try:
        assert orch.stop(agent.id) == 2
    finally:
        go.touch()

    assert [run.status.value for run in orch.store.runs_of(agent.id)] == ["stopped", "stopped"]


def test_a_run_whose_runner_died_is_reported_as_failed(orch: Orchestrator, project: str) -> None:
    agent = orch.create_agent(project, worktree=False)
    run = orch.store.add_run(agent.id, "never started")
    orch.store.update_run(run.id, pid=2**22 + 12345)  # no such process

    (view,) = orch.overview(project)

    assert view["status"] == "failed" and view["last_run"]["error"] == "runner process died"


def test_remove_deletes_the_worktree_and_optionally_the_branch(
    orch: Orchestrator, project: str, repo: Path
) -> None:
    agent, _ = orch.spawn(project, "TOUCH x.txt")
    finished(orch, agent.id)
    orch.archive(agent.id, confirm=True)

    orch.remove(agent.id, delete_branch=True)

    assert not Path(agent.cwd).exists()
    assert not git.branch_exists(repo, agent.branch)
    assert orch.store.get_agent(agent.id) is None


def test_lead_is_created_once_per_project_and_sub_agents_form_a_hierarchy(
    orch: Orchestrator, project: str
) -> None:
    lead = orch.lead(project)
    child, _ = orch.spawn(project, "task", parent_id=lead.id)

    finished(orch, child.id)

    assert orch.lead(project).id == lead.id
    assert lead.cwd == orch.project(project).path and lead.role == "lead"
    assert orch.view(child)["parent_id"] == lead.id


@pytest.fixture
def empty_repo(tmp_path: Path) -> Path:
    path = tmp_path / "fresh"
    path.mkdir()
    for args in (
        ["init", "-q", "-b", "main"],
        ["config", "user.name", "t"],
        ["config", "user.email", "t@example.com"],
    ):
        subprocess.run(["git", "-C", str(path), *args], check=True)
    (path / "draft.txt").write_text("not committed")
    return path


def test_a_repository_without_commits_can_be_registered(
    orch: Orchestrator, empty_repo: Path
) -> None:
    project = orch.add_project(str(empty_repo), default_provider="fake")
    lead = orch.lead(project.name)
    orch.send(lead.id, "TOUCH from-lead.txt")

    finished(orch, lead.id)

    assert project.base_branch == "main"
    assert "no commits yet" in orch.project_note(project)
    assert (empty_repo / "from-lead.txt").exists()
    assert not git.has_commits(empty_repo)


def test_first_worktree_in_a_fresh_repository_gets_an_initial_commit(
    orch: Orchestrator, empty_repo: Path
) -> None:
    project = orch.add_project(str(empty_repo), default_provider="fake")
    first, _ = orch.spawn(project.name, "TOUCH a.txt")
    second, _ = orch.spawn(project.name, "TOUCH b.txt")

    views = finished(orch, first.id, second.id)

    assert [view["status"] for view in views] == ["done", "done"]
    assert git.git(empty_repo, "log", "--format=%s").splitlines() == ["Initial commit"]
    assert (empty_repo / "draft.txt").read_text() == "not committed"
    assert not (Path(first.cwd) / "draft.txt").exists()
    assert orch.project_note(project) is None
    notes = [e.text for a in (first, second) for e in orch.store.events(a.id) if e.kind == "info"]
    assert len(notes) == 1 and "empty initial commit" in notes[0]


def stored_usage(orch: Orchestrator, agent_id: str) -> list[tuple]:
    usage = orch.store.usage_of(agent_id)
    return [
        (u.input_tokens, u.output_tokens, u.cache_read_tokens, u.cache_creation_tokens, u.model)
        for u in usage.values()
    ]


@pytest.mark.parametrize("mode", ["", "CUMULATIVE "])
def test_each_run_of_a_resumed_agent_stores_its_own_usage(
    orch: Orchestrator, project: str, mode: str
) -> None:
    """Per-run figures whether the CLI reports per-run (Claude) or thread totals (Codex)."""
    agent, _ = orch.spawn(project, f"{mode}TOKENS 100", worktree=False)
    finished(orch, agent.id)
    orch.send(agent.id, f"{mode}TOKENS 40")

    finished(orch, agent.id)
    view = orch.view(agent, detail=True)

    assert stored_usage(orch, agent.id) == [
        (100, 10, 200, 50, "fake-model-1"),
        (40, 4, 80, 20, "fake-model-1"),
    ]
    assert [run.cost_usd for run in orch.store.runs_of(agent.id)] == [0.001, 0.0004]
    assert view["tokens"] == {"in": 140, "out": 14, "cache_read": 280, "cache_write": 70}
    assert view["cost_usd"] == 0.0014
    assert view["last_run"]["usage"] == {
        "tokens": {"in": 40, "out": 4, "cache_read": 80, "cache_write": 20},
        "cost_usd": 0.0004,
        "model": "fake-model-1",
    }


def test_a_failed_run_keeps_its_usage_and_a_run_without_a_result_has_none(
    orch: Orchestrator, project: str
) -> None:
    agent, failed = orch.spawn(project, "FAIL TOKENS 10", worktree=False)
    finished(orch, agent.id)
    plain = orch.send(agent.id, "no usage reported")

    finished(orch, agent.id)
    view = orch.view(agent, detail=True)

    assert view["status"] == "done"
    assert orch.store.get_usage(failed.id).input_tokens == 10
    assert orch.store.get_run(failed.id).status.value == "failed"
    assert orch.store.get_usage(plain.id) is None
    assert view["tokens"]["in"] == 10
    assert view["last_run"]["usage"] == {"tokens": None, "cost_usd": None, "model": None}


def test_codex_style_usage_without_cost_reports_no_cost(orch: Orchestrator, project: str) -> None:
    agent, run = orch.spawn(project, "no usage", worktree=False)
    finished(orch, agent.id)
    orch.store.set_usage(run.id, agent.id, Usage(5, 1, 0, 0))

    view = orch.view(agent, detail=True)

    assert view["last_run"]["usage"]["cost_usd"] is None
    assert view["tokens"]["in"] == 5 and view["cost_usd"] == 0
