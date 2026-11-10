"""Compact handoff: result files, capped {{result}} references and per-role output schemas."""

from __future__ import annotations

import json
from importlib import resources
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from orchestrator import cli, config, mcp_server, roles
from orchestrator.models import RunStatus
from orchestrator.runner import RESULT_CHARS, render_prompt
from orchestrator.service import Orchestrator, OrchestratorError

SCHEMA_ROLES = ("worker", "reviewer", "tester", "planner")
OWN_SCHEMA = {
    "type": "object",
    "properties": {"ok": {"type": "boolean"}},
    "required": ["ok"],
    "additionalProperties": False,
}


def idle(orch: Orchestrator, *agent_ids: str) -> list[dict]:
    done, views = orch.wait(list(agent_ids), timeout=30)
    assert done, "agents did not finish in time"
    return views


def finished_agent(orch: Orchestrator, project: str, result: str, structured: Any = None) -> str:
    agent = orch.create_agent(project, name="src", worktree=False)
    run = orch.store.add_run(agent.id, "task")
    orch.store.finish_run(run.id, RunStatus.DONE, result=result, structured=structured)
    return agent.id


# -- result file ------------------------------------------------------------


def test_a_done_run_writes_its_full_answer_to_the_result_file(
    orch: Orchestrator, project: str
) -> None:
    agent, run = orch.spawn(project, "hello", worktree=False)
    idle(orch, agent.id)

    path = config.logs_dir() / f"run-{run.id}" / "result.md"
    assert path.read_text() == "echo[new]: hello"
    assert not (path.parent / "result.json").exists()
    assert orch.view(agent, detail=True)["last_run"]["result_file"] == str(path)


def test_a_failed_run_writes_the_result_file_too(orch: Orchestrator, project: str) -> None:
    agent, run = orch.spawn(project, "FAIL", worktree=False)
    (view,) = idle(orch, agent.id)

    assert view["last_run"]["status"] == "failed"
    assert config.result_file(run.id).read_text() == "working"


def test_result_file_is_in_the_detail_view_only(orch: Orchestrator, project: str) -> None:
    agent, run = orch.spawn(project, "hello", worktree=False)
    idle(orch, agent.id)

    (listed,) = mcp_server.list_agents()
    detail = mcp_server.get_agent(agent.id)
    by_run = mcp_server.get_agent(agent.id, run_id=run.id)

    assert "result_file" not in listed["last_run"]
    assert "result_file" not in orch.view(agent)["last_run"]
    assert detail["last_run"]["result_file"] == str(config.result_file(run.id))
    assert by_run["run"]["result_file"] == str(config.result_file(run.id))


def test_a_run_without_a_result_file_reports_none(orch: Orchestrator, project: str) -> None:
    agent_id = finished_agent(orch, project, "old answer")

    assert mcp_server.get_agent(agent_id)["last_run"]["result_file"] is None


def test_structured_output_is_written_beside_the_result_file(tmp_path: Path) -> None:
    from orchestrator.runner import write_result

    write_result(tmp_path, '{"ok":true}', {"ok": True})

    assert json.loads((tmp_path / "result.json").read_text()) == {"ok": True}
    assert (tmp_path / "result.md").read_text() == '{"ok":true}'


# -- {{result:<agent-id>}} --------------------------------------------------


def test_a_long_text_result_is_cut_with_its_length_and_file(
    orch: Orchestrator, project: str
) -> None:
    text = "x" * (RESULT_CHARS + 500)
    agent_id = finished_agent(orch, project, text)
    run = orch.store.last_run(agent_id)
    config.run_dir(run.id).mkdir(parents=True)
    config.result_file(run.id).write_text(text)

    rendered = render_prompt(orch.store, f"see {{{{result:{agent_id}}}}} end")
    body, note = rendered.removeprefix("see ").removesuffix(" end").split("\n")

    assert body == "x" * RESULT_CHARS
    assert str(RESULT_CHARS + 500) in note and str(config.result_file(run.id)) in note


def test_full_reference_injects_the_whole_text(orch: Orchestrator, project: str) -> None:
    text = "y" * (RESULT_CHARS + 500)
    agent_id = finished_agent(orch, project, text)

    assert render_prompt(orch.store, f"{{{{result:{agent_id}:full}}}}") == text


def test_a_short_text_result_is_injected_as_is(orch: Orchestrator, project: str) -> None:
    agent_id = finished_agent(orch, project, "short")

    assert render_prompt(orch.store, f"[{{{{result:{agent_id}}}}}]") == "[short]"


def test_structured_output_is_injected_as_compact_json(orch: Orchestrator, project: str) -> None:
    data = {"status": "done", "open": ["a", "b"]}
    agent_id = finished_agent(orch, project, "prose", structured=data)

    rendered = render_prompt(orch.store, f"{{{{result:{agent_id}}}}}")

    assert rendered == '{"status":"done","open":["a","b"]}'


def test_unknown_or_unfinished_agents_are_left_untouched(orch: Orchestrator, project: str) -> None:
    pending = orch.create_agent(project, name="pending", worktree=False)
    orch.store.add_run(pending.id, "task")
    prompt = f"{{{{result:nobody-1234}}}} {{{{result:{pending.id}:full}}}}"

    assert render_prompt(orch.store, prompt) == prompt


# -- role output schemas ----------------------------------------------------


def strict_problems(schema: Any, where: str = "$") -> list[str]:
    """Where `schema` breaks OpenAI strict structured output (Codex `--output-schema`)."""
    problems = []
    if not isinstance(schema, dict):
        return [f"{where}: not an object"]
    types = schema.get("type")
    types = types if isinstance(types, list) else [types]
    if "object" in types:
        props = schema.get("properties", {})
        if set(schema.get("required", [])) != set(props) or not props:
            problems.append(f"{where}: required must list every property")
        if schema.get("additionalProperties") is not False:
            problems.append(f"{where}: additionalProperties must be false")
        for name, sub in props.items():
            problems += strict_problems(sub, f"{where}.{name}")
    if "array" in types:
        problems += strict_problems(schema.get("items"), f"{where}[]")
    return problems


@pytest.mark.parametrize("name", SCHEMA_ROLES)
def test_packaged_schemas_are_strict_and_valid(name: str) -> None:
    jsonschema = pytest.importorskip("jsonschema")
    role = roles.load(name)
    schema = roles.output_schema(role)

    assert role.output_schema == f"{name}.schema.json"
    jsonschema.Draft202012Validator.check_schema(schema)
    assert schema["type"] == "object"
    assert strict_problems(schema) == []


def test_strict_checker_finds_a_loose_nested_object() -> None:
    loose = {
        "type": "object",
        "properties": {"items": {"type": "array", "items": {"type": "object", "properties": {}}}},
        "required": ["items"],
        "additionalProperties": False,
    }
    assert strict_problems(loose) == [
        "$.items[]: required must list every property",
        "$.items[]: additionalProperties must be false",
    ]


def test_packaged_schema_files_ship_with_the_package() -> None:
    root = resources.files("orchestrator") / "roles"
    assert {f"{name}.schema.json" for name in SCHEMA_ROLES} <= {e.name for e in root.iterdir()}


@pytest.mark.parametrize("name", ("lead", "orchestrator"))
def test_lead_roles_answer_in_free_text(name: str) -> None:
    role = roles.load(name)
    assert role.output_schema is None and roles.output_schema(role) is None


def write_role(directory: Path, name: str, schema_line: str, schema: Any = None) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{name}.md").write_text(f"---\nprovider: fake\n{schema_line}\n---\nDo it.")
    if schema is not None:
        (directory / "own.schema.json").write_text(json.dumps(schema))


def test_a_role_schema_is_read_from_the_file_beside_it(tmp_path: Path) -> None:
    write_role(config.user_roles_dir(), "custom", "output_schema: own.schema.json", OWN_SCHEMA)

    assert roles.output_schema(roles.load("custom")) == OWN_SCHEMA


def test_a_project_schema_file_overrides_the_packaged_one(repo: Path) -> None:
    override = {**OWN_SCHEMA, "title": "project"}
    project_roles = repo / ".orch" / "roles"
    project_roles.mkdir(parents=True)
    (project_roles / "worker.schema.json").write_text(json.dumps(override))

    assert roles.output_schema(roles.load("worker", repo), repo) == override
    assert roles.output_schema(roles.load("worker")) != override


@pytest.mark.parametrize("line", ("output_schema: none", "output_schema: None", ""))
def test_none_or_absent_means_free_text(line: str) -> None:
    write_role(config.user_roles_dir(), "free", line)

    role = roles.load("free")
    assert role.output_schema is None and roles.output_schema(role) is None


def test_a_missing_schema_file_is_a_clear_error() -> None:
    write_role(config.user_roles_dir(), "broken", "output_schema: gone.json")

    with pytest.raises(roles.SchemaError, match=r"role 'broken'.*`gone.json`.*not found"):
        roles.output_schema(roles.load("broken"))


def test_an_invalid_schema_file_is_a_clear_error() -> None:
    write_role(config.user_roles_dir(), "bad", "output_schema: own.schema.json", [1, 2])

    with pytest.raises(roles.SchemaError, match=r"must hold a JSON object"):
        roles.output_schema(roles.load("bad"))


# -- applied only to agent-to-agent handoff ---------------------------------


def run_schema(orch: Orchestrator, run_id: int) -> Any:
    return orch.store.get_run(run_id).output_schema


def worker_schema() -> dict:
    return roles.output_schema(roles.load("worker"))


def test_mcp_spawn_applies_the_role_schema(orch: Orchestrator, project: str) -> None:
    worker = mcp_server.spawn_agent("hello", project=project, worktree=False)
    reviewer = mcp_server.spawn_agent("hello", project=project, role="reviewer")
    idle(orch, worker["id"], reviewer["id"])

    assert run_schema(orch, worker["run_id"]) == worker_schema()
    assert run_schema(orch, reviewer["run_id"]) == roles.output_schema(roles.load("reviewer"))


def test_mcp_spawn_freeform_skips_and_an_own_schema_wins(orch: Orchestrator, project: str) -> None:
    free = mcp_server.spawn_agent("hello", project=project, worktree=False, freeform=True)
    own = mcp_server.spawn_agent(
        "hello", project=project, worktree=False, output_schema=OWN_SCHEMA, freeform=True
    )
    idle(orch, free["id"], own["id"])

    assert run_schema(orch, free["run_id"]) is None
    assert run_schema(orch, own["run_id"]) == OWN_SCHEMA


def test_mcp_spawn_of_a_lead_gets_no_schema(orch: Orchestrator, project: str) -> None:
    spawned = mcp_server.spawn_agent("hello", project=project, role="lead", provider="fake")
    idle(orch, spawned["id"])

    assert run_schema(orch, spawned["run_id"]) is None


def test_mcp_send_message_applies_the_role_schema_unless_told_otherwise(
    orch: Orchestrator, project: str
) -> None:
    agent = orch.create_agent(project, name="w", worktree=False)
    default = mcp_server.send_message(agent.id, "one")
    free = mcp_server.send_message(agent.id, "two", freeform=True)
    own = mcp_server.send_message(agent.id, "three", output_schema=OWN_SCHEMA)
    idle(orch, agent.id)

    assert run_schema(orch, default["run_id"]) == worker_schema()
    assert run_schema(orch, free["run_id"]) is None
    assert run_schema(orch, own["run_id"]) == OWN_SCHEMA


def test_a_broken_role_schema_fails_the_spawn_without_creating_an_agent(
    orch: Orchestrator, project: str
) -> None:
    write_role(config.user_roles_dir(), "broken", "output_schema: gone.json")

    with pytest.raises(OrchestratorError, match="gone.json"):
        mcp_server.spawn_agent("hello", project=project, role="broken")
    assert orch.store.list_agents(project) == []


def test_cli_and_web_spawn_keep_free_text(orch: Orchestrator, project: str) -> None:
    from starlette.testclient import TestClient

    from orchestrator.web.app import app

    cli_out = CliRunner().invoke(cli.app, ["spawn", "hello", "-p", project, "--no-worktree"])
    web = TestClient(app, base_url="http://localhost").post(
        "/api/agents",
        json={"project": project, "task": "hello", "worktree": False},
        headers={"origin": "http://localhost"},
    )
    assert cli_out.exit_code == 0 and web.status_code == 200
    agents = [agent.id for agent in orch.store.list_agents(project)]
    idle(orch, *agents)
    sent = CliRunner().invoke(cli.app, ["send", agents[0], "again"])
    assert sent.exit_code == 0
    idle(orch, *agents)

    runs = [run for agent_id in agents for run in orch.store.runs_of(agent_id)]
    assert len(runs) == 3 and all(run.output_schema is None for run in runs)
