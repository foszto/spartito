"""docs/capabilities.md describes every built-in capability from its own entry: a section per
capability (its label and `name` in the heading), its providers and default rows, what it
reaches and what it requires word for word, and every status. A capability added to
capabilities.BUILTIN without a section fails here. No home, no subprocess."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from test_docs_providers import flat, row, sections

from orchestrator import capabilities
from orchestrator.capabilities import BUILTIN, DEFAULT_ON, Capability

ROOT = Path(__file__).resolve().parent.parent
DOC = ROOT / "docs" / "capabilities.md"
STATUSES = (capabilities.UNKNOWN, capabilities.UNAVAILABLE, capabilities.AVAILABLE)


def section_of(capability: Capability) -> str:
    matching = [
        body
        for heading, body in sections(DOC).items()
        if capability.label in heading and f"`{capability.name}`" in heading
    ]
    assert len(matching) == 1, (
        f"docs/capabilities.md needs one heading with {capability.label!r} and "
        f"`{capability.name}` (found {len(matching)})"
    )
    return matching[0]


@pytest.mark.parametrize("capability", BUILTIN, ids=lambda c: c.name)
def test_every_builtin_capability_has_its_section(capability: Capability) -> None:
    body = section_of(capability)
    providers = [name.strip(" `") for name in row(body, "providers").split(",")]
    assert providers == list(capability.providers)
    assert row(body, "default") == ("on" if capability.name in DEFAULT_ON else "off")


@pytest.mark.parametrize("capability", BUILTIN, ids=lambda c: c.name)
def test_what_it_reaches_and_requires_is_listed_word_for_word(capability: Capability) -> None:
    body = section_of(capability)
    assert flat(row(body, "reaches")) == flat(capability.reaches)
    listed = flat(body)
    for needed in capability.requires:
        assert f"- {flat(needed)}" in listed, f"{capability.name}: not listed: {needed}"


def test_the_list_of_levels_states_the_default() -> None:
    stated = "1. the default: off for every capability" in DOC.read_text(encoding="utf-8")
    assert stated == (not DEFAULT_ON), "the default level in docs/capabilities.md is out of date"


def test_every_status_is_explained() -> None:
    text = DOC.read_text(encoding="utf-8")
    for status in STATUSES:
        assert re.search(rf"^\|\s*`{status}`\s*\|", text, re.M), status


def test_the_usage_doc_names_the_command() -> None:
    usage = (ROOT / "docs" / "usage.md").read_text(encoding="utf-8")
    assert "spartito capabilities" in usage
    assert "(capabilities.md)" in usage


def test_the_check_sees_a_missing_section() -> None:
    """Guards the checker itself: a capability without a section is not found."""
    other = Capability(name="other", label="Other thing", description="", reaches="", providers={})
    with pytest.raises(AssertionError, match="needs one heading"):
        section_of(other)
