"""Measure what an orchestrator agent loads and how large its first request is, per role.

Goes through the real launch path (the `Orchestrator` service API -> runner -> provider CLI)
in a throw-away ORCH_HOME with a throw-away git repository, one role at a time:

    uv run python scripts/measure_loadout.py --out loadout.json
    uv run python scripts/measure_loadout.py --roles worker,lead --model haiku

Per role it reads `$ORCH_HOME/logs/run-<id>/stream.jsonl`: the `system/init` event (tools,
MCP servers, skills, slash commands, plugins, agents, model), the `usage` of the FIRST
assistant message (first-request input = input + cache creation + cache read tokens) and
the `result` event (cost, turns). Each role costs one real model request.

Caveat: without `--mcp-config` Claude Code may emit `init` before the claude.ai connectors
have been fetched, so their servers/tools can be missing (or `pending`) in the counts; the
first-request input is the reliable size measure.
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
from collections import Counter
from pathlib import Path
from typing import Any

ROLES = ("orchestrator", "lead", "planner", "reviewer", "tester", "worker")
PROMPT = "Reply with the single word: ok"
TIERS = ("orchestrator", "lead", "worker")


def make_repo(path: Path) -> None:
    path.mkdir()
    (path / "README.md").write_text("# loadout probe\n", encoding="utf-8")
    git = ["git", "-c", "user.name=probe", "-c", "user.email=probe@example.invalid"]
    for args in (
        ["init", "-q", "-b", "main"],
        ["add", "README.md"],
        ["commit", "-q", "-m", "init"],
    ):
        subprocess.run([*git, *args], cwd=path, check=True)


def read_stream(path: Path) -> dict[str, Any]:
    """Loadout and first-request size from one run's raw stream."""
    init: dict[str, Any] | None = None
    first_usage: dict[str, Any] | None = None
    result: dict[str, Any] | None = None
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                data = json.loads(line)
            except json.JSONDecodeError:
                continue
            kind = data.get("type")
            if kind == "system" and data.get("subtype") == "init" and init is None:
                init = data
            elif kind == "assistant" and first_usage is None:
                first_usage = data.get("message", {}).get("usage")
            elif kind == "result":
                result = data
    out: dict[str, Any] = {"init_seen": init is not None}
    if init:
        tools = init.get("tools") or []
        servers = init.get("mcp_servers") or []
        out.update(
            model=init.get("model"),
            api_key_source=init.get("apiKeySource"),  # "none": subscription login
            tools=len(tools),
            mcp_tools=sum(1 for t in tools if str(t).startswith("mcp__")),
            builtin_tools=sorted(t for t in tools if not str(t).startswith("mcp__")),
            mcp_servers=len(servers),
            # Counts only: server names identify the account's connectors and systems.
            mcp_server_status=dict(
                Counter(s.get("status", "?") if isinstance(s, dict) else "?" for s in servers)
            ),
            orch_connected=any(
                isinstance(s, dict) and s.get("name") == "orch" and s.get("status") == "connected"
                for s in servers
            ),
            skills=len(init.get("skills") or []),
            slash_commands=len(init.get("slash_commands") or []),
            plugins=len(init.get("plugins") or []) if "plugins" in init else None,
            agents=len(init.get("agents") or []) if "agents" in init else None,
            # every other list in the init event, so new fields show up without a code change
            other_lists={
                key: len(value)
                for key, value in init.items()
                if isinstance(value, list)
                and key
                not in {"tools", "mcp_servers", "skills", "slash_commands", "plugins", "agents"}
            },
        )
    if first_usage:
        out["first_request_input"] = sum(
            int(first_usage.get(key) or 0)
            for key in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")
        )
        out["first_usage"] = {
            key: first_usage.get(key)
            for key in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")
        }
    if result:
        out.update(
            cost_usd=result.get("total_cost_usd"),
            turns=result.get("num_turns"),
            result_subtype=result.get("subtype"),
            is_error=result.get("is_error"),
        )
    return out


def measure(roles: list[str], model: str | None, timeout: float, keep: bool) -> dict[str, Any]:
    tmp = Path(tempfile.mkdtemp(prefix="orch-loadout-")).resolve()
    os.environ["ORCH_HOME"] = str(tmp / "home")
    # Imported after ORCH_HOME is set; nothing may touch the live home.
    from orchestrator import config
    from orchestrator.service import Orchestrator

    home = config.home().resolve()
    assert home.is_relative_to(tmp), f"ORCH_HOME {home} escapes {tmp}"
    repo = tmp / "repo"
    make_repo(repo)
    assert not (repo / "CLAUDE.md").exists()

    orch = Orchestrator()
    project = orch.add_project(str(repo), name="probe", default_provider="claude")
    tier_values = {"effort": "low", **({"model": model} if model else {})}
    orch.update_settings({tier: {"claude": tier_values} for tier in TIERS})

    measured: dict[str, Any] = {}
    for role in roles:
        started = time.monotonic()
        try:
            if role == "orchestrator":
                agent = orch.orchestrator()
                run = orch.send(agent.id, PROMPT)
            elif role == "lead":
                agent = orch.lead(project.name)
                run = orch.send(agent.id, PROMPT)
            else:
                agent, run = orch.spawn(
                    project.name, PROMPT, role=role, provider="claude", effort="low", model=model
                )
            idle, views = orch.wait([agent.id], timeout=timeout)
            view = views[0]
            entry: dict[str, Any] = {
                "agent": agent.id,
                "run": run.id,
                "status": view["status"] if idle else "timeout",
                "error": (view.get("last_run") or {}).get("error"),
                "reply": ((view.get("last_run") or {}).get("result") or "")[:80],
                "seconds": round(time.monotonic() - started, 1),
            }
            if not idle:
                orch.stop(agent.id)
            entry.update(read_stream(config.logs_dir() / f"run-{run.id}" / "stream.jsonl"))
        except Exception as exc:  # report and move on: no retries, no loops
            entry = {"status": "error", "error": f"{type(exc).__name__}: {exc}"}
        measured[role] = entry
        print(f"{role}: {entry.get('status')} {entry.get('error') or ''}".rstrip(), file=sys.stderr)

    info = {
        "claude_version": subprocess.run(
            ["claude", "--version"], capture_output=True, text=True
        ).stdout.strip(),
        "requested_model": model,
        "effort": "low",
        "prompt": PROMPT,
        "measured_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "roles": measured,
    }
    if keep:
        info["orch_home"] = str(home)
    else:
        cleanup(orch)
        shutil.rmtree(tmp, ignore_errors=True)
    return info


def cleanup(orch: Any) -> None:
    """Delete every agent with its worktree and branch, best effort (the temp dir goes
    anyway). Only archived agents are deleted: each is stopped and archived first, newest
    first so that sub-agents and agents sharing a worktree go before the ones above."""
    for agent in reversed(orch.store.list_agents()):
        with contextlib.suppress(Exception):
            orch.stop(agent.id)
            orch.archive(agent.id, confirm=True)
        with contextlib.suppress(Exception):
            orch.remove(agent.id, delete_branch=True)


COLUMNS = (
    ("role", "role"),
    ("status", "status"),
    ("tools", "tools"),
    ("mcp", "mcp_tools"),
    ("servers", "mcp_servers"),
    ("skills", "skills"),
    ("slash", "slash_commands"),
    ("plugins", "plugins"),
    ("agents", "agents"),
    ("1st input", "first_request_input"),
    ("cost $", "cost_usd"),
    ("turns", "turns"),
    ("model", "model"),
)


def table(info: dict[str, Any]) -> str:
    rows = [[title for title, _ in COLUMNS]]
    for role, entry in info["roles"].items():
        row = []
        for _, key in COLUMNS:
            value = role if key == "role" else entry.get(key)
            if isinstance(value, float):
                value = f"{value:.4f}"
            row.append("-" if value is None else str(value))
        rows.append(row)
    widths = [max(len(row[i]) for row in rows) for i in range(len(COLUMNS))]
    return "\n".join(
        "  ".join(cell.ljust(w) for cell, w in zip(row, widths, strict=True)) for row in rows
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--roles", default=",".join(ROLES), help="comma-separated roles")
    parser.add_argument("--model", default=None, help="model for every role (default: CLI's)")
    parser.add_argument("--out", type=Path, help="write the measurements as JSON")
    parser.add_argument("--timeout", type=float, default=300, help="seconds per role")
    parser.add_argument("--keep", action="store_true", help="keep the temporary ORCH_HOME")
    args = parser.parse_args()
    roles = [r.strip() for r in args.roles.split(",") if r.strip()]
    unknown = sorted(set(roles) - set(ROLES))
    if unknown:
        parser.error(f"unknown roles: {', '.join(unknown)}")
    info = measure(roles, args.model, args.timeout, args.keep)
    print(table(info))
    if args.out:
        args.out.write_text(json.dumps(info, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
