"""The fake provider standing in for Codex with Codex's credit: a turn's attribution rule
follows the provider's `credit`, not its name. Loaded by the runners a test starts
(ORCH_PROVIDERS)."""

from __future__ import annotations

from fakeprovider import FakeProvider

from orchestrator.providers.codex import CodexProvider


class CodexStandIn(FakeProvider):
    credit = CodexProvider.credit
