"""Claude Code's dialect: `claude -p --output-format stream-json` (providers/claude.py).

- Records an `--mcp-config` file as `config_path`, `config` (parsed), `mode`, `dir_mode`.
- Runs the SessionStart hooks of `--settings` (the memory canary) before it reads stdin.
- Reads the prompt as one stream-json user message with `--input-format stream-json`.
- Reports a `system`/`init` line whose `mcp_servers` lists every server of its MCP
  configuration as `connected`, changed by FAKE_MCP_STATUS (JSON {"<name>": "<status>"};
  null leaves a server out); FAKE_MCP_STATUS=none: no `mcp_servers`.
"""

from __future__ import annotations

import json
import os
import subprocess

from fakecli import private_file

SESSION = "fake-claude-session"
MODEL = "fake-model-1"


class Dialect:
    def __init__(self, argv: list[str]) -> None:
        self.argv = argv
        self.servers: dict = {}

    def option(self, name: str) -> str | None:
        return self.argv[self.argv.index(name) + 1] if name in self.argv else None

    def record(self, seen: dict) -> None:
        config = self.option("--mcp-config")
        if config is not None and config.startswith("{"):
            self.servers = json.loads(config)["mcpServers"]
        elif config is not None:
            file = private_file(config)
            seen["config_path"] = config
            seen["config"] = file["content"]
            seen["mode"] = file["mode"]
            seen["dir_mode"] = file["dir_mode"]
            self.servers = seen["config"]["mcpServers"]

    def before_input(self) -> None:
        settings = self.option("--settings")
        if settings:  # its SessionStart hooks: the memory canary
            for group in json.loads(settings)["hooks"].get("SessionStart", []):
                for hook in group["hooks"]:
                    subprocess.run(hook["command"], shell=True, check=True)

    def prompt(self, raw: str) -> str:
        if self.option("--input-format") == "stream-json":
            return json.loads(raw.splitlines()[0])["message"]["content"][0]["text"]
        return raw

    def session(self) -> list[dict]:
        init = {"type": "system", "subtype": "init", "session_id": SESSION, "model": MODEL}
        if os.environ.get("FAKE_MCP_STATUS") != "none":
            statuses = {name: "connected" for name in self.servers}
            statuses.update(json.loads(os.environ.get("FAKE_MCP_STATUS") or "{}"))
            init["mcp_servers"] = [{"name": n, "status": s} for n, s in statuses.items() if s]
        return [init]

    @staticmethod
    def compaction(trigger: str, pre: int, post: int) -> list[dict]:
        meta = {"trigger": trigger, "pre_tokens": pre, "post_tokens": post}
        return [{"type": "system", "subtype": "compact_boundary", "compact_metadata": meta}]

    @staticmethod
    def result(text: str, error: bool, usage: dict | None) -> list[dict]:
        line = {
            "type": "result",
            "subtype": "error_during_execution" if error else "success",
            "is_error": error,
            "result": text,
            "session_id": SESSION,
        }
        if usage:  # this invocation's figures, per model
            line["total_cost_usd"] = usage["cost_usd"]
            line["model_usage_since_last_result"] = {
                MODEL: {
                    "inputTokens": usage["input_tokens"],
                    "outputTokens": usage["output_tokens"],
                    "cacheReadInputTokens": usage["cache_read_tokens"],
                    "cacheCreationInputTokens": usage["cache_creation_tokens"],
                    "costUSD": usage["cost_usd"],
                }
            }
        return [line]
