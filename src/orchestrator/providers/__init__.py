"""Provider registry."""

from __future__ import annotations

import importlib
import os

from .base import Invocation, McpServer, Provider, RunContext
from .claude import ClaudeProvider
from .codex import CodexProvider

PROVIDERS: dict[str, Provider] = {
    ClaudeProvider.name: ClaudeProvider(),
    CodexProvider.name: CodexProvider(),
}


def _load_plugins() -> None:
    """ORCH_PROVIDERS="name=package.module:Class,..." registers extra providers."""
    for spec in filter(None, os.environ.get("ORCH_PROVIDERS", "").split(",")):
        name, _, target = spec.partition("=")
        module, _, attribute = target.partition(":")
        PROVIDERS[name.strip()] = getattr(importlib.import_module(module), attribute)()


_load_plugins()


def get(name: str) -> Provider:
    try:
        return PROVIDERS[name]
    except KeyError:
        raise KeyError(f"unknown provider '{name}' (available: {', '.join(PROVIDERS)})") from None


__all__ = ["PROVIDERS", "Invocation", "McpServer", "Provider", "RunContext", "get"]
