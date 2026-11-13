"""Print the release notes of one version from the changelog.

    python3 scripts/changelog_section.py <version> [<path, default CHANGELOG.md>]

Prints the body of the section `## [<version>]` (Keep a Changelog style): everything after its
heading line up to the next heading of level one or two, without leading and trailing blank
lines. The link-reference definitions at the end of the file (`[0.1.0]: https://...`) belong to
no section. The version must match exactly: `0.2.0` does not match `[0.2.0.1]` or `[10.2.0]`.

Exit code 0 with the text on stdout. A missing file, no section for the version, or an empty
one: a one-line error on stderr, nothing on stdout, exit code 1.

The release workflow (.github/workflows/release.yml) runs it with the runner's own `python3`,
without uv: standard library only, and syntax that also runs on Python 3.12.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

DEFAULT_PATH = "CHANGELOG.md"
HEADING = re.compile(r"#{1,2}\s")  # a heading that ends a section (`### Added` does not)
LINK_REFERENCE = re.compile(r"\[[^\]]+\]:\s*\S")


class ChangelogError(Exception):
    """Why there are no release notes for the version (a one-line message)."""


def blank(line: str) -> bool:
    return not line.strip()


def section(text: str, version: str) -> str | None:
    """The body of the section `## [<version>]` of the changelog `text`, without leading and
    trailing blank lines ("" when it has none); None when there is no such section."""
    lines = text.splitlines()
    end = len(lines)
    while end and (blank(lines[end - 1]) or LINK_REFERENCE.match(lines[end - 1])):
        end -= 1  # the link-reference definitions at the end of the file
    lines = lines[:end]
    heading = f"## [{version}]"
    start = next((i + 1 for i, line in enumerate(lines) if line.startswith(heading)), None)
    if start is None:
        return None
    stop = next(
        (i for i in range(start, len(lines)) if HEADING.match(lines[i])),
        len(lines),
    )
    body = lines[start:stop]
    while body and blank(body[0]):
        body.pop(0)
    while body and blank(body[-1]):
        body.pop()
    return "\n".join(body)


def notes(version: str, path: Path) -> str:
    """The non-empty section of `version` in the file `path`; ChangelogError otherwise."""
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise ChangelogError(f"{path}: no such file (asked for the section of {version})") from None
    except OSError as exc:
        raise ChangelogError(f"{path}: cannot be read ({exc.strerror}) for {version}") from None
    body = section(text, version)
    if body is None:
        raise ChangelogError(f"{path}: no section '## [{version}]'")
    if not body:
        raise ChangelogError(f"{path}: the section '## [{version}]' is empty")
    return body


def main(argv: list[str]) -> int:
    if len(argv) not in (1, 2) or not argv[0]:
        print("usage: changelog_section.py <version> [<path>]", file=sys.stderr)
        return 2
    path = Path(argv[1] if len(argv) == 2 else DEFAULT_PATH)
    try:
        body = notes(argv[0], path)
    except ChangelogError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    sys.stdout.write(body + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
