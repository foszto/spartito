from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from orchestrator import config, notify
from orchestrator.core.views import QUEUE_LISTED
from orchestrator.models import RunStatus
from orchestrator.service import ORCHESTRATOR_ID, Orchestrator
from orchestrator.store import NOTIFICATION, Store
from orchestrator.web.app import app

ORIGIN = {"origin": "http://localhost"}


@pytest.fixture
def client(orch: Orchestrator) -> TestClient:
    return TestClient(app, base_url="http://localhost")


@pytest.fixture
def fake_orchestrator_role(orch: Orchestrator) -> None:
    """The packaged orchestrator role runs Claude; point it at the fake provider."""
    config.user_roles_dir().mkdir(parents=True)
    role = "---\nlead: true\nworktree: false\nprovider: fake\n---\nYou orchestrate."
    (config.user_roles_dir() / "orchestrator.md").write_text(role)


def wait_idle(orch: Orchestrator, agent_id: str) -> dict:
    done, (view,) = orch.wait([agent_id], timeout=30)
    assert done
    return view


def test_writes_from_another_origin_or_as_a_form_are_refused(
    client: TestClient, repo: Path
) -> None:
    body = {"path": str(repo)}

    assert client.post("/api/projects", json=body).status_code == 403
    assert (
        client.post(
            "/api/projects", json=body, headers={"origin": "http://evil.example"}
        ).status_code
        == 403
    )
    assert client.post("/api/projects", data=body, headers=ORIGIN).status_code == 415
    assert client.get("/api/state", headers={"host": "evil.example"}).status_code == 403
    assert client.get("/api/state").json()["projects"] == []


def test_the_page_and_the_state_carry_the_display_names(client: TestClient) -> None:
    page = client.get("/").text
    state = client.get("/api/state").json()

    assert f"<title>{config.PROJECT_NAME}</title>" in page
    assert f"<h1>{config.PROJECT_NAME}</h1>" in page and "{{" not in page
    assert f'<body data-orchestrator-name="{config.ORCHESTRATOR_NAME}">' in page
    assert (state["project_name"], state["orchestrator_name"]) == ("Spartito", "Maestro")


def test_project_and_agent_lifecycle_over_the_api(
    client: TestClient, orch: Orchestrator, repo: Path
) -> None:
    added = client.post(
        "/api/projects",
        json={"path": str(repo), "name": "demo", "provider": "fake"},
        headers=ORIGIN,
    )
    spawned = client.post(
        "/api/agents",
        json={"project": "demo", "task": "TOUCH a.txt", "name": "one", "model": ""},
        headers=ORIGIN,
    ).json()
    agent_id = spawned["agent_id"]
    wait_idle(orch, agent_id)
    client.post(f"/api/agents/{agent_id}/messages", json={"message": "again"}, headers=ORIGIN)
    wait_idle(orch, agent_id)

    state = client.get("/api/state").json()
    events = client.get(f"/api/agents/{agent_id}/events").json()

    assert added.json()["base_branch"] == "main"
    (agent,) = state["projects"][0]["agents"]
    assert (agent["id"], agent["status"], agent["runs"]) == (agent_id, "done", 2)
    assert [e["text"] for e in events["events"] if e["kind"] == "prompt"] == [
        "TOUCH a.txt",
        "again",
    ]
    assert events["agent"]["last_run"]["result"] == "echo[resumed]: again"

    refused = client.post("/api/projects/demo/remove", json={}, headers=ORIGIN)
    client.post(f"/api/agents/{agent_id}/archive", json={"confirm": True}, headers=ORIGIN)
    client.post(f"/api/agents/{agent_id}/remove", json={"delete_branch": True}, headers=ORIGIN)
    removed = client.post("/api/projects/demo/remove", json={}, headers=ORIGIN)

    assert refused.status_code == 400 and "still has agents" in refused.json()["error"]
    assert removed.status_code == 200 and client.get("/api/state").json()["projects"] == []


def test_invalid_requests_report_the_reason(client: TestClient, tmp_path: Path) -> None:
    not_a_repo = client.post("/api/projects", json={"path": str(tmp_path)}, headers=ORIGIN)
    no_task = client.post("/api/agents", json={"project": "demo"}, headers=ORIGIN)

    assert (
        not_a_repo.status_code == 400
        and "not inside a git repository" in not_a_repo.json()["error"]
    )
    assert no_task.status_code == 400 and no_task.json()["error"] == "'task' is required"


def test_orchestrator_agent_has_no_project_and_parents_the_project_lead(
    client: TestClient, orch: Orchestrator, project: str, fake_orchestrator_role: None
) -> None:
    sent = client.post("/api/orchestrator", json={"message": "hello"}, headers=ORIGIN).json()
    view = wait_idle(orch, ORCHESTRATOR_ID)
    lead = client.post(
        f"/api/projects/{project}/lead", json={"message": "plan"}, headers=ORIGIN
    ).json()
    wait_idle(orch, lead["agent_id"])

    state = client.get("/api/state").json()

    assert sent["agent_id"] == ORCHESTRATOR_ID
    assert (view["project"], view["last_run"]["result"]) == (None, "echo[new]: hello")
    assert state["orchestrator"]["cwd"] == str(config.workspace_dir())
    (lead_view,) = state["projects"][0]["agents"]
    assert (lead_view["role"], lead_view["parent_id"]) == ("lead", ORCHESTRATOR_ID)
    assert orch.lead(project).id == lead["agent_id"]


def test_a_database_from_before_the_orchestrator_agent_is_upgraded_in_place(tmp_path: Path) -> None:
    path = tmp_path / "old.db"
    old = sqlite3.connect(path)
    old.executescript(
        """
        CREATE TABLE projects (name TEXT PRIMARY KEY, path TEXT NOT NULL, base_branch TEXT NOT NULL,
            default_provider TEXT NOT NULL, created_at TEXT NOT NULL);
        CREATE TABLE agents (id TEXT PRIMARY KEY, project TEXT NOT NULL REFERENCES projects(name),
            parent_id TEXT, name TEXT NOT NULL, role TEXT NOT NULL, provider TEXT NOT NULL, model TEXT,
            cwd TEXT NOT NULL, branch TEXT, base_ref TEXT, session_id TEXT, permission TEXT,
            created_at TEXT NOT NULL);
        INSERT INTO projects VALUES ('p', '/repo', 'main', 'claude', 't');
        INSERT INTO agents (id, project, name, role, provider, cwd, created_at)
            VALUES ('a-1', 'p', 'a', 'worker', 'claude', '/w', 't');
        """
    )
    old.close()

    store = Store(path)
    run = store.add_run("a-1", "still linked")

    assert store.get_agent("a-1").project == "p"
    assert store.last_run("a-1").id == run.id
    with sqlite3.connect(path) as conn:
        notnull = {row[1]: row[3] for row in conn.execute("PRAGMA table_info(agents)")}
    assert notnull["project"] == 0 and notnull["cwd"] == 1


def test_folder_picker_lists_folders_and_marks_repositories(
    client: TestClient, orch: Orchestrator, project: str, repo: Path
) -> None:
    (repo.parent / "plain").mkdir()
    (repo.parent / ".hidden").mkdir()
    (repo.parent / "file.txt").write_text("x")

    listing = client.get("/api/browse", params={"path": str(repo.parent)}).json()
    inside = client.get("/api/browse", params={"path": str(repo)}).json()
    missing = client.get("/api/browse", params={"path": str(repo / "nope")})

    entries = {entry["name"]: entry for entry in listing["entries"]}
    assert set(entries) == {"home", "plain", "repo"}
    assert (entries["repo"]["git"], entries["repo"]["project"]) == (True, project)
    assert (entries["plain"]["git"], entries["plain"]["project"]) == (False, None)
    assert listing["parent"] == str(repo.parent.parent)
    assert inside["git"] and inside["project"] == project
    assert missing.status_code == 400 and "not a directory" in missing.json()["error"]


def test_agent_detail_lists_the_usage_of_recent_runs(
    client: TestClient, orch: Orchestrator, project: str
) -> None:
    spawned = client.post(
        "/api/agents",
        json={"project": project, "task": "TOKENS 100", "worktree": False},
        headers=ORIGIN,
    ).json()
    wait_idle(orch, spawned["agent_id"])

    detail = client.get(f"/api/agents/{spawned['agent_id']}/events").json()["agent"]
    (listed,) = client.get("/api/state").json()["projects"][0]["agents"]

    (run,) = detail["recent_runs"]
    assert (run["id"], run["status"], run["tokens"]["in"], run["model"]) == (
        spawned["run_id"],
        "done",
        100,
        "fake-model-1",
    )
    assert listed["tokens"]["out"] == 10 and listed["cost_usd"] == 0.001
    assert "model_override" in detail and "model_override" not in listed
    assert detail["effective"] == {"model": detail["model"], "effort": detail["effort"]}


def test_idle_parents_queue_and_notifications_reach_the_dashboard(
    client: TestClient, orch: Orchestrator, project: str
) -> None:
    lead = orch.create_agent(project, name="boss", role="lead", worktree=False)
    child = orch.create_agent(project, name="kid", worktree=False, parent_id=lead.id)
    orch.store.add_run(child.id, "work")  # queued, never started: the child counts as working
    told = orch.store.add_run(lead.id, notify.PENDING, kind=NOTIFICATION)
    orch.store.add_event(told.id, lead.id, "notification", "[orch notification] kid finished")
    orch.store.finish_run(told.id, RunStatus.DONE, result="noted")
    asked = orch.store.add_run(lead.id, "status please", sender=child.id)

    state = client.get("/api/state").json()
    detail = client.get(f"/api/agents/{lead.id}/events").json()

    listed = {agent["id"]: agent for agent in state["projects"][0]["agents"]}
    assert listed[lead.id]["children_running"] == 1 and listed[child.id]["children_running"] == 0
    assert (listed[lead.id]["status"], listed[lead.id]["queued_runs"]) == ("waiting", 1)
    agent = detail["agent"]
    assert agent["children_running"] == 1
    (queued,) = agent["queue"]
    assert (queued["id"], queued["kind"], queued["sender"]) == (asked.id, "message", child.id)
    assert queued["prompt"] == "status please" and queued["created_at"]
    (event,) = detail["events"]
    assert (event["kind"], event["text"]) == ("notification", "[orch notification] kid finished")
    child_detail = client.get(f"/api/agents/{child.id}/events").json()["agent"]
    assert child_detail["queue"][0]["kind"] == "message" and child_detail["children_running"] == 0


def test_the_listed_queue_is_capped_but_queued_runs_counts_all(
    client: TestClient, orch: Orchestrator, project: str
) -> None:
    agent = orch.create_agent(project, name="busy", worktree=False)
    for n in range(QUEUE_LISTED + 3):
        orch.store.add_run(agent.id, f"message {n}")

    detail = client.get(f"/api/agents/{agent.id}/events").json()["agent"]

    # The dashboard shows `+N more` for queued_runs beyond the listed queue.
    assert (len(detail["queue"]), detail["queued_runs"]) == (QUEUE_LISTED, QUEUE_LISTED + 3)
