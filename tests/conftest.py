from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from fakeprovider import FakeProvider

from orchestrator import providers
from orchestrator.service import Orchestrator


@pytest.fixture
def orch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Orchestrator:
    """An orchestrator with an isolated home; runners load the fake provider via env."""
    monkeypatch.setenv("ORCH_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("ORCH_PROVIDERS", "fake=fakeprovider:FakeProvider")
    monkeypatch.setenv("PYTHONPATH", str(Path(__file__).parent))
    monkeypatch.setitem(providers.PROVIDERS, "fake", FakeProvider())
    return Orchestrator()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    path = tmp_path / "repo"
    path.mkdir()
    for args in (
        ["init", "-q", "-b", "main"],
        [
            "-c",
            "user.name=t",
            "-c",
            "user.email=t@example.com",
            "commit",
            "-q",
            "--allow-empty",
            "-m",
            "init",
        ],
    ):
        subprocess.run(["git", "-C", str(path), *args], check=True)
    return path


@pytest.fixture
def project(orch: Orchestrator, repo: Path) -> str:
    return orch.add_project(str(repo), "demo", "fake").name
