"""`scripts/changelog_section.py`: the release notes of one version, as the release workflow
takes them from CHANGELOG.md (no home, no network)."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).parent.parent / "scripts"

CHANGELOG = """\
# Changelog

Notes on the format.

## [Unreleased]

## [10.2.0] - 2027-01-01

### Added

- Something much later.

## [0.2.0.1] - 2026-12-01

- A fourth number.

## [0.2.0] - 2026-11-13

### Added

- A new command.

### Fixed

- A fix.


## [0.1.1] - 2026-11-13


## [0.1.0] - 2026-11-13

- The first release.

[Unreleased]: https://example.com/compare/v0.2.0...HEAD
[0.2.0]: https://example.com/compare/v0.1.0...v0.2.0
[0.1.0]: https://example.com/releases/tag/v0.1.0
"""


@pytest.fixture
def changelog(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.syspath_prepend(str(SCRIPTS))
    import changelog_section

    return changelog_section


def test_a_middle_section_ends_at_the_next_heading(changelog) -> None:
    assert changelog.section(CHANGELOG, "0.2.0") == (
        "### Added\n\n- A new command.\n\n### Fixed\n\n- A fix."
    )


def test_the_last_section_leaves_out_the_link_references(changelog) -> None:
    assert changelog.section(CHANGELOG, "0.1.0") == "- The first release."


def test_a_version_matches_exactly(changelog) -> None:
    assert changelog.section(CHANGELOG, "0.2.0.1") == "- A fourth number."
    assert changelog.section(CHANGELOG, "10.2.0") == "### Added\n\n- Something much later."
    assert changelog.section(CHANGELOG, "0.2") is None
    assert changelog.section(CHANGELOG, "2.0") is None


def test_a_missing_or_empty_section(changelog, tmp_path: Path) -> None:
    assert changelog.section(CHANGELOG, "0.3.0") is None
    assert changelog.section(CHANGELOG, "0.1.1") == ""
    path = tmp_path / "CHANGELOG.md"
    path.write_text(CHANGELOG, encoding="utf-8")
    with pytest.raises(changelog.ChangelogError, match=r"no section '## \[0\.3\.0\]'"):
        changelog.notes("0.3.0", path)
    with pytest.raises(changelog.ChangelogError, match=r"section '## \[0\.1\.1\]' is empty"):
        changelog.notes("0.1.1", path)
    assert changelog.notes("0.1.0", path) == "- The first release."


def test_a_missing_file(changelog, tmp_path: Path) -> None:
    path = tmp_path / "CHANGELOG.md"
    with pytest.raises(changelog.ChangelogError, match="no such file") as caught:
        changelog.notes("0.2.0", path)
    assert str(path) in str(caught.value) and "0.2.0" in str(caught.value)


def test_the_script_prints_the_section_or_fails_with_one_line(tmp_path: Path) -> None:
    path = tmp_path / "CHANGELOG.md"
    path.write_text(CHANGELOG, encoding="utf-8")
    script = str(SCRIPTS / "changelog_section.py")

    def run(*args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-I", script, *args],
            capture_output=True,
            text=True,
            timeout=60,
            cwd=tmp_path,
        )

    found = run("0.2.0")  # CHANGELOG.md of the working directory
    assert found.returncode == 0 and found.stderr == ""
    assert found.stdout == "### Added\n\n- A new command.\n\n### Fixed\n\n- A fix.\n"
    failed = run("0.3.0", str(path))
    assert failed.returncode != 0 and failed.stdout == ""
    assert failed.stderr.count("\n") == 1
    assert "0.3.0" in failed.stderr and str(path) in failed.stderr
