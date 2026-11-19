"""opencode's dialect: `opencode run --standalone --format json` (providers/opencode.py), in
the event shapes seen with opencode 2.3.2 (docs/providers-design.md, §2).

- Calls that are no turn answer at once: `--version` (`opencode v2.3.2`; FAKE_OPENCODE_VERSION
  changes the number), `models` (plain `provider/model` lines) and `auth list --format json`
  (FAKE_OPENCODE_AUTH: the JSON it prints; default: one provider connected by a variable).
- Records the OPENCODE_CONFIG file as `config_path`, `config` (parsed), `mode`, `dir_mode`;
  OPENCODE_CONFIG_CONTENT (parsed) as `config_content`; the text of its `instructions` files
  as `instructions`; its own XDG_CONFIG_HOME as `config_home`, and the text of the global
  AGENTS.md there (`opencode/AGENTS.md`) as `global_agents` (None: no such file).
- What the agent's commands would see as XDG_CONFIG_HOME, `command_config_home`: it runs the
  configured `shell` (else /bin/sh) with `-c`, as opencode runs a command; `(unset)` when
  unset. What each local MCP server would see, `server_config_home` (name -> value): its
  `environment`'s value, else the one it inherits from the CLI.
- The prompt is stdin as it is.
- Every line carries the session of `--session`. A turn is `step_start`, a `text` part and a
  `step_finish` with the TOKENS usage (zero without; cost in USD). FAIL ends with the nested
  `error` line and exit 1. No compaction event (opencode's stream has none).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

from fakecli import private_file

VERSION = "2.3.2"
MODELS = ("opencode/big-pickle", "openrouter/openai/gpt-4.1-mini")
AUTH = [{"id": "openrouter", "name": "OpenRouter", "connections": [{"type": "env", "name": "X"}]}]
SHOW_HOME = 'printf "%s" "${XDG_CONFIG_HOME-(unset)}"'


class Dialect:
    def __init__(self, argv: list[str]) -> None:
        self.argv = argv
        self.exit_code = 0

    def option(self, name: str) -> str | None:
        return self.argv[self.argv.index(name) + 1] if name in self.argv else None

    def command(self) -> int | None:
        if self.argv[:1] == ["--version"]:
            print(f"opencode v{os.environ.get('FAKE_OPENCODE_VERSION') or VERSION}")
            return 0
        if self.argv[:1] == ["models"]:
            print("\n".join(MODELS))
            return 0
        if self.argv[:2] == ["auth", "list"]:
            print(os.environ.get("FAKE_OPENCODE_AUTH") or json.dumps(AUTH))
            return 0
        if self.argv[:1] != ["run"]:
            print(f"fakecli opencode: no such command {self.argv}", file=sys.stderr)
            return 1
        return None

    def record(self, seen: dict) -> None:
        path = os.environ.get("OPENCODE_CONFIG")
        config: dict = {}
        if path:
            file = private_file(path)
            config = file["content"]
            seen.update(config_path=path, config=config, mode=file["mode"])
            seen["dir_mode"] = file["dir_mode"]
        content = os.environ.get("OPENCODE_CONFIG_CONTENT")
        seen["config_content"] = json.loads(content) if content else None
        seen["instructions"] = [
            Path(name).read_text(encoding="utf-8") for name in config.get("instructions", [])
        ]
        seen["config_home"] = os.environ.get("XDG_CONFIG_HOME")
        agents = Path(seen["config_home"] or "/nonexistent") / "opencode" / "AGENTS.md"
        seen["global_agents"] = agents.read_text(encoding="utf-8") if agents.is_file() else None
        shell = config.get("shell") or "/bin/sh"
        shown = subprocess.run([shell, "-c", SHOW_HOME], capture_output=True, text=True)
        seen["command_config_home"] = shown.stdout
        servers = config.get("mcp", {}).get("servers", {})
        seen["server_config_home"] = {
            name: server.get("environment", {}).get(
                "XDG_CONFIG_HOME", os.environ.get("XDG_CONFIG_HOME")
            )
            for name, server in servers.items()
            if server.get("type") == "local"
        }

    def before_input(self) -> None:
        pass

    @staticmethod
    def prompt(raw: str) -> str:
        return raw

    def line(self, kind: str, **fields) -> dict:
        session = self.option("--session") or ""
        return {"type": kind, "timestamp": int(time.time() * 1000), "sessionID": session, **fields}

    def part(self, kind: str, **fields) -> dict:
        session = self.option("--session") or ""
        return {
            "id": f"prt_{kind}",
            "messageID": "msg_1",
            "sessionID": session,
            "type": kind,
            **fields,
        }

    def session(self) -> list[dict]:
        return [self.line("step_start", part=self.part("step-start", snapshot="0"))]

    @staticmethod
    def compaction(trigger: str, pre: int, post: int) -> list[dict]:
        return []

    def result(self, text: str, error: bool, usage: dict | None) -> list[dict]:
        if error:
            self.exit_code = 1
            return [self.line("error", error={"type": "unknown", "message": text})]
        usage = usage or {}
        tokens = {
            "input": usage.get("input_tokens", 0),
            "output": usage.get("output_tokens", 0),
            "reasoning": 0,
            "cache": {
                "read": usage.get("cache_read_tokens", 0),
                "write": usage.get("cache_creation_tokens", 0),
            },
        }
        now = int(time.time() * 1000)
        text_part = self.part("text", text=text, time={"start": now, "end": now})
        finish = self.part(
            "step-finish", reason="stop", cost=usage.get("cost_usd", 0), tokens=tokens
        )
        return [self.line("text", part=text_part), self.line("step_finish", part=finish)]
