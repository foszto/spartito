"""Executes one run in its own detached process: `python -m orchestrator.runner <run_id>`.

The runner waits for the run's turn (dependencies, then the agent's turn order, see
`Store.may_start`), prepares the workspace, launches the provider CLI and translates its
output stream into events. Because every run has its own process there is no daemon to
keep alive: whoever queued the run (CLI, MCP server) may exit immediately. When the run
has ended, in whatever way, the runner checks whether a parent is due a notification.
"""

from __future__ import annotations

import contextlib
import dataclasses
import json
import os
import queue
import re
import signal
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from . import config, git, loadout, notify, providers, roles, settings
from .models import Agent, Event, Project, Run, RunStatus, Usage, running_total
from .providers import McpServer, RunContext
from .providers.base import NOTIFICATION_RESULT
from .store import MESSAGE, NOTIFICATION, Store

POLL_SECONDS = 0.5
RECONCILE_EVERY = 10  # polls
RESULT_REF = re.compile(r"\{\{result:([\w.-]+)(:full)?\}\}")
RESULT_CHARS = 4000  # text of a {{result:<agent-id>}} reference without :full


class Stopped(Exception):
    pass


def _raise_stopped(signum: int, frame: object) -> None:
    raise Stopped


def wait_for_turn(store: Store, run: Run) -> str | None:
    """Block until the message run has taken its agent's turn (it is then marked running).
    Returns a reason when it must be skipped instead; raises notify.Gone when the run was
    removed or ended by someone else meanwhile."""
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
        if not blocked:
            started = store.take_turn(run, MESSAGE)
            if started is None:
                raise notify.Gone
            if started:
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
    """Replace {{result:<agent-id>}} with the last successful result of that agent:
    structured output whole as compact JSON (never cut: cut JSON is useless), text cut at
    RESULT_CHARS with a line naming its length and the result file.
    {{result:<agent-id>:full}} injects the text uncut. Unknown or unfinished agents are
    left as they are."""

    def substitute(match: re.Match[str]) -> str:
        done = [r for r in store.runs_of(match.group(1)) if r.status is RunStatus.DONE]
        if not done:
            return match.group(0)
        last = done[-1]
        if last.structured is not None:
            return json.dumps(last.structured, ensure_ascii=False, separators=(",", ":"))
        text = last.result or ""
        if match.group(2) or len(text) <= RESULT_CHARS:
            return text
        path = config.result_file(last.id)
        where = f"; full text in {path}" if path.is_file() else ""
        return f"{text[:RESULT_CHARS]}\n[cut at {RESULT_CHARS} of {len(text)} chars{where}]"

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
    scratch = config.run_dir(run.id)
    scratch.mkdir(exist_ok=True)
    invocation = provider.invocation(
        RunContext(
            agent=agent,
            prompt=render_prompt(store, run.prompt),
            system_prompt=system_prompt(agent, project, role),
            scratch=scratch,
            mcp=mcp_server(agent) if role.lead else None,
            lead=role.lead,
            output_schema=run.output_schema,
            writable_dirs=writable,
            loadout=loadout.resolve(
                role,
                project.path if project else None,
                warn=lambda text: store.add_event(run.id, agent.id, "info", text),
            ),
            **settings.resolve(store, agent, role),
        )
    )
    env = {k: v for k, v in os.environ.items() if k not in invocation.unset_env}
    env.update(invocation.env)

    # Every process the CLI starts inherits this mark, also those that leave its session.
    token = f"{run.id}-{uuid.uuid4().hex}"
    env[TURN_ENV] = token
    mark = f"{TURN_ENV}={token}"

    stderr_path = scratch / "stderr.log"
    last_text, results, errors, reported_model, session_id = "", [], [], None, None
    answered = False  # a result answering the prompt (not a background-task notification)
    with (
        open(scratch / "stream.jsonl", "w", encoding="utf-8") as raw,
        open(stderr_path, "w", encoding="utf-8") as stderr,
    ):
        # In the runner's process group (the runner leads its own session and group, see
        # Orchestrator._launch), so a stop that kills the group ends the CLI as well.
        child = subprocess.Popen(
            invocation.argv,
            cwd=agent.cwd,
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=stderr,
            text=True,
        )
        assert child.stdin and child.stdout
        lines: queue.Queue[str | None] = queue.Queue()
        reader = threading.Thread(target=_pump, args=(child.stdout, lines), daemon=True)
        reader.start()
        known: dict[int, tuple[int, int]] = {}
        try:
            child.stdin.write(invocation.stdin)
            child.stdin.close()
            eof, exited_at = False, None
            while True:
                try:
                    line = lines.get(timeout=0.5)
                except queue.Empty:
                    if child.poll() is None:
                        continue
                    # Exited, but something it started may hold its stdout open: once
                    # nothing more arrives, stop waiting for an end of file.
                    exited_at = exited_at or time.monotonic()
                    if time.monotonic() - exited_at >= QUIET_AFTER_EXIT_SECONDS:
                        break
                    continue
                if line is None:
                    eof = True
                    break
                exited_at = None
                raw.write(line)
                raw.flush()
                for event in provider.parse(line):
                    if event.session_id:
                        store.set_session(agent.id, event.session_id)
                        session_id = event.session_id
                    if event.kind == "session":
                        reported_model = event.model or reported_model
                        continue
                    if event.kind in ("result", NOTIFICATION_RESULT):
                        results.append(event)
                        answered = answered or event.kind == "result"
                        continue
                    if event.kind == "text":
                        last_text = event.text
                    if event.kind == "error":
                        errors.append(event.text)
                    store.add_event(run.id, agent.id, event.kind, event.text)
                if answered and role.lead:
                    # A lead's turn ends with its answer: a background command it left
                    # running must not keep it unreachable (the CLI would stay alive and
                    # answer again when the command ends). What the turn started is
                    # noted now, while the CLI still links it.
                    known = turn_processes(child.pid, mark)
                    break
            if answered and role.lead:
                code = finish_lead_turn(child, mark, known)
            elif eof:
                code = child.wait()
            else:  # the CLI is gone; end whatever holds its stdout
                end_turn(child, mark)
                code = child.returncode
        except BaseException:
            end_turn(child, mark, known)
            raise
        finally:
            reader.join(timeout=2)
            if not reader.is_alive():
                child.stdout.close()

    result = combine_results(results)
    usage = record_usage(store, run, agent, result, reported_model, session_id)
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
        write_result(store, run, scratch, text, structured)
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
    write_result(store, run, scratch, last_text or error, None)
    store.finish_run(run.id, RunStatus.FAILED, error=error, result=last_text or None, cost_usd=cost)


LEAD_EXIT_GRACE_SECONDS = 5.0  # a lead's CLI may exit on its own after its answer
TERM_GRACE_SECONDS = 3.0  # between SIGTERM and SIGKILL; well below service.STOP_GRACE_SECONDS
QUIET_AFTER_EXIT_SECONDS = 3.0  # the CLI exited but something it started holds its stdout
TURN_ENV = "ORCH_TURN"  # marks the processes of one turn through their environment
RUNNER_ARGV = "-m orchestrator.runner"  # other runners: sub-agents a lead started


def _pump(stream: Any, lines: queue.Queue[str | None]) -> None:
    """Move the CLI's output lines to a queue (None at the end), so that reading can
    give up on a pipe that a leftover process keeps open."""
    try:
        for line in stream:
            lines.put(line)
    except OSError, ValueError:
        pass
    finally:
        lines.put(None)


def finish_lead_turn(
    child: subprocess.Popen[str], mark: str, known: dict[int, tuple[int, int]]
) -> int:
    """After a lead's answer: give the CLI a moment to exit, then end it and everything
    else of the turn. Returns its exit code, or 0 when it had to be ended: the turn is
    complete. A stop (SIGTERM to the runner) meanwhile only ends it sooner."""
    with contextlib.suppress(subprocess.TimeoutExpired, Stopped):
        child.wait(timeout=LEAD_EXIT_GRACE_SECONDS)
    code = child.returncode  # None: still running; < 0: ended by a signal
    end_turn(child, mark, known)
    return code if code is not None and code > 0 else 0


@dataclasses.dataclass(frozen=True)
class _Proc:
    ppid: int
    pgid: int
    sid: int
    command: str  # on macOS followed by the environment, where ps may show it


def _process_table() -> dict[int, _Proc]:
    """Every process; empty when ps is unavailable."""
    darwin = sys.platform == "darwin"
    argv = ["ps", "-A", "-ww", *(["-E"] if darwin else []), "-o", "pid=,ppid=,pgid=,command="]
    try:
        out = subprocess.run(
            argv, capture_output=True, text=True, errors="replace", timeout=10
        ).stdout
    except OSError, subprocess.SubprocessError:
        return {}
    table = {}
    for line in out.splitlines():
        fields = line.split(None, 3)
        if len(fields) < 3 or not all(field.isdigit() for field in fields[:3]):
            continue
        try:
            sid = os.getsid(int(fields[0]))
        except OSError:
            continue
        command = fields[3] if len(fields) > 3 else ""
        table[int(fields[0])] = _Proc(int(fields[1]), int(fields[2]), sid, command)
    return table


def _marked(pid: int, proc: _Proc, mark: str) -> bool:
    """Whether the process inherited the turn's mark. macOS shows the environment in ps
    (not for system binaries such as /bin/sleep), Linux in /proc."""
    if mark in proc.command:
        return True
    if sys.platform == "darwin":
        return False
    try:
        return mark.encode() in Path(f"/proc/{pid}/environ").read_bytes()
    except OSError:
        return False


def turn_processes(
    cli: int, mark: str, known: dict[int, tuple[int, int]] | None = None
) -> dict[int, tuple[int, int]]:
    """The processes of a turn, pid -> (process group, session): the CLI and its
    descendants; the other members of the runner's session when it leads one (as the
    service launches it); processes that inherited the turn's mark; `known` ones that are
    still the same processes; and the members of process groups and sessions that any of
    these created (Claude's shells start sessions of their own, and what they leave
    behind keeps their ids). Other runners (sub-agents a lead started) and everything in
    their sessions are left out."""
    known = known or {}
    table = _process_table()
    me = os.getpid()
    own_ids = {os.getpgid(0), os.getsid(0)}
    leads_session = os.getsid(0) == me
    runners = {pid for pid, proc in table.items() if pid != me and RUNNER_ARGV in proc.command}

    def foreign(pid: int, proc: _Proc) -> bool:
        return pid == me or pid in runners or proc.sid in runners or proc.pgid in runners

    def same(pid: int) -> bool:  # a known pid not reused by another process since
        return pid not in table or (table[pid].pgid, table[pid].sid) == known[pid]

    found = {
        pid
        for pid, proc in table.items()
        if not foreign(pid, proc)
        and (
            pid == cli
            or (leads_session and proc.sid == me)
            or (pid in known and same(pid))
            or _marked(pid, proc, mark)
        )
    }
    known_ids = {pid for pid in known if same(pid)}
    while True:
        creators = (found | known_ids) - own_ids
        more = {
            pid
            for pid, proc in table.items()
            if pid not in found
            and not foreign(pid, proc)
            and (proc.ppid in found or proc.pgid in creators or proc.sid in creators)
        }
        if not more:
            return {pid: (table[pid].pgid, table[pid].sid) for pid in found}
        found |= more


def _still(pid: int, ids: tuple[int, int]) -> bool:
    """Whether the pid still is that process (same group and session)."""
    try:
        return (os.getpgid(pid), os.getsid(pid)) == ids
    except OSError:
        return False


def _send(procs: dict[int, tuple[int, int]], sig: int) -> None:
    for pid, ids in procs.items():
        if _still(pid, ids):
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.kill(pid, sig)


@contextlib.contextmanager
def _stop_ignored() -> Any:
    """Ignore SIGTERM/SIGINT (a stop) while the turn is being ended: that is the stop."""
    if threading.current_thread() is not threading.main_thread():
        yield
        return
    previous = {sig: signal.signal(sig, signal.SIG_IGN) for sig in (signal.SIGTERM, signal.SIGINT)}
    try:
        yield
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)


def end_turn(
    child: subprocess.Popen[str], mark: str, known: dict[int, tuple[int, int]] | None = None
) -> None:
    """End the CLI and every other process of the turn (see turn_processes): SIGTERM,
    then, after at most TERM_GRACE_SECONDS, SIGKILL to what a fresh look still finds.
    Signals go to processes only, never to the runner's own group."""
    known = dict(known or {})
    with _stop_ignored():
        child.poll()
        procs = turn_processes(child.pid, mark, known)
        _send(procs, signal.SIGTERM)
        deadline = time.monotonic() + TERM_GRACE_SECONDS
        while time.monotonic() < deadline:
            child.poll()  # reap the CLI so that it does not count as alive
            if not any(_still(pid, ids) for pid, ids in procs.items()):
                break
            time.sleep(0.1)
        child.poll()
        _send(turn_processes(child.pid, mark, {**known, **procs}), signal.SIGKILL)
        with contextlib.suppress(subprocess.TimeoutExpired):
            child.wait(timeout=TERM_GRACE_SECONDS)


def combine_results(results: list[Event]) -> Event | None:
    """One result for a run whose CLI reported several (it answered again after a
    background command ended): text, status and structured output of the last; usage
    summed when the provider reports it per result, the last when it reports session
    totals."""
    if not results:
        return None
    last = results[-1]
    if len(results) == 1:
        return last
    if last.usage is not None and last.usage.cumulative:
        return last
    reported = [
        r.usage or Usage(cost_usd=r.cost_usd)
        for r in results
        if r.usage is not None or r.cost_usd is not None
    ]
    if not reported:
        return last
    usage = sum(reported[1:], reported[0])
    return dataclasses.replace(last, usage=usage, cost_usd=usage.cost_usd)


def write_result(store: Store, run: Run, scratch: Path, text: str, structured: Any) -> None:
    """Write the full final answer to result.md (a failed run: its last text, else the
    error) and structured output to result.json, each atomically.

    Called before the run is marked finished, so whoever sees it finished (a dependent
    run, get_agent) finds the file. A side channel: a failed write only adds an event and
    the run's outcome is stored as usual.
    """
    files = [("result.md", text)]
    if structured is not None:
        files.append(("result.json", json.dumps(structured, ensure_ascii=False, indent=2)))
    try:
        for name, content in files:
            partial = scratch / f".{name}.tmp"
            partial.write_text(content, encoding="utf-8")
            os.replace(partial, scratch / name)
    except OSError as exc:
        store.add_event(run.id, run.agent_id, "error", f"could not write the result file: {exc}")


def record_usage(
    store: Store,
    run: Run,
    agent: Agent,
    result: Event | None,
    model: str | None,
    session_id: str | None = None,
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
        # The CLI reported totals for the whole session/thread (Codex always; Claude without
        # model_usage_since_last_result): this run's share is what exceeds the running total
        # of the agent's earlier runs in the same session. Runs recorded under another
        # session id are left out (the session was started over); runs without a recorded
        # session id (old code, or none reported) count. Agent totals stay correct, but
        # per-run values are off in two known cases: the first run under this code of an
        # agent whose earlier runs had no usage records (old code) gets their tokens too
        # (Claude's legacy session cost is known and subtracted, tokens are not), and the
        # tokens of a turn that failed or was stopped without a result land in the agent's
        # next run.
        recorded = store.usage_of(agent.id)

        def same_session(other: Run) -> bool:
            other_session = recorded[other.id].session_id if other.id in recorded else None
            return not session_id or other_session in (None, session_id)

        earlier = [o for o in store.runs_of(agent.id) if o.id < run.id and same_session(o)]
        usage = usage.since(running_total(earlier, recorded))
    usage.model = model or usage.model
    usage.session_id = session_id
    store.set_usage(run.id, agent.id, usage)
    return usage


def main(run_id: int) -> None:
    from .service import Orchestrator  # the service launches runners; import it lazily

    store = Store()
    orch = Orchestrator(store)
    run = store.get_run(run_id)
    if run is None or not run.status.active:
        return
    kind = store.meta(run_id).kind
    store.update_run(run_id, pid=os.getpid())
    signal.signal(signal.SIGTERM, _raise_stopped)
    signal.signal(signal.SIGINT, _raise_stopped)
    try:
        take_turn(store, run, kind)
    finally:
        # Whatever happened, the run is over: maybe a parent is now due a notification.
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        signal.signal(signal.SIGINT, signal.SIG_IGN)
        orch.notify_parents(run.agent_id)


def take_turn(store: Store, run: Run, kind: str) -> None:
    """Wait for the run's turn and execute it; record how it ended."""
    run_id = run.id
    try:
        if kind == NOTIFICATION:
            prepared = notify.start(store, run)
            if prepared is None:
                return  # nothing to report: the run was deleted without a turn
            run = prepared
        else:
            skip_reason = wait_for_turn(store, run)
            if skip_reason:
                store.add_event(run_id, run.agent_id, "error", f"skipped: {skip_reason}")
                store.finish_run(run_id, RunStatus.SKIPPED, error=skip_reason)
                return
        agent = store.get_agent(run.agent_id)
        project = store.get_project(agent.project) if agent and agent.project else None
        if agent is None or (agent.project and project is None):
            raise RuntimeError("agent or project was removed")
        store.add_event(
            run_id, agent.id, "notification" if kind == NOTIFICATION else "prompt", run.prompt
        )
        execute(store, run, agent, project)
    except notify.Gone:
        return
    except Stopped:
        fresh = store.get_run(run_id)
        if kind == NOTIFICATION and fresh and fresh.started_at is None:
            store.delete_run(run_id)  # it never had a turn; do not leave it as the last run
            return
        store.add_event(run_id, run.agent_id, "error", "stopped")
        store.finish_run(run_id, RunStatus.STOPPED, error="stopped")
    except Exception as exc:  # the run must never stay 'running' after its runner is gone
        message = f"{type(exc).__name__}: {exc}"
        store.add_event(run_id, run.agent_id, "error", message)
        store.finish_run(run_id, RunStatus.FAILED, error=message)


if __name__ == "__main__":
    main(int(sys.argv[1]))
