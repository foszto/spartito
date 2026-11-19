"""Provider registry."""

from __future__ import annotations

import importlib
import os

from .base import Invocation, McpServer, Provider, RunContext
from .claude import ClaudeProvider
from .codex import CodexProvider
from .opencode import OpencodeProvider

# The supported harnesses, in the order "the first available one" goes by.
BUILTIN = (ClaudeProvider, CodexProvider, OpencodeProvider)
PROVIDERS: dict[str, Provider] = {cls.name: cls() for cls in BUILTIN}


def _load_plugins() -> None:
    """ORCH_PROVIDERS="name=package.module:Class,..." registers extra providers."""
    for spec in filter(None, os.environ.get("ORCH_PROVIDERS", "").split(",")):
        name, _, target = spec.partition("=")
        module, _, attribute = target.partition(":")
        PROVIDERS[name.strip()] = getattr(importlib.import_module(module), attribute)()


_load_plugins()


def builtin_labels() -> str:
    """The supported harnesses as people read them: `Claude Code, Codex and OpenCode`."""
    labels = [getattr(cls, "label", None) or cls.name for cls in BUILTIN]
    return " and ".join([", ".join(labels[:-1]), labels[-1]] if len(labels) > 1 else labels)


def get(name: str) -> Provider:
    try:
        return PROVIDERS[name]
    except KeyError:
        raise KeyError(f"unknown provider '{name}' (available: {', '.join(PROVIDERS)})") from None


__all__ = ["BUILTIN", "PROVIDERS", "Invocation", "McpServer", "Provider", "RunContext", "get"]
