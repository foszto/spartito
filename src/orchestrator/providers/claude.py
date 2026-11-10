"""Claude Code in headless mode: `claude -p --output-format stream-json`."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
import uuid

from ..loadout import Loadout
from ..models import Agent, Event, Usage
from .base import NOTIFICATION_RESULT, Invocation, RunContext, brief

# Set by a parent Claude Code session; a child must not believe it is that session.
PARENT_SESSION_ENV = [
    "CLAUDECODE",
    "CLAUDE_CODE_ENTRYPOINT",
    "CLAUDE_CODE_SESSION_ID",
    "CLAUDE_CODE_CHILD_SESSION",
    "CLAUDE_CODE_SESSION_ATTENDED",
    "CLAUDE_CODE_MESSAGING_SOCKET",
    "CLAUDE_CODE_MESSAGING_TOKEN",
    "CLAUDE_PID",
    "CLAUDE_EFFORT",
]

DEFAULT_PERMISSION = "auto"

# Removes `run_in_background` from the Bash tool and keeps commands in the foreground
# (verified with Claude Code 2.1.322). Set for leads: a background command keeps the CLI
# alive after its answer, and a lead must stay reachable between turns.
NO_BACKGROUND_ENV = {"CLAUDE_CODE_DISABLE_BACKGROUND_TASKS": "1"}


# The picker's fallback when the installed CLI cannot say what it offers.
FALLBACK_MODELS = ("fable", "opus", "sonnet", "haiku")
MODEL_LIST_TIMEOUT = 20
# A fallback or generic list is asked for again after this long; a full one is kept.
MODEL_LIST_RETRY_SECONDS = 600
UNAVAILABLE_NOTE = "model list unavailable: showing aliases"
GENERIC_NOTE = "model list may be incomplete"
_INIT_ID = "orch-models"


def request_model_list() -> str:
    """The CLI's answer to an SDK `initialize` control request (stdout, stream-json).

    The answer lists the models of the account like the `/model` picker. No user message is
    sent, so no model request is made; closing stdin ends the CLI after its answer. No user
    settings (hooks, plugins) or MCP servers are loaded and no session is saved.
    """
    request = {
        "type": "control_request",
        "request_id": _INIT_ID,
        "request": {"subtype": "initialize"},
    }
    argv = ["claude", "-p", "--input-format", "stream-json", "--output-format", "stream-json"]
    argv += ["--verbose", "--strict-mcp-config", "--setting-sources", "project,local"]
    argv += ["--no-session-persistence"]
    env = {k: v for k, v in os.environ.items() if k not in PARENT_SESSION_ENV}
    with tempfile.TemporaryDirectory(prefix="orch-models-") as cwd:
        proc = subprocess.Popen(
            argv,
            cwd=cwd,
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
        try:
            out, err = proc.communicate(json.dumps(request) + "\n", timeout=MODEL_LIST_TIMEOUT)
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, signal.SIGKILL)
            proc.communicate()
            raise
    if proc.returncode:
        raise subprocess.SubprocessError(f"exit {proc.returncode}: {brief(err, 200)}")
    return out


def parse_model_list(output: str) -> list[tuple[str, str]]:
    """(value, label) pairs from the `initialize` answer; the label carries the version."""
    return [_model_choice(entry) for entry in model_entries(output)]


def model_entries(output: str) -> list[dict]:
    """The model entries of the `initialize` answer (each with a string `value`)."""
    for line in output.splitlines():
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(data, dict) or data.get("type") != "control_response":
            continue
        response = data.get("response")
        if not isinstance(response, dict):
            continue
        if response.get("request_id") != _INIT_ID:
            continue
        if response.get("subtype") != "success":
            raise ValueError(f"initialize failed: {brief(response.get('error', response), 200)}")
        answer = response.get("response")
        entries = answer.get("models") if isinstance(answer, dict) else None
        if not isinstance(entries, list):
            raise ValueError("initialize answer has no model list")
        models = [m for m in entries if isinstance(m, dict) and _text(m.get("value"))]
        if not models:
            raise ValueError("initialize answer lists no models")
        return models
    raise ValueError("no initialize answer")


def looks_generic(entries: list[dict]) -> bool:
    """The short list the CLI gives before it knows the account: no full model ids, or
    names without a version (seen: "Opus" for `opus`, "Fable" for `claude-fable-5-1[1m]`)."""
    others = [entry for entry in entries if entry["value"] != "default"]
    full_ids = any(entry["value"].startswith("claude-") for entry in others)
    unversioned = any(not _has_digit(_display_name(entry)) for entry in others)
    return not full_ids or unversioned


def _text(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""


def _has_digit(text: str) -> bool:
    return any(c.isdigit() for c in text)


def _display_name(entry: dict) -> str:
    return _text(entry.get("displayName")) or entry["value"]


def _model_choice(entry: dict) -> tuple[str, str]:
    value, label, resolved = entry["value"], _display_name(entry), _text(entry.get("resolvedModel"))
    if value == "default":
        # Not the picker's "inherit": an own `default` runs the CLI's default, whatever that
        # is at the time; the model named here is the one it stood for at lookup.
        return value, f"Claude Code default (now {resolved})" if resolved else "Claude Code default"
    if not _has_digit(label) and resolved and resolved != label:
        label = f"{label} · {resolved}"  # a name without a version: add the model it stands for
    return value, label


def loadout_flags(loadout: Loadout) -> list[str]:
    """CLI flags for a loadout. Needed on every turn: `--resume` does not remember them."""
    flags = []
    if loadout.tools is not None:
        flags += ["--tools", ",".join(loadout.tools)]
    if loadout.strict_mcp:
        flags.append("--strict-mcp-config")
    if loadout.setting_sources is not None:
        flags += ["--setting-sources", ",".join(loadout.setting_sources)]
    if not loadout.slash_commands:
        flags.append("--disable-slash-commands")
    return flags


class ClaudeProvider:
    name = "claude"
    efforts = ("low", "medium", "high", "xhigh", "max")

    models_note: str | None = None  # set when the list is the fallback or looks generic
    _models: list[tuple[str, str]] | list[str] | None = None
    _models_at = 0.0  # time.monotonic() of the lookup
    _models_lock = threading.Lock()

    def models(self) -> list[tuple[str, str]] | list[str]:
        """What Claude Code's own `/model` picker offers, as (value for --model, label).

        Asked from the installed CLI once per process; the aliases when that fails. A
        fallback or generic answer is asked for again once it is MODEL_LIST_RETRY_SECONDS old.
        """
        with self._models_lock:
            age = time.monotonic() - self._models_at
            if self._models is None or (self.models_note and age >= MODEL_LIST_RETRY_SECONDS):
                self._models_at = time.monotonic()
                try:
                    entries = model_entries(request_model_list())
                except (OSError, subprocess.SubprocessError, ValueError) as exc:
                    print(
                        f"claude model list unavailable ({exc}); showing aliases", file=sys.stderr
                    )
                    self._models = list(FALLBACK_MODELS)
                    self.models_note = UNAVAILABLE_NOTE
                else:
                    self._models = [_model_choice(entry) for entry in entries]
                    self.models_note = GENERIC_NOTE if looks_generic(entries) else None
            return self._models

    def invocation(self, ctx: RunContext) -> Invocation:
        agent = ctx.agent
        argv = ["claude", "-p", "--output-format", "stream-json", "--verbose"]
        if agent.session_id:
            argv += ["--resume", agent.session_id]
        else:
            argv += ["--session-id", str(uuid.uuid4()), "--name", agent.id]
        argv += ["--permission-mode", agent.permission or DEFAULT_PERMISSION]
        argv += loadout_flags(ctx.loadout)
        if ctx.model:
            argv += ["--model", ctx.model]
        if ctx.effort:
            argv += ["--effort", ctx.effort]
        if ctx.system_prompt:
            argv += ["--append-system-prompt", ctx.system_prompt]
        if ctx.mcp:
            servers = {
                "orch": {"command": ctx.mcp.command, "args": ctx.mcp.args, "env": ctx.mcp.env}
            }
            argv += ["--mcp-config", json.dumps({"mcpServers": servers})]
            argv += ["--allowedTools", "mcp__orch"]
        if ctx.output_schema:
            argv += ["--json-schema", json.dumps(ctx.output_schema)]
        return Invocation(
            argv=argv,
            stdin=ctx.prompt,
            env=dict(NO_BACKGROUND_ENV) if ctx.lead else {},
            unset_env=PARENT_SESSION_ENV,
        )

    def parse(self, line: str) -> list[Event]:
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            return [Event("info", line.strip())] if line.strip() else []
        kind = data.get("type")
        if kind == "system" and data.get("subtype") == "init":
            return [Event("session", session_id=data.get("session_id"), model=data.get("model"))]
        if kind == "assistant":
            return self._content(data)
        if kind == "user":
            return self._tool_results(data)
        if kind == "result":
            failed = bool(data.get("is_error")) or data.get("subtype") != "success"
            text = data.get("result") or ""
            if failed and not text:
                text = "; ".join(data.get("errors") or []) or data.get("subtype", "error")
            usage = self._usage(data)
            origin = data.get("origin")
            # A turn the CLI took for a background task notification (after its answer, or
            # on resume for a task the previous process left behind) has an origin.
            by_notification = isinstance(origin, dict) and origin.get("kind") == "task-notification"
            return [
                Event(
                    NOTIFICATION_RESULT if by_notification else "result",
                    text,
                    session_id=data.get("session_id"),
                    cost_usd=usage.cost_usd if usage else data.get("total_cost_usd"),
                    is_error=failed,
                    structured=data.get("structured_output"),
                    usage=usage,
                )
            ]
        return []

    @staticmethod
    def _usage(data: dict) -> Usage | None:
        """Tokens and cost of this invocation, summed over every model it used.

        On a resumed session `total_cost_usd` and `modelUsage` are totals for the whole
        session, while `model_usage_since_last_result` covers only this invocation (verified
        with Claude Code 2.1.322). `usage` is per invocation too but counts only the main
        model, not subagents or auxiliary models, so it would not match the cost.
        """
        per_model = data.get("model_usage_since_last_result")
        cumulative = not isinstance(per_model, dict)
        if cumulative:  # older CLIs: only the session totals
            per_model = data.get("modelUsage")
        if not isinstance(per_model, dict):
            cost = data.get("total_cost_usd")
            return None if cost is None else Usage(cost_usd=cost, cumulative=True)
        entries = [entry for entry in per_model.values() if isinstance(entry, dict)]

        def total(key: str) -> int:
            return sum(int(entry.get(key) or 0) for entry in entries)

        costs = [entry["costUSD"] for entry in entries if entry.get("costUSD") is not None]
        if not costs and cumulative:
            costs = [data["total_cost_usd"]] if data.get("total_cost_usd") is not None else []
        return Usage(
            input_tokens=total("inputTokens"),
            output_tokens=total("outputTokens"),
            cache_read_tokens=total("cacheReadInputTokens"),
            cache_creation_tokens=total("cacheCreationInputTokens"),
            cost_usd=sum(costs) if costs else None,
            model=next(iter(per_model)) if len(per_model) == 1 else None,
            cumulative=cumulative,
        )

    @staticmethod
    def _content(data: dict) -> list[Event]:
        events = []
        prefix = "[subagent] " if data.get("parent_tool_use_id") else ""
        for block in data.get("message", {}).get("content", []):
            if block.get("type") == "text" and block.get("text", "").strip():
                events.append(Event("text", prefix + block["text"]))
            elif block.get("type") == "tool_use":
                events.append(
                    Event("tool", f"{prefix}{block.get('name')} {brief(block.get('input'))}")
                )
        return events

    @staticmethod
    def _tool_results(data: dict) -> list[Event]:
        events = []
        content = data.get("message", {}).get("content", [])
        for block in content if isinstance(content, list) else []:
            if block.get("type") == "tool_result" and block.get("is_error"):
                events.append(Event("tool_result", "error: " + brief(block.get("content"))))
        return events

    def attach_command(self, agent: Agent) -> list[str]:
        return ["claude", "--resume", agent.session_id or ""]
