from __future__ import annotations

import subprocess
import time
from pathlib import Path

import pytest

from orchestrator import git
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
