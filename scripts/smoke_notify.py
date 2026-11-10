"""End-to-end smoke test of parent notifications with the REAL Claude CLI.

Drives a project lead and one worker through the `Orchestrator` service API in a throw-away
ORCH_HOME with a throw-away git repository (nothing touches the live home):

1. the user asks the lead to start one worker that runs `sleep 45`; the lead should end its
   turn while the worker still runs (or sit in `wait_for_agents`, which is recorded; step 1
   then ends as soon as that is seen, so step 2 happens while the worker runs);
2. while the worker runs, the user sends the lead "pong": that run must end before the
   worker does (the lead is idle, or its `wait_for_agents` returns early);
3. once the worker ends, exactly one `[orch notification]` run is queued on the lead, it
   finishes `done`, names the worker, and nothing else runs within 20 s afterwards.

    uv run python scripts/smoke_notify.py --out smoke.json [--keep]

Costs about four short haiku requests on the logged-in Claude account. Exit code 0 only if
every check passes.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path
from typing import Any

MODEL = "haiku"
EFFORT = "low"
STEP1 = (
    "Start one worker agent with the task: run the shell command `sleep 45`, then reply with"
    " the single word done. Then do what your instructions say about waiting."
)
STEP2 = "Reply with the single word: pong"
STEP1_TIMEOUT = 120
STEP2_TIMEOUT = 180
WORKER_TIMEOUT = 300
SETTLE_TIMEOUT = 300
QUIET_SECONDS = 20
POLL = 0.5
PREVIEW = 150


def make_repo(path: Path) -> None:
    path.mkdir()
    (path / "README.md").write_text("# notify smoke test\n", encoding="utf-8")
    git = ["git", "-c", "user.name=smoke", "-c", "user.email=smoke@example.invalid"]
    for args in (
        ["init", "-q", "-b", "main"],
        ["add", "README.md"],
        ["commit", "-q", "-m", "init"],
    ):
        subprocess.run([*git, *args], cwd=path, check=True)


def ts(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


def clip(text: str | None) -> str | None:
    if text is None:
        return None
    text = " ".join(text.split())
    return text[:PREVIEW] + ("…" if len(text) > PREVIEW else "")


class Smoke:
    def __init__(self, orch: Any, store: Any) -> None:
        self.orch = orch
        self.store = store
        self.checks: list[dict[str, Any]] = []
        self.facts: dict[str, Any] = {}

    def check(self, name: str, ok: bool, detail: str) -> None:
        self.checks.append({"check": name, "pass": bool(ok), "detail": detail})
        print(f"{'PASS' if ok else 'FAIL'}  {name}: {detail}", file=sys.stderr, flush=True)

    def log(self, text: str) -> None:
        print(f"[{time.strftime('%H:%M:%S')}] {text}", file=sys.stderr, flush=True)

    def wait_until(self, condition: Any, timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if condition():
                return True
            time.sleep(POLL)
        return condition()

    def run(self, run_id: int) -> Any:
        return self.store.get_run(run_id)

    def ended(self, run_id: int) -> bool:
        run = self.run(run_id)
        return run is None or not run.status.active

    def workers(self, lead_id: str) -> list[Any]:
        return [a for a in self.store.list_agents() if a.parent_id == lead_id]

    def all_runs(self) -> list[tuple[Any, Any]]:
        runs = []
        for agent in self.store.list_agents():
            runs += [(agent, run) for run in self.store.runs_of(agent.id)]
        return sorted(runs, key=lambda pair: pair[1].id)

    def tool_calls(self, agent_id: str, run_id: int) -> list[str]:
        return [
            clip(f"{e.kind}: {e.text}") or ""
            for e in self.store.events(agent_id)
            if e.run_id == run_id and e.kind not in ("text", "result")
        ]

    def timeline(self) -> list[dict[str, Any]]:
        rows = []
        for agent, run in self.all_runs():
            meta = self.store.meta(run.id)
            usage = self.store.get_usage(run.id)
            rows.append(
                {
                    "run": run.id,
                    "agent": agent.id,
                    "role": agent.role,
                    "kind": meta.kind if meta.known else "legacy",
                    "sender": meta.sender,
                    "created_at": run.created_at,
                    "started_at": run.started_at,
                    "ended_at": run.ended_at,
                    "status": run.status.value,
                    "prompt": clip(run.prompt),
                    "result": clip(run.result),
                    "error": clip(run.error),
                    "tokens": usage.tokens() if usage else None,
                    "cost_usd": usage.cost_usd if usage else None,
                    "model": usage.model if usage else None,
                    "events": self.tool_calls(agent.id, run.id),
                }
            )
        return rows

    # -- the scenario -----------------------------------------------------

    def scenario(self, project: str) -> None:
        orch, store = self.orch, self.store
        lead = orch.lead(project)
        self.facts["lead"] = lead.id
        run1 = orch.send(lead.id, STEP1)
        self.facts["run1"] = run1.id
        self.log(f"step 1: lead {lead.id} run {run1.id} queued")

        # Step 1: until the lead's first run ends or 120 s pass.
        worker_run: dict[str, Any] = {}

        def watch_worker() -> None:
            if worker_run:
                return
            for agent in self.workers(lead.id):
                runs = store.runs_of(agent.id)
                if runs:
                    worker_run.update(agent=agent.id, run=runs[0].id)
                    self.log(f"worker {agent.id} run {runs[0].id} queued")

        def lead_waiting() -> bool:  # sitting in wait_for_agents while the worker runs
            calls = self.tool_calls(lead.id, run1.id)
            return (
                bool(worker_run)
                and not self.ended(worker_run["run"])
                and any("wait_for_agents" in call for call in calls)
            )

        def step1_done() -> bool:
            watch_worker()
            return self.ended(run1.id) or lead_waiting()

        self.wait_until(step1_done, STEP1_TIMEOUT)
        watch_worker()
        lead_ended = self.ended(run1.id)
        worker_active = bool(worker_run) and not self.ended(worker_run["run"])
        if lead_ended and worker_active:
            path = "ended turn while worker ran (intended)"
        elif lead_ended:
            path = "ended turn, but worker not running (none started or already finished)"
        elif worker_active and lead_waiting():
            path = "sat in wait_for_agents while worker ran"
        elif worker_active:
            path = "still in its first turn after 120 s, not in wait_for_agents"
        else:
            path = "still in its first turn after 120 s, no active worker"
        lead_calls = self.tool_calls(lead.id, run1.id)
        waited = any("wait_for_agents" in call for call in lead_calls)
        self.facts.update(step1_path=path, step1_lead_called_wait=waited, worker=worker_run)
        self.log(f"step 1: {path}; wait_for_agents called: {waited}")
        self.check("step1 worker spawned", bool(worker_run), f"worker={worker_run or None}")
        self.check(
            "step1 lead released its turn (ended or in wait_for_agents) while worker ran",
            worker_active and (lead_ended or lead_waiting()),
            path,
        )
        if not worker_run:
            return

        # Step 2: a user message while the worker still runs.
        wid, wrun = worker_run["agent"], worker_run["run"]
        before = not self.ended(wrun)
        run2 = orch.send(lead.id, STEP2)
        self.facts["run2"] = run2.id
        self.log(f"step 2: pong run {run2.id} queued (worker active: {before})")
        self.wait_until(lambda: self.ended(run2.id) or self.ended(wrun), STEP2_TIMEOUT)
        self.wait_until(lambda: self.ended(run2.id), STEP2_TIMEOUT)
        r2, w = self.run(run2.id), self.run(wrun)
        r2_end, w_end = ts(r2.ended_at) if r2 else None, ts(w.ended_at) if w else None
        self.check("step2 sent while worker active", before, f"worker run {wrun} active={before}")
        self.check(
            "step2 pong ended before worker",
            bool(r2_end) and (w_end is None or r2_end <= w_end),
            f"pong run {run2.id} status={r2 and r2.status.value} started={r2 and r2.started_at}"
            f" ended={r2 and r2.ended_at}; worker ended={w and w.ended_at}",
        )
        self.check(
            "step2 pong answered",
            bool(r2 and r2.status.value == "done" and "pong" in (r2.result or "").lower()),
            f"result={clip(r2.result) if r2 else None}",
        )
        r1 = self.run(run1.id)
        self.facts["run1_end_vs_pong_start"] = {
            "run1_ended": r1 and r1.ended_at,
            "pong_started": r2 and r2.started_at,
        }

        # Step 3: worker end, then the lead's subtree settles; no runs are queued by us.
        self.wait_until(
            lambda: not any(r.status.active for r in store.runs_of(wid)), WORKER_TIMEOUT
        )
        w = self.run(wrun)
        self.log(f"step 3: worker run {wrun} {w.status.value} at {w.ended_at}")
        self.check("step3 worker done", w.status.value == "done", f"status={w.status.value}")
        settled = self.wait_until(lambda: not store.subtree_busy(lead.id), SETTLE_TIMEOUT)
        self.log(f"step 3: lead settled={settled}")
        self.check("step3 lead settled", settled, f"within {SETTLE_TIMEOUT}s")
        snapshot = [run.id for _, run in self.all_runs()]
        time.sleep(QUIET_SECONDS)
        later = [run.id for _, run in self.all_runs() if run.id not in snapshot]
        self.check("step3 no further runs", not later, f"new runs in {QUIET_SECONDS}s: {later}")

        meta = store.meta_of(lead.id)
        notes = [
            r
            for r in store.runs_of(lead.id)
            if meta.get(r.id) and meta[r.id].kind == "notification"
        ]
        self.facts["notification_runs"] = [r.id for r in notes]
        self.check(
            "step3 exactly one notification", len(notes) == 1, f"runs={[r.id for r in notes]}"
        )
        if notes:
            note = notes[0]
            w_end = ts(w.ended_at)
            started = ts(note.started_at)
            self.check(
                "step3 notification started after worker ended",
                bool(started and w_end and started >= w_end),
                f"notification started={note.started_at} worker ended={w.ended_at}",
            )
            self.check(
                "step3 notification done",
                note.status.value == "done",
                f"status={note.status.value}",
            )
            self.check(
                "step3 notification prompt",
                note.prompt.startswith("[orch notification]") and wid in note.prompt,
                f"prompt={clip(note.prompt)}",
            )
            self.facts["notification_result"] = clip(note.result)


def print_report(info: dict[str, Any]) -> None:
    print("\nTIMELINE")
    for row in info["timeline"]:
        tokens = row["tokens"] or {}
        print(
            f"#{row['run']} {row['agent']} [{row['kind']}, sender={row['sender']}] {row['status']}"
            f"\n    created={row['created_at']} started={row['started_at']} ended={row['ended_at']}"
            f"\n    prompt: {row['prompt']}\n    result: {row['result']}"
            + (f"\n    error: {row['error']}" if row["error"] else "")
            + f"\n    tokens: {tokens} cost=${row['cost_usd']} model={row['model']}"
        )
        for event in row["events"]:
            print(f"      · {event}")
    print("\nCHECKS")
    for item in info["checks"]:
        print(f"{'PASS' if item['pass'] else 'FAIL'}  {item['check']}: {item['detail']}")
    totals = info["totals"]
    print(f"\nTOTAL tokens={totals['tokens']} cost=${totals['cost_usd']:.4f}")
    print(f"step 1 path: {info['facts'].get('step1_path')}")
    print(f"VERDICT: {'PASS' if info['passed'] else 'FAIL'}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--out", type=Path, help="write the report as JSON")
    parser.add_argument("--keep", action="store_true", help="keep the temporary ORCH_HOME")
    args = parser.parse_args()

    tmp = Path(tempfile.mkdtemp(prefix="orch-smoke-notify-")).resolve()
    os.environ["ORCH_HOME"] = str(tmp / "home")  # before anything opens a store
    os.environ["ORCH_NOTIFY_DELAY"] = "1"  # notify.SETTLE_ENV
    os.environ.pop("ORCH_AGENT_ID", None)  # we act as the user
    from orchestrator import config
    from orchestrator.service import Orchestrator

    home = config.home().resolve()
    assert home.is_relative_to(tmp), f"ORCH_HOME {home} escapes {tmp}"
    repo = tmp / "repo"
    make_repo(repo)

    orch = Orchestrator()
    assert config.db_path().resolve().is_relative_to(tmp)
    project = orch.add_project(str(repo), name="smoke", default_provider="claude")
    tier = {"claude": {"model": MODEL, "effort": EFFORT}}
    orch.update_settings({"lead": tier, "worker": tier})

    smoke = Smoke(orch, orch.store)
    started = time.monotonic()
    try:
        smoke.scenario(project.name)
    except Exception as exc:  # report, do not retry
        smoke.check("scenario ran", False, f"{type(exc).__name__}: {exc}")
    finally:
        for agent in orch.store.list_agents():  # no claude process outlives the test
            if any(run.status.active for run in orch.store.runs_of(agent.id)):
                with contextlib.suppress(Exception):
                    orch.stop(agent.id)

    timeline = smoke.timeline()
    token_total: dict[str, int] = {}
    for row in timeline:
        for key, value in (row["tokens"] or {}).items():
            token_total[key] = token_total.get(key, 0) + value
    info = {
        "claude_version": subprocess.run(
            ["claude", "--version"], capture_output=True, text=True
        ).stdout.strip(),
        "model": MODEL,
        "effort": EFFORT,
        "notify_delay": os.environ["ORCH_NOTIFY_DELAY"],
        "seconds": round(time.monotonic() - started, 1),
        "timeline": timeline,
        "checks": smoke.checks,
        "facts": smoke.facts,
        "totals": {
            "tokens": token_total,
            "cost_usd": sum(row["cost_usd"] or 0 for row in timeline),
        },
        "passed": bool(smoke.checks) and all(item["pass"] for item in smoke.checks),
    }
    if args.keep:
        info["orch_home"] = str(home)
    print_report(info)
    if args.out:
        args.out.write_text(json.dumps(info, indent=2) + "\n", encoding="utf-8")
    if args.keep:
        print(f"kept {tmp}", file=sys.stderr)
    else:
        for agent in orch.store.list_agents():
            with contextlib.suppress(Exception):
                orch.remove(agent.id, delete_branch=True)
        shutil.rmtree(tmp, ignore_errors=True)
    return 0 if info["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
