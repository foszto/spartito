"""The exceptions the interfaces catch from the core."""

from __future__ import annotations


class OrchestratorError(Exception):
    """A request that cannot be fulfilled; the message is meant for the caller."""


class NeedsConfirmation(OrchestratorError):
    """Archiving would hide work that is not merged: the message says what; repeat the
    request with confirmation to archive anyway."""
