"""Executes one run in its own detached process: `python -m orchestrator.runner <run_id>`.

The runner waits for the run's turn (dependencies, then earlier runs of the same agent),
prepares the workspace, launches the provider CLI and translates its output stream into
events. Because every run has its own process there is no daemon to keep alive: whoever
queued the run (CLI, MCP server) may exit immediately.
"""

from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import sys
import time
from pathlib import Path

from . import config, git, loadout, providers, roles, settings
from .models import Agent, Event, Project, Run, RunStatus, Usage
from .providers import McpServer, RunContext
from .store import Store, now

POLL_SECONDS = 0.5
RECONCILE_EVERY = 10  # polls
RESULT_REF = re.compile(r"\{\{result:([\w.-]+)\}\}")


class Stopped(Exception):
    pass


def _raise_stopped(signum: int, frame: object) -> None:
    raise Stopped


def wait_for_turn(store: Store, run: Run) -> str | None:
    """Block until the run may start. Returns a reason when it must be skipped instead."""
    polls = 0
    while True:
        blocked = False
        for dep_id in run.depends_on:
            dep = store.get_run(dep_id)
            if dep is None:
                return f"dependency run {dep_id} no longer exists"
            if dep.status.active:
                blocked = True
            elif dep.status is not RunStatus.DONE:
                return f"dependency run {dep_id} ({dep.agent_id}) ended as {dep.status.value}"
        earlier = [other for other in store.active_runs(run.agent_id) if other.id < run.id]
        if not blocked and not earlier:
            return None
        polls += 1
        if polls % RECONCILE_EVERY == 0:
            store.reconcile()
        time.sleep(POLL_SECONDS)


def prepare_workspace(agent: Agent, project: Project | None) -> list[str]:
    """Create the agent's worktree on first use; return extra dirs it must be able to write."""
    cwd = Path(agent.cwd)
    if not cwd.exists():
        if not agent.owns_worktree or project is None:
            raise RuntimeError(f"workspace {cwd} does not exist (is its owner agent done yet?)")
        git.add_worktree(project.path, cwd, agent.branch, agent.base_ref or project.base_branch)
    try:
        shared_git = git.common_dir(cwd)
    except git.GitError:
        return []
    # In a worktree commits land in the main repository's .git, outside the cwd.
    return [] if shared_git.is_relative_to(cwd) else [str(shared_git)]


def render_prompt(store: Store, prompt: str) -> str:
    """Replace {{result:<agent-id>}} with the last successful result of that agent."""

    def substitute(match: re.Match[str]) -> str:
        done = [r for r in store.runs_of(match.group(1)) if r.status is RunStatus.DONE]
        if not done:
            return match.group(0)
        last = done[-1]
        if last.structured is not None:
            return json.dumps(last.structured, ensure_ascii=False, indent=2)
        return last.result or ""

    return RESULT_REF.sub(substitute, prompt)


def system_prompt(agent: Agent, project: Project | None, role: roles.Role) -> str:
    context = [
        f"You are agent `{agent.id}` (role: {agent.role})"
        + (f" in project `{project.name}`." if project else "."),
        f"Working directory: {agent.cwd}",
    ]
    if agent.branch:
        context.append(
            f"You work on branch `{agent.branch}` in your own git worktree, "
            f"based on `{agent.base_ref}`. Never switch branches."
        )
    return f"{role.prompt}\n\n## Context\n" + "\n".join(context)


def mcp_server(agent: Agent) -> McpServer:
    return McpServer(
        command=sys.executable,
        args=["-m", "orchestrator.mcp_server"],
        env={"ORCH_HOME": str(config.home()), "ORCH_AGENT_ID": agent.id},
    )


def execute(store: Store, run: Run, agent: Agent, project: Project | None) -> None:
    role = roles.load(agent.role, project.path if project else None)
    provider = providers.get(agent.provider)
    if project and agent.owns_worktree and git.ensure_initial_commit(project.path):
        note = "the repository had no commits: created an empty initial commit to branch from"
        store.add_event(run.id, agent.id, "info", note)
    writable = prepare_workspace(agent, project)
    scratch = config.logs_dir() / f"run-{run.id}"
    scratch.mkdir(exist_ok=True)
    invocation = provider.invocation(
        RunContext(
            agent=agent,
            prompt=render_prompt(store, run.prompt),
            system_prompt=system_prompt(agent, project, role),
            scratch=scratch,
            mcp=mcp_server(agent) if role.lead else None,
            output_schema=run.output_schema,
            writable_dirs=writable,
            loadout=loadout.resolve(role, project.path if project else None),
            **settings.resolve(store, agent, role),
        )
    )
    env = {k: v for k, v in os.environ.items() if k not in invocation.unset_env}
    env.update(invocation.env)

    stderr_path = scratch / "stderr.log"
    last_text, result, errors, reported_model = "", None, [], None
    with (
        open(scratch / "stream.jsonl", "w", encoding="utf-8") as raw,
        open(stderr_path, "w", encoding="utf-8") as stderr,
    ):
        child = subprocess.Popen(
            invocation.argv,
            cwd=agent.cwd,
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=stderr,
            text=True,
        )
        try:
            assert child.stdin and child.stdout
            child.stdin.write(invocation.stdin)
            child.stdin.close()
            for line in child.stdout:
                raw.write(line)
                raw.flush()
                for event in provider.parse(line):
                    if event.session_id:
                        store.set_session(agent.id, event.session_id)
                    if event.kind == "session":
                        reported_model = event.model or reported_model
                        continue
                    if event.kind == "result":
                        result = event
                        continue
                    if event.kind == "text":
                        last_text = event.text
                    if event.kind == "error":
                        errors.append(event.text)
                    store.add_event(run.id, agent.id, event.kind, event.text)
            code = child.wait()
        except BaseException:
            child.terminate()
            try:
                child.wait(timeout=10)
            except subprocess.TimeoutExpired:
                child.kill()
            raise

    usage = record_usage(store, run, agent, result, reported_model)
    cost = usage.cost_usd if usage else None
    if result and not result.is_error and code == 0:
        text = result.text or last_text
        structured = result.structured
        if structured is None and run.output_schema:
            try:
                structured = json.loads(text)
            except json.JSONDecodeError:
                structured = None
        if text != last_text:  # otherwise the activity log already ends with it
            store.add_event(run.id, agent.id, "result", text)
        store.finish_run(run.id, RunStatus.DONE, result=text, structured=structured, cost_usd=cost)
        return
    stderr_tail = stderr_path.read_text(encoding="utf-8", errors="replace").strip()[-1500:]
    error = (
        (result.text if result else "")
        or "; ".join(errors)
        or stderr_tail
        or f"{invocation.argv[0]} exited with code {code} without a result"
    )
    store.add_event(run.id, agent.id, "error", error)
    store.finish_run(run.id, RunStatus.FAILED, error=error, result=last_text or None, cost_usd=cost)


def record_usage(
    store: Store, run: Run, agent: Agent, result: Event | None, model: str | None
) -> Usage | None:
    """Store the run's own tokens and cost; providers may report session totals instead."""
    if result is None:
        return None
    usage = result.usage
    if usage is None and result.cost_usd is not None:
        usage = Usage(cost_usd=result.cost_usd)
    if usage is None:
        return None
    if usage.cumulative:
        earlier = [u for run_id, u in store.usage_of(agent.id).items() if run_id != run.id]
        usage = usage.since(sum(earlier, Usage()))
    usage.model = model or usage.model
    store.set_usage(run.id, agent.id, usage)
    return usage


def main(run_id: int) -> None:
    store = Store()
    run = store.get_run(run_id)
    if run is None or not run.status.active:
        return
    store.update_run(run_id, pid=os.getpid())
    signal.signal(signal.SIGTERM, _raise_stopped)
    signal.signal(signal.SIGINT, _raise_stopped)
    try:
        skip_reason = wait_for_turn(store, run)
        if skip_reason:
            store.add_event(run_id, run.agent_id, "error", f"skipped: {skip_reason}")
            store.finish_run(run_id, RunStatus.SKIPPED, error=skip_reason)
            return
        agent = store.get_agent(run.agent_id)
        project = store.get_project(agent.project) if agent and agent.project else None
        if agent is None or (agent.project and project is None):
            raise RuntimeError("agent or project was removed")
        store.update_run(run_id, status=RunStatus.RUNNING, started_at=now())
        store.add_event(run_id, agent.id, "prompt", run.prompt)
        execute(store, run, agent, project)
    except Stopped:
        store.add_event(run_id, run.agent_id, "error", "stopped")
        store.finish_run(run_id, RunStatus.STOPPED, error="stopped")
    except Exception as exc:  # the run must never stay 'running' after its runner is gone
        message = f"{type(exc).__name__}: {exc}"
        store.add_event(run_id, run.agent_id, "error", message)
        store.finish_run(run_id, RunStatus.FAILED, error=message)


if __name__ == "__main__":
    main(int(sys.argv[1]))
