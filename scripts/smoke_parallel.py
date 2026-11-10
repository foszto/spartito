"""End-to-end check with the REAL Claude CLI: a busy lead hands a new instruction out at once.

Drives a project lead through the `Orchestrator` service API in a throw-away ORCH_HOME with
throw-away git repositories (nothing touches the live home); one fresh project per scenario:

1. independent work: the user asks for a worker that runs `sleep 60` and commits `a.txt`;
   once the lead's turn has ended and that worker runs, the user asks for `b.txt`. The
   lead's second turn must start and end while worker 1 runs, a second worker must start
   before worker 1 ends, both workers end `done`, the lead ends idle, then 20 s of quiet.
2. overlapping work: the same for `shared.txt` ("one"), then "append the line two". Records
   how the lead dispatches it (`spawn_agent`/`send_message` arguments from its stream):
   acceptable is the same worker via `send_message`, or a new worker with `after` and
   `base` on worker 1; not acceptable is a second worker started at once from the base.

    uv run python scripts/smoke_parallel.py --out parallel.json [--scenario 1|2] [--keep]

Costs a few short sonnet (lead) and haiku (worker) requests on the logged-in Claude account.
Exit code 0 only if every check passes.
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
from pathlib import Path
from typing import Any

from smoke_notify import POLL, Smoke, make_repo, ts

LEAD = {"claude": {"model": "sonnet", "effort": "low"}}
WORKER = {"claude": {"model": "haiku", "effort": "low"}}
SLEEP = "The worker must first run the shell command `sleep 60`."
SCENARIOS = {
    1: (
        "a.txt",
        f"Have a worker create the file `a.txt` containing the word alpha and commit it. {SLEEP}",
        "Also have a worker create the file `b.txt` containing the word beta and commit it.",
    ),
    2: (
        "shared.txt",
        "Have a worker create the file `shared.txt` containing the line one and commit it."
        f" {SLEEP}",
        "Also have a worker append the line two to `shared.txt` and commit it.",
    ),
}
FIRST_TIMEOUT = 240
SECOND_TIMEOUT = 300
WORKERS_TIMEOUT = 600
SETTLE_TIMEOUT = 600
QUIET_SECONDS = 20


class Parallel(Smoke):
    def dispatches(self, lead_id: str, runs: list[int]) -> list[dict[str, Any]]:
        """The lead's orch tool calls in these runs, with their full arguments."""
        from orchestrator import config

        calls = []
        for run_id in runs:
            path = config.run_dir(run_id) / "stream.jsonl"
            if not path.exists():
                continue
            for line in path.read_text(encoding="utf-8").splitlines():
                with contextlib.suppress(ValueError):
                    event = json.loads(line)
                    if event.get("type") != "assistant":
                        continue
                    for block in event.get("message", {}).get("content", []):
                        name = block.get("name", "")
                        if block.get("type") == "tool_use" and name.startswith("mcp__orch__"):
                            tool = name.removeprefix("mcp__orch__")
                            calls.append({"run": run_id, "tool": tool, "input": block["input"]})
        return calls

    def scenario(self, project: str, number: int, repo: Path) -> None:
        orch, store = self.orch, self.store
        filename, first, second = SCENARIOS[number]
        tag = f"s{number}"
        lead = orch.lead(project)
        facts = self.facts.setdefault(tag, {"lead": lead.id})
        run1 = orch.send(lead.id, first)
        self.log(f"{tag}: lead {lead.id} run {run1.id} queued")

        def first_worker() -> Any:
            children = sorted(self.workers(lead.id), key=lambda a: a.created_at)
            return children[0] if children else None

        self.wait_until(lambda: self.ended(run1.id), FIRST_TIMEOUT)
        w1 = first_worker()
        w1_runs = store.runs_of(w1.id) if w1 else []
        w1_run = w1_runs[0].id if w1_runs else None
        w1_active = bool(w1_run) and not self.ended(w1_run)
        facts.update(run1=run1.id, worker1=w1 and w1.id, worker1_run=w1_run)
        self.check(f"{tag} first lead turn ended", self.ended(run1.id), f"run {run1.id}")
        self.check(f"{tag} worker 1 running", w1_active, f"worker={w1 and w1.id} run={w1_run}")
        if not (w1 and w1_run and self.ended(run1.id)):
            return

        known = {a.id for a in self.workers(lead.id)}
        run2 = orch.send(lead.id, second)
        facts["run2"] = run2.id
        self.log(f"{tag}: second instruction run {run2.id} queued (worker 1 active: {w1_active})")
        self.wait_until(lambda: self.ended(run2.id), SECOND_TIMEOUT)
        r2 = self.run(run2.id)
        w1r = self.run(w1_run)
        self.log(f"{tag}: run {run2.id} {r2.status.value} at {r2.ended_at}")

        def new_workers() -> list[Any]:
            fresh = [a for a in self.workers(lead.id) if a.id not in known]
            return sorted(fresh, key=lambda a: a.created_at)

        r2_end, w1_end = ts(r2.ended_at), ts(w1r.ended_at)
        if number == 1:
            self.check(
                f"{tag} second lead turn ran while worker 1 ran",
                bool(r2.started_at and r2_end) and (w1_end is None or r2_end <= w1_end),
                f"run {run2.id} {r2.status.value} started={r2.started_at} ended={r2.ended_at};"
                f" worker 1 run {w1_run} ended={w1r.ended_at}",
            )

        # All agents of the lead finish; the lead settles; then quiet.
        def all_ended() -> bool:
            return not any(
                r.status.active for a in self.workers(lead.id) for r in store.runs_of(a.id)
            )

        self.wait_until(all_ended, WORKERS_TIMEOUT)
        settled = self.wait_until(lambda: not store.subtree_busy(lead.id), SETTLE_TIMEOUT)
        snapshot = {run.id for _, run in self.all_runs()}
        if settled:
            time.sleep(QUIET_SECONDS)
        later = [run.id for _, run in self.all_runs() if run.id not in snapshot]
        w1r = self.run(w1_run)
        w1_end = ts(w1r.ended_at)
        fresh = new_workers()
        lead_runs = store.runs_of(lead.id)
        meta = store.meta_of(lead.id)
        facts["lead_runs"] = [
            {
                "run": r.id,
                "kind": meta[r.id].kind if r.id in meta else None,
                "status": r.status.value,
            }
            for r in lead_runs
        ]
        facts["dispatches"] = self.dispatches(lead.id, [r.id for r in lead_runs])
        facts["workers"] = [
            {
                "agent": a.id,
                "branch": a.branch,
                "base_ref": a.base_ref,
                "runs": [
                    {
                        "run": r.id,
                        "status": r.status.value,
                        "depends_on": r.depends_on,
                        "started": r.started_at,
                        "ended": r.ended_at,
                    }
                    for r in store.runs_of(a.id)
                ],
                filename: self.show(repo, a.branch, filename),
                "b.txt": self.show(repo, a.branch, "b.txt") if number == 1 else None,
            }
            for a in sorted(self.workers(lead.id), key=lambda a: a.created_at)
        ]

        if number == 1:
            w2 = fresh[0] if fresh else None
            w2_runs = store.runs_of(w2.id) if w2 else []
            w2_start = ts(w2_runs[0].started_at) if w2_runs else None
            self.check(
                f"{tag} second worker started before worker 1 ended",
                bool(w2_start and w1_end and w2_start < w1_end),
                f"worker 2={w2 and w2.id} run={w2_runs and w2_runs[0].id}"
                f" started={w2_runs and w2_runs[0].started_at}; worker 1 ended={w1r.ended_at}",
            )
            for label, agent in (("1", w1), ("2", w2)):
                runs = store.runs_of(agent.id) if agent else []
                self.check(
                    f"{tag} worker {label} done",
                    bool(runs) and all(r.status.value == "done" for r in runs),
                    f"{agent and agent.id}: {[(r.id, r.status.value) for r in runs]}",
                )
        else:
            facts["verdict"] = self.judge(w1, fresh, facts["dispatches"], run2.id)
            self.check(
                f"{tag} overlapping work not run in parallel from different bases",
                facts["verdict"].startswith("acceptable"),
                facts["verdict"],
            )

        last = lead_runs[-1] if lead_runs else None
        self.check(
            f"{tag} lead idle after its notification turn(s)",
            settled and bool(last) and last.status.value == "done",
            f"settled={settled} runs={[(r['run'], r['kind'], r['status']) for r in facts['lead_runs']]}",
        )
        self.check(f"{tag} no further runs", settled and not later, f"in {QUIET_SECONDS}s: {later}")

    @staticmethod
    def show(repo: Path, branch: str | None, filename: str) -> str | None:
        if not branch:
            return None
        out = subprocess.run(
            ["git", "show", f"{branch}:{filename}"], cwd=repo, capture_output=True, text=True
        )
        return out.stdout if out.returncode == 0 else None

    @staticmethod
    def judge(w1: Any, fresh: list[Any], calls: list[dict[str, Any]], run2: int) -> str:
        sent = [
            c for c in calls if c["tool"] == "send_message" and c["input"].get("agent_id") == w1.id
        ]
        if not fresh and sent:
            return f"acceptable: sent to worker 1 with send_message {sent[0]['input']}"
        if not fresh:
            return "other: no second worker and no message to worker 1"
        w2 = fresh[0]
        spawn = next((c for c in calls if c["tool"] == "spawn_agent" and c["run"] >= run2), None)
        args = spawn["input"] if spawn else {}
        after = args.get("after") or []
        chained = w1.id in after
        on_w1 = w2.base_ref == w1.branch
        if chained and on_w1:
            return f"acceptable: new worker {w2.id} after and based on worker 1: {args}"
        if not chained and not on_w1:
            return f"not acceptable: new worker {w2.id} at once from {w2.base_ref}: {args}"
        return f"other: new worker {w2.id} after={after} base_ref={w2.base_ref}: {args}"


def print_report(info: dict[str, Any]) -> None:
    print("\nTIMELINE")
    for row in info["timeline"]:
        print(
            f"#{row['run']} {row['agent']} [{row['kind']}, sender={row['sender']}] {row['status']}"
            f" started={row['started_at']} ended={row['ended_at']}"
            f"\n    prompt: {row['prompt']}\n    result: {row['result']}"
            + (f"\n    error: {row['error']}" if row["error"] else "")
            + f"\n    tokens: {row['tokens']} cost=${row['cost_usd']} model={row['model']}"
        )
        for event in row["events"]:
            print(f"      · {event}")
    for tag, facts in info["facts"].items():
        print(f"\nDISPATCHES {tag}")
        for call in facts.get("dispatches", []):
            print(f"  run {call['run']} {call['tool']}: {json.dumps(call['input'])}")
        for worker in facts.get("workers", []):
            print(f"  {json.dumps(worker)}")
    print("\nCHECKS")
    for item in info["checks"]:
        print(f"{'PASS' if item['pass'] else 'FAIL'}  {item['check']}: {item['detail']}")
    totals = info["totals"]
    print(f"\nTOTAL tokens={totals['tokens']} cost=${totals['cost_usd']:.4f}")
    print(f"VERDICT: {'PASS' if info['passed'] else 'FAIL'}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--out", type=Path, help="write the report as JSON")
    parser.add_argument("--scenario", type=int, choices=sorted(SCENARIOS), action="append")
    parser.add_argument("--keep", action="store_true", help="keep the temporary ORCH_HOME")
    args = parser.parse_args()

    tmp = Path(tempfile.mkdtemp(prefix="orch-smoke-parallel-")).resolve()
    os.environ["ORCH_HOME"] = str(tmp / "home")  # before anything opens a store
    os.environ["ORCH_NOTIFY_DELAY"] = "1"  # notify.SETTLE_ENV
    os.environ.pop("ORCH_AGENT_ID", None)  # we act as the user
    from orchestrator import config
    from orchestrator.service import Orchestrator

    home = config.home().resolve()
    assert home.is_relative_to(tmp), f"ORCH_HOME {home} escapes {tmp}"
    orch = Orchestrator()
    assert config.db_path().resolve().is_relative_to(tmp)
    orch.update_settings({"lead": LEAD, "worker": WORKER})

    smoke = Parallel(orch, orch.store)
    started = time.monotonic()
    for number in args.scenario or sorted(SCENARIOS):
        repo = tmp / f"repo{number}"
        make_repo(repo)
        project = orch.add_project(str(repo), name=f"par{number}", default_provider="claude")
        try:
            smoke.scenario(project.name, number, repo)
        except Exception as exc:  # report, do not retry
            smoke.check(f"s{number} ran", False, f"{type(exc).__name__}: {exc}")
        finally:
            for agent in orch.store.list_agents():  # no claude process outlives the test
                if any(run.status.active for run in orch.store.runs_of(agent.id)):
                    with contextlib.suppress(Exception):
                        orch.stop(agent.id)
            time.sleep(POLL)

    timeline = smoke.timeline()
    token_total: dict[str, int] = {}
    for row in timeline:
        for key, value in (row["tokens"] or {}).items():
            token_total[key] = token_total.get(key, 0) + value
    info = {
        "claude_version": subprocess.run(
            ["claude", "--version"], capture_output=True, text=True
        ).stdout.strip(),
        "settings": {"lead": LEAD, "worker": WORKER},
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
