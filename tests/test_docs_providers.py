"""docs/providers.md describes every built-in provider from its own attributes: a section per
provider (its label and `name` in the heading), its efforts, cost and role prompt rows, its
limits word for word, and every availability status. A provider registered in
providers.BUILTIN without a section fails here. No home, no subprocess."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from orchestrator.providers import BUILTIN, locate

ROOT = Path(__file__).resolve().parent.parent
DOC = ROOT / "docs" / "providers.md"
HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*$")
COST = {True: "reported", False: "not reported"}
ROLE_PROMPT = {"every_turn": "every turn", "first_turn": "first turn of a conversation"}


def flat(text: str) -> str:
    """The text with every run of whitespace as one space: line breaks do not matter."""
    return " ".join(text.split())


def sections(path: Path) -> dict[str, str]:
    """Each heading's text with the text up to the next heading of the same or a higher
    level (code fences are not headings)."""
    lines = path.read_text(encoding="utf-8").splitlines()
    found: list[tuple[int, int, str]] = []  # (line index, level, heading)
    fenced = False
    for index, line in enumerate(lines):
        if line.lstrip().startswith(("```", "~~~")):
            fenced = not fenced
        elif not fenced and (match := HEADING.match(line)):
            found.append((index, len(match.group(1)), match.group(2)))
    result = {}
    for position, (index, level, heading) in enumerate(found):
        end = next((i for i, lv, _ in found[position + 1 :] if lv <= level), len(lines))
        result[heading] = "\n".join(lines[index + 1 : end])
    return result


def section_of(cls: type) -> str:
    label = getattr(cls, "label", None) or cls.name
    matching = [
        body
        for heading, body in sections(DOC).items()
        if label in heading and f"`{cls.name}`" in heading
    ]
    assert len(matching) == 1, (
        f"docs/providers.md needs one heading with {label!r} and `{cls.name}` "
        f"(found {len(matching)})"
    )
    return matching[0]


def row(body: str, name: str) -> str:
    """The value of the table row `| name | value |` in a section."""
    match = re.search(rf"^\|\s*{re.escape(name)}\s*\|\s*(.*?)\s*\|\s*$", body, re.MULTILINE)
    assert match, f"no `| {name} | … |` row"
    return match.group(1)


@pytest.mark.parametrize("cls", BUILTIN, ids=lambda cls: cls.name)
def test_every_builtin_provider_has_its_section(cls: type) -> None:
    body = section_of(cls)
    efforts = row(body, "efforts")
    assert [e.strip(" `") for e in efforts.split(",")] == list(cls.efforts)
    reports_cost = getattr(cls, "reports_cost", None)
    if reports_cost is not None:
        assert row(body, "cost") == COST[reports_cost]
    role_prompt = getattr(cls, "role_prompt", None)
    if role_prompt is not None:
        assert row(body, "role prompt") == ROLE_PROMPT[role_prompt]


@pytest.mark.parametrize("cls", BUILTIN, ids=lambda cls: cls.name)
def test_the_limits_are_listed_word_for_word(cls: type) -> None:
    listed = flat(section_of(cls))
    for limit in getattr(cls, "limits", None) or ():
        assert f"- {flat(limit)}" in listed, f"{cls.name}: limit not listed: {limit}"


def test_the_supported_harness_table_lists_every_builtin() -> None:
    table = sections(DOC)["Supported harnesses"]
    for cls in BUILTIN:
        label = getattr(cls, "label", None) or cls.name
        assert re.search(rf"^\|\s*{re.escape(label)}\s*\|\s*`{cls.name}`\s*\|", table, re.M)


def test_every_status_is_explained() -> None:
    text = DOC.read_text(encoding="utf-8")
    for status in locate.STATUSES:
        assert re.search(rf"^\|\s*`{status}`\s*\|", text, re.M), status


def test_the_usage_doc_names_the_command() -> None:
    assert "spartito providers" in (ROOT / "docs" / "usage.md").read_text(encoding="utf-8")


def test_the_check_sees_a_missing_section() -> None:
    """Guards the checker itself: a provider without a section is not found."""

    class Other:
        name = "other"
        label = "Other Agent"
        efforts = ("low",)

    with pytest.raises(AssertionError, match="needs one heading"):
        section_of(Other)
