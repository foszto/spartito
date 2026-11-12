"""Every relative link in README.md and docs/*.md resolves: the file exists and the `#anchor`
names a heading of the target (GitHub's heading-to-anchor rule). README.md is also PyPI's
project page, where relative links lead nowhere: its links into this repository are absolute
(REPO), and they are checked as the files they name."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
DOCS = [ROOT / "README.md", *sorted((ROOT / "docs").glob("*.md"))]

FENCE = re.compile(r"^\s*(```|~~~)")
HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
INLINE_CODE = re.compile(r"(`+).*?\1")
# [text](target) or ![alt](target); the target ends at whitespace or ")".
LINK = re.compile(r"!?\[(?:[^\[\]]|\[[^\]]*\])*\]\(\s*<?([^)\s>]*)>?(?:\s+\"[^\"]*\")?\s*\)")
EXTERNAL = re.compile(r"^[a-z][a-z0-9+.-]*:", re.IGNORECASE)
REPO = "https://github.com/foszto/spartito"


def local(target: str) -> str:
    """A link to this repository's files on master as the local path; others unchanged."""
    for prefix in (f"{REPO}/blob/master/", f"{REPO}/tree/master/"):
        if target.startswith(prefix):
            return str(ROOT / target.removeprefix(prefix))
    if target.partition("#")[0] == REPO:
        return str(ROOT / "README.md") + target.removeprefix(REPO)
    return target


def prose_lines(path: Path) -> list[tuple[int, str]]:
    """The lines outside fenced code blocks, with their line numbers."""
    lines, fenced = [], False
    for number, line in enumerate(path.read_text().splitlines(), 1):
        if FENCE.match(line):
            fenced = not fenced
            continue
        if not fenced:
            lines.append((number, line))
    return lines


def slug(heading: str) -> str:
    """GitHub's anchor for a heading: the rendered text, lower case, without punctuation
    other than `-` and `_`, spaces turned into `-`."""
    text = re.sub(r"!?\[([^\]]*)\]\([^)]*\)", r"\1", heading)  # links: their text
    text = text.replace("`", "")
    text = re.sub(r"<[^>]+>", "", text).strip().lower()
    text = "".join(c for c in text if c.isalnum() or c in " -_")
    return text.replace(" ", "-")


def anchors(path: Path) -> set[str]:
    found: set[str] = set()
    seen: dict[str, int] = {}
    for _, line in prose_lines(path):
        if match := HEADING.match(line):
            base = slug(match.group(2))
            count = seen.get(base, 0)
            seen[base] = count + 1
            found.add(base if count == 0 else f"{base}-{count}")
    return found


def links(path: Path) -> list[tuple[int, str]]:
    found = []
    for number, line in prose_lines(path):
        for match in LINK.finditer(INLINE_CODE.sub("", line)):
            target = local(match.group(1))
            if target and not EXTERNAL.match(target):
                found.append((number, target))
    return found


def broken(path: Path) -> list[str]:
    problems = []
    for number, target in links(path):
        file_part, _, anchor = target.partition("#")
        dest = (path.parent / file_part).resolve() if file_part else path
        where = f"{path.relative_to(ROOT)}:{number}: {target}"
        if not dest.exists():
            problems.append(f"{where}: no such file")
        elif anchor and dest.suffix == ".md" and anchor not in anchors(dest):
            problems.append(f"{where}: no heading with this anchor in {dest.name}")
    return problems


@pytest.mark.parametrize("path", DOCS, ids=lambda p: str(p.relative_to(ROOT)))
def test_relative_links_resolve(path: Path):
    assert not (problems := broken(path)), "broken links:\n" + "\n".join(problems)


def test_the_check_sees_links():
    """Guards the checker itself: it finds links, and GitHub's rule on a few headings."""
    assert any(target.startswith(str(ROOT / "docs")) for _, target in links(ROOT / "README.md"))
    assert slug("Overrides: `loadout.toml`") == "overrides-loadouttoml"
    assert slug("Desktop notifications: `desktop_notifications`") == (
        "desktop-notifications-desktop_notifications"
    )
    assert slug("Automatic compaction: `compact_at_tokens`") == (
        "automatic-compaction-compact_at_tokens"
    )
    assert slug("Command line, MCP and API") == "command-line-mcp-and-api"
    assert slug("1b. Launcher, name and icon (macOS)") == "1b-launcher-name-and-icon-macos"


def test_the_readme_links_work_on_pypi():
    """PyPI shows README.md without the repository: no relative link, no bare #anchor."""
    found = [
        f"README.md:{number}: {match.group(1)}"
        for number, line in prose_lines(ROOT / "README.md")
        for match in LINK.finditer(INLINE_CODE.sub("", line))
        if not EXTERNAL.match(match.group(1))
    ]
    assert not found, "relative links in README.md:\n" + "\n".join(found)
