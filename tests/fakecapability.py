"""Capabilities for the tests' plugin loading (ORCH_CAPABILITIES): `fake-tool` is carried by
the fake provider, which records the wirings it is given (fakeprovider.py)."""

from __future__ import annotations

from orchestrator.capabilities import Capability
from orchestrator.providers.base import Support, Wiring

FAKE_TOOL = Capability(
    name="fake-tool",
    label="Fake tool",
    description="a capability of the test suite",
    reaches="nothing",
    providers={
        "fake": Support(
            on=Wiring(argv=("--fake-tool",), env={"FAKE_TOOL": "1"}),
            off=Wiring(env={"FAKE_TOOL": "0"}),
        )
    },
)
OTHER = Capability(
    name="fake-other",
    label="Fake other",
    description="another capability of the test suite",
    reaches="nothing",
    providers={},
)
BOTH = (FAKE_TOOL, OTHER)
