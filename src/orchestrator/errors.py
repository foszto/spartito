"""The exceptions the interfaces catch from the core."""

from __future__ import annotations


class OrchestratorError(Exception):
    """A request that cannot be fulfilled; the message is meant for the caller."""


class NeedsConfirmation(OrchestratorError):
    """The request would lose or hide something (archiving unmerged work, moving an agent's
    conversation to another provider): the message says what; repeat the request with
    confirmation to go ahead anyway."""


class ConfirmAgents(NeedsConfirmation):
    """Removing a project would delete its archived agents: the message names them; repeat
    the request confirming exactly these ids (`agents`)."""

    def __init__(self, message: str, agents: list[str]) -> None:
        super().__init__(message)
        self.agents = agents
