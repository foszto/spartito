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

from . import config, git, keep_awake, loadout, notify, providers, roles, settings
from .models import (
    CONTEXT_FIELDS,
    Agent,
    Event,
    Project,
    Run,
    RunStatus,
    Usage,
    answers,
    context_now,
    running_total,
)
from .providers import McpServer, RunContext
from .providers.base import NOTIFICATION_RESULT, Invocation
from .store import COMPACT, MESSAGE, NOTIFICATION, Store, now

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
        # Withdrawn meanwhile (Orchestrator.withdraw deletes it): end at this safe point.
        if store.get_run(run.id) is None:
            raise notify.Gone
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
    """Replace {{result:<agent-id>}} with the last successful result of that agent (not a
    compaction):
    structured output whole as compact JSON (never cut: cut JSON is useless), text cut at
    RESULT_CHARS with a line naming its length and the result file.
    {{result:<agent-id>:full}} injects the text uncut. Unknown or unfinished agents are
    left as they are."""

    def substitute(match: re.Match[str]) -> str:
        agent_id = match.group(1)
        runs = answers(store.runs_of(agent_id), store.compactions(agent_id))
        done = [r for r in runs if r.status is RunStatus.DONE]
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
    # A sub-agent's runner inherits the mark of the lead's turn (through the lead's MCP
    # server); nothing this runner starts (git, hooks) may carry it.
    os.environ.pop(TURN_ENV, None)
    role = roles.load(agent.role, project.path if project else None)
    provider = providers.get(agent.provider)
    kind = store.meta(run.id).kind
    compact = kind == COMPACT
    began = now()
    recorded = store.usage_of(agent.id)
    before = context_now(store.runs_of(agent.id), recorded, agent.session_id)
    if compact and getattr(provider, "compact_needs_size", False) and compact_limit(before) is None:
        # Checked when it was queued, but e.g. a compaction just before left it unknown.
        store.add_event(run.id, agent.id, "error", SIZE_UNKNOWN)
        store.finish_run(run.id, RunStatus.FAILED, error=SIZE_UNKNOWN)
        return
    if project and agent.owns_worktree and git.ensure_initial_commit(project.path):
        note = "the repository had no commits: created an empty initial commit to branch from"
        store.add_event(run.id, agent.id, "info", note)
    writable = prepare_workspace(agent, project)
    scratch = config.run_dir(run.id)
    scratch.mkdir(exist_ok=True)
    resolved = loadout.resolve(
        role,
        project.path if project else None,
        warn=lambda text: store.add_event(run.id, agent.id, "info", text),
    )
    if compact:  # the CLI's own `/compact` is a slash command; the rest of the loadout stays
        resolved = dataclasses.replace(resolved, slash_commands=True)
    ctx = RunContext(
        agent=agent,
        # A notification carries child output: data, never {{result:…}} references. A
        # compaction's prompt must reach the CLI exactly as it is (`/compact …`).
        prompt=run.prompt if kind in (NOTIFICATION, COMPACT) else render_prompt(store, run.prompt),
        system_prompt=system_prompt(agent, project, role),
        scratch=scratch,
        mcp=mcp_server(agent) if role.lead else None,
        lead=role.lead,
        output_schema=None if compact else run.output_schema,
        writable_dirs=writable,
        loadout=resolved,
        compact_below=compact_limit(before) if compact else None,
        **settings.resolve(store, agent, role),
    )
    invocation = provider.invocation(ctx)
    plain_ctx = dataclasses.replace(ctx, loadout=dataclasses.replace(ctx.loadout, memory=False))
    if invocation.canary is not None and hooks_known_off(agent):
        invocation = _plain(provider, plain_ctx)
    # Every process the CLI starts inherits this mark, also those that leave its session.
    token = f"{run.id}-{uuid.uuid4().hex}"
    mark = f"{TURN_ENV}={token}"

    stderr_path = scratch / "stderr.log"
    last_text, results, errors, reported_model, session_id = "", [], [], None, None
    # The size after the last model request (None after a compaction no request followed),
    # a compaction seen, and the context window per model from the results.
    context: Usage | None = None
    first_context: Usage | None = None  # the run's first model request
    compaction: Usage | None = None
    windows: dict[str, int] = {}
    answered = False  # a result answering the prompt (not a background-task notification)
    with (
        open(scratch / "stream.jsonl", "w", encoding="utf-8") as raw,
        open(stderr_path, "w", encoding="utf-8") as stderr,
    ):
        child = _start(invocation, agent.cwd, token, stderr)
        if invocation.canary is not None:
            try:
                outcome = wait_for_canary(child, invocation.canary)
                if outcome == HOOKS_OFF:  # it has read no prompt yet: nothing ran unguarded
                    end_turn(child, mark)
            except BaseException:
                end_turn(child, mark)
                raise
            # EXITED: no relaunch; the turn fails below with the CLI's own error.
            if outcome == HOOKS_RAN:
                note_hooks(store, run.id, agent.id, off=False)
            elif outcome == HOOKS_OFF and invocation.canary.exists():
                # It came while the CLI was being ended: a slow start, not hooks being off.
                # This turn goes on without memory; the next one tries again.
                store.add_event(run.id, agent.id, "info", MEMORY_SLOW)
            elif outcome == HOOKS_OFF:
                note_hooks(store, run.id, agent.id, off=True)
            if outcome == HOOKS_OFF:
                for pipe in (child.stdin, child.stdout):
                    if pipe:
                        pipe.close()
                invocation = _plain(provider, plain_ctx)
                child = _start(invocation, agent.cwd, token, stderr)
        assert child.stdin and child.stdout
        lines: queue.Queue[str | None] = queue.Queue()
        reader = threading.Thread(target=_pump, args=(child.stdout, lines), daemon=True)
        reader.start()
        known: dict[int, tuple[int, int]] = {}
        try:
            with contextlib.suppress(BrokenPipeError):  # a CLI that exited unasked
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
                    if event.kind == "context":
                        context = event.usage
                        first_context = first_context or event.usage
                        continue
                    if event.kind == "compact":
                        compaction, context = event.usage, None
                        store.add_event(run.id, agent.id, "info", event.text)
                        continue
                    if event.kind in ("result", NOTIFICATION_RESULT):
                        windows.update(event.windows or {})
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
                    known = turn_processes(_live_pid(child), mark)
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
    # A run without a result (the CLI died) reports no window: the model's known one.
    known = {u.model: u.context_window for u in recorded.values() if u.model and u.context_window}
    measured = measured_context(context, compaction, {**known, **windows}, first_context)
    context_after = getattr(provider, "context_after", None)
    if context_after and session_id:
        read = context_after(session_id, began)
        if read and read.compact_trigger and compaction is None:
            read.compact_pre_tokens = before.context_tokens if before else None
            read.compact_post_tokens = read.context_tokens
            store.add_event(run.id, agent.id, "info", compaction_note(read))
        measured = read or measured
    usage = record_usage(store, run, agent, result, reported_model, session_id, measured)
    cost = usage.cost_usd if usage else None
    if compact and result and not result.is_error and code == 0 and not measured.compact_trigger:
        # Nothing was compacted, e.g. "/compact isn't available in this environment." (or
        # a Codex model's no-op reply, which must not read as success).
        answer = one_line(result.text or last_text)
        failure = NOT_COMPACTED + (f" (the CLI answered: {answer})" if answer else "")
        result = dataclasses.replace(result, text=failure, is_error=True)
    if result and not result.is_error and code == 0:
        text = compact_summary(measured) if compact else result.text or last_text
        structured = result.structured
        if structured is None and run.output_schema:
            try:
                structured = json.loads(text)
            except json.JSONDecodeError:
                structured = None
        if text != last_text:  # otherwise the activity log already ends with it
            store.add_event(run.id, agent.id, "result", text)
        write_result(store, run, scratch, text, structured)
        if not compact:  # queued while this turn still holds the agent: nothing comes first
            queue_auto_compaction(store, run.id)
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


CANARY_SECONDS = 15.0  # the CLI's SessionStart hooks run within about 0.2 s of its start
CANARY_ENV = "ORCH_CANARY_SECONDS"  # overrides CANARY_SECONDS (tests)
HOOKS_RAN, HOOKS_OFF, EXITED = "ran", "off", "exited"
RECHECK_SECONDS = 24 * 3600  # a record of hooks being off is trusted this long
MEMORY_OFF = (
    "memory writes are off: hooks are disabled by settings or policy, so the guard that "
    "keeps Write/Edit inside the memory directory cannot run; turns run without them and "
    "check again in a new session or after a day"
)
MEMORY_ON = "memory writes are on again: hooks run in this session"
MEMORY_SLOW = (
    "memory writes are off for this turn: the CLI started too slowly to prove that its hooks "
    "run; the next turn tries again"
)


def _plain(provider: Any, ctx: RunContext) -> Invocation:
    invocation = provider.invocation(ctx)
    if invocation.canary is not None:
        raise RuntimeError(f"{provider.name} keeps memory on without hooks")
    return invocation


def hooks_record(agent_id: str) -> Path:
    """Where the runner notes that an agent's hooks were off (absent: not known to be)."""
    return config.home() / "hooks-off" / f"{re.sub(r'[^\w.-]', '_', agent_id)}.json"


def hooks_known_off(agent: Agent) -> bool:
    """A fresh record says this agent's hooks are off: launch without memory, no canary.
    A new session, or a record older than RECHECK_SECONDS, tries the canary again."""
    if agent.session_id is None:
        return False
    try:
        checked = float(json.loads(hooks_record(agent.id).read_text(encoding="utf-8"))["checked"])
    except OSError, ValueError, KeyError, TypeError:
        return False
    return 0 <= time.time() - checked < RECHECK_SECONDS


def note_hooks(store: Store, run_id: int, agent_id: str, off: bool) -> None:
    """Keep the record of the canary's outcome; the info event only when it changes.
    Advisory only: a record that cannot be written or removed never fails the turn."""
    path = hooks_record(agent_id)
    was_off = path.exists()
    with contextlib.suppress(OSError):
        if off:
            path.parent.mkdir(exist_ok=True)
            path.write_text(json.dumps({"checked": time.time()}), encoding="utf-8")
        else:
            path.unlink(missing_ok=True)
    if off != was_off:
        store.add_event(run_id, agent_id, "info", MEMORY_OFF if off else MEMORY_ON)


def _start(invocation: Invocation, cwd: str, token: str, stderr: Any) -> subprocess.Popen[str]:
    env = {k: v for k, v in os.environ.items() if k not in invocation.unset_env}
    env.update(invocation.env)
    env[TURN_ENV] = token
    if invocation.canary is not None:
        invocation.canary.unlink(missing_ok=True)
    # In the runner's process group (the runner leads its own session and group, see
    # Orchestrator._launch), so a stop that kills the group ends the CLI as well.
    return subprocess.Popen(
        invocation.argv,
        cwd=cwd,
        env=env,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=stderr,
        text=True,
    )


def wait_for_canary(
    child: subprocess.Popen[str], canary: Path, timeout: float | None = None
) -> str:
    """HOOKS_RAN once the CLI has created `canary` (its SessionStart hook) before it was sent
    anything; HOOKS_OFF when it is still waiting for input after the time: its hooks, the
    guard among them, do not run in this session; EXITED when it ended first (a failure of
    its own: login, flags, a missing session), which says nothing about hooks."""
    if timeout is None:
        timeout = float(os.environ.get(CANARY_ENV) or CANARY_SECONDS)
    deadline = time.monotonic() + timeout
    while not canary.exists():
        if child.poll() is not None:
            return HOOKS_RAN if canary.exists() else EXITED
        if time.monotonic() >= deadline:
            return HOOKS_RAN if canary.exists() else HOOKS_OFF
        time.sleep(0.05)
    return HOOKS_RAN


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


def _live_pid(child: subprocess.Popen[str]) -> int | None:
    """The CLI's pid while it is not reaped: afterwards another process may hold it."""
    return child.pid if child.poll() is None else None


def turn_processes(
    cli: int | None, mark: str, known: dict[int, tuple[int, int]] | None = None
) -> dict[int, tuple[int, int]]:
    """The processes of a turn, pid -> (process group, session): the CLI (`cli`, None
    once it has exited) and its descendants; the other members of the runner's session when it leads one (as the
    service launches it); processes that inherited the turn's mark; `known` ones that are
    still the same processes; and the members of process groups and sessions that any of
    these created (Claude's shells start sessions of their own, and what they leave
    behind keeps their ids). Other runners (sub-agents a lead started) and everything in
    their sessions are left out, as is the runner's sleep inhibitor (keep_awake.held)."""
    known = known or {}
    table = _process_table()
    me = os.getpid()
    own_ids = {os.getpgid(0), os.getsid(0)}
    leads_session = os.getsid(0) == me
    runners = {pid for pid, proc in table.items() if pid != me and RUNNER_ARGV in proc.command}

    def foreign(pid: int, proc: _Proc) -> bool:
        return (
            pid == me
            or pid in runners
            or proc.sid in runners
            or proc.pgid in runners
            # The runner's sleep inhibitor (and its child): it lasts until the run is stored.
            or pid in keep_awake.held
            or proc.sid in keep_awake.held
        )

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
        procs = turn_processes(_live_pid(child), mark, known)
        _send(procs, signal.SIGTERM)
        deadline = time.monotonic() + TERM_GRACE_SECONDS
        while time.monotonic() < deadline:
            child.poll()  # reap the CLI so that it does not count as alive
            if not any(_still(pid, ids) for pid, ids in procs.items()):
                break
            time.sleep(0.1)
        child.poll()
        _send(turn_processes(_live_pid(child), mark, {**known, **procs}), signal.SIGKILL)
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


def measured_context(
    context: Usage | None,
    compaction: Usage | None,
    windows: dict[str, int],
    first: Usage | None = None,
) -> Usage:
    """The context fields of a run's usage from what its stream reported (`first`: its
    first model request)."""
    measured = Usage()
    if first is not None:
        measured.first_context_tokens = first.context_tokens
    if compaction is not None:
        for key in ("compact_trigger", "compact_pre_tokens", "compact_post_tokens"):
            setattr(measured, key, getattr(compaction, key))
    if context is not None:
        measured.context_tokens = context.context_tokens
        window = windows.get(context.model or "")
        if window is None and len(set(windows.values())) == 1:
            window = next(iter(windows.values()))
        measured.context_window = window
    return measured


NOT_COMPACTED = "nothing was compacted"
SIZE_UNKNOWN = (
    "not compacted: the conversation's size is not known (e.g. right after a compaction);"
    " it is after the agent's next turn"
)


def one_line(text: str, limit: int = 200) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[:limit] + "…"


def compact_limit(before: Usage | None) -> int | None:
    """A compaction turn's token limit for a CLI that compacts above it: half the size."""
    if before is None or not before.context_tokens:
        return None
    return max(1, before.context_tokens // 2)


def kilo(tokens: int | None) -> str:
    if tokens is None:
        return "?"
    return f"{tokens / 1000:.0f}k" if tokens >= 1000 else str(tokens)


def compact_summary(usage: Usage) -> str:
    """The stored result of a compaction run, e.g. `compacted 48k -> 1k tokens`."""
    return f"compacted {kilo(usage.compact_pre_tokens)} -> {kilo(usage.compact_post_tokens)} tokens"


def compaction_note(usage: Usage) -> str:
    """The info event of a compaction the provider reported outside its stream."""
    sizes = f"{usage.compact_pre_tokens or '?'} -> {usage.compact_post_tokens or '?'} tokens"
    return f"context compacted ({usage.compact_trigger}): {sizes}"


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
    measured: Usage | None = None,
) -> Usage | None:
    """Store the run's own tokens and cost; providers may report session totals instead.
    The context fields come from `measured` (see measured_context), also without a result."""
    usage = result.usage if result else None
    if usage is None and result and result.cost_usd is not None:
        usage = Usage(cost_usd=result.cost_usd)
    has_context = measured is not None and any(getattr(measured, k) for k in CONTEXT_FIELDS)
    if usage is None and not has_context:
        return None
    if usage is None:
        usage = Usage()
    elif usage.cumulative:
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

        # The runs that took their turn before this one: those that have ended (one run of
        # an agent at a time; an automatic compaction runs ahead of lower ids).
        earlier = [
            o
            for o in store.runs_of(agent.id)
            if o.id != run.id and not o.status.active and same_session(o)
        ]
        usage = usage.since(running_total(earlier, recorded))
    usage.model = model or usage.model
    usage.session_id = session_id
    for key in CONTEXT_FIELDS:
        setattr(usage, key, getattr(measured, key) if measured else None)
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
        if kind == COMPACT:
            try:
                orch.compaction_ended(run_id)
            except Exception as exc:
                print(f"checking the compaction's outcome failed: {exc!r}", file=sys.stderr)
        orch.run_ended(run_id, run.agent_id)


def take_turn(store: Store, run: Run, kind: str) -> None:
    """Wait for the run's turn and execute it; record how it ended. From the turn taken
    until the run is stored, the machine is kept from sleeping on idle (keep_awake)."""
    run_id = run.id
    inhibitor = None
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
            # The text as it is now that the turn is taken: it may have been replaced
            # while queued (Store.replace_queued), never after.
            run = store.get_run(run_id) or run
        agent = store.get_agent(run.agent_id)
        project = store.get_project(agent.project) if agent and agent.project else None
        if agent is None or (agent.project and project is None):
            raise RuntimeError("agent or project was removed")
        store.add_event(
            run_id, agent.id, "notification" if kind == NOTIFICATION else "prompt", run.prompt
        )
        inhibitor = hold_awake(store, run_id, agent.id)
        execute(store, run, agent, project)
    except notify.Gone:
        return
    except Stopped:
        withdraw_auto_queued(store)
        if not _drop_unstarted(store, run_id, kind):
            store.add_event(run_id, run.agent_id, "error", "stopped")
            store.finish_run(run_id, RunStatus.STOPPED, error="stopped")
    except Exception as exc:  # the run must never stay 'running' after its runner is gone
        if not _drop_unstarted(store, run_id, kind):
            message = f"{type(exc).__name__}: {exc}"
            store.add_event(run_id, run.agent_id, "error", message)
            store.finish_run(run_id, RunStatus.FAILED, error=message)
    finally:
        keep_awake.release(inhibitor, store, run_id, run.agent_id)


auto_queued: list[int] = []  # the automatic compaction this runner's turn queued


def queue_auto_compaction(store: Store, run_id: int) -> None:
    """Orchestrator.auto_compact, which must never fail the turn whose end calls it; only a
    stop goes through (take_turn then withdraws what was queued)."""
    from .service import Orchestrator  # the service launches runners; import it lazily

    try:
        queued = Orchestrator(store).auto_compact(run_id)
    except Stopped:
        raise
    except Exception as exc:
        print(f"queueing an automatic compaction failed: {exc!r}", file=sys.stderr)
    else:
        if queued is not None:
            auto_queued.append(queued.id)


def withdraw_auto_queued(store: Store) -> None:
    """A stopped turn leaves no automatic compaction queued behind it."""
    for run_id in auto_queued:
        with contextlib.suppress(ValueError):  # it started (the stop reaches it) or is gone
            store.withdraw_queued(run_id)
    auto_queued.clear()


def hold_awake(store: Store, run_id: int, agent_id: str) -> Any:
    """keep_awake.start, which must never fail a turn (a busy database, an odd setting);
    only a stop goes through."""
    try:
        return keep_awake.start(store, run_id, agent_id, unset=TURN_ENV)
    except Stopped:
        raise
    except Exception:
        return None


def _drop_unstarted(store: Store, run_id: int, kind: str) -> bool:
    """Delete a notification run that never had a turn (it must not stay as a failed or
    stopped last run, nor be reported upward). True when it was one."""
    fresh = store.get_run(run_id)
    if kind == NOTIFICATION and fresh and fresh.started_at is None:
        store.delete_run(run_id)
        return True
    return False


if __name__ == "__main__":
    main(int(sys.argv[1]))
