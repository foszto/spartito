"""A small orchestrator for fleets of coding agents (Claude Code, Codex)."""

from importlib import metadata

# The version lives in pyproject.toml only; this reads the installed distribution's metadata.
# Without it (an environment that installed this checkout under another distribution name,
# before it is reinstalled: docs/releasing.md) the version is unknown, nothing else changes.
try:
    __version__ = metadata.version("spartito")
except metadata.PackageNotFoundError:
    __version__ = "0+unknown"
