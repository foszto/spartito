"""The suite's guard against a real home (tests/home_guard.py). Where a test needs "a real
home", it is a stand-in directory under tmp_path that the guard is told about, or the
ORCH_HOME a nested pytest run inherits."""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
from pathlib import Path

import home_guard
import pytest

from orchestrator import config
from orchestrator.service import Orchestrator
from orchestrator.store import Store

TESTS = Path(__file__).parent


def test_undoing_the_patches_falls_back_to_the_session_home(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.undo()  # also the fixtures' ORCH_HOME and HOME
    home = config.home().resolve()
    assert home == home_guard.SESSION["home"]
    assert home_guard.real_home_of(home) is None


@pytest.mark.parametrize("inside", ["", "projects/demo"])
def test_a_real_home_is_refused_before_anything_is_created(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, inside: str
):
    real = tmp_path / "real-home"
    real.mkdir()
    monkeypatch.setattr(home_guard, "REAL_HOMES", [*home_guard.REAL_HOMES, real.resolve()])
    with monkeypatch.context() as patch:
        patch.setenv("ORCH_HOME", str(real / inside))
        for opener in (config.home, config.db_path, Store, Orchestrator):
            with pytest.raises(home_guard.RealHomeError, match="real Spartito home"):
                opener()
    assert list(real.iterdir()) == []


def test_the_default_home_is_refused_too(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Without ORCH_HOME the home is ~/.orchestrator: refused when that is a real one."""
    real = tmp_path / "user" / ".orchestrator"
    monkeypatch.setattr(home_guard, "REAL_HOMES", [*home_guard.REAL_HOMES, real.resolve()])
    with monkeypatch.context() as patch:
        patch.setenv("HOME", str(tmp_path / "user"))
        patch.delenv("ORCH_HOME")
        with pytest.raises(home_guard.RealHomeError):
            Store()
    assert not real.exists()


def test_the_guard_resolves_the_home_as_config_does(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "user"))
    for value in (str(tmp_path / "set"), "~/tilde", None):
        if value is None:
            monkeypatch.delenv("ORCH_HOME")
        else:
            monkeypatch.setenv("ORCH_HOME", value)
        assert home_guard.resolve_home() == config.home().resolve()


def test_children_inherit_a_temporary_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    show = "from orchestrator import config; print(config.home().resolve())"

    def child_home() -> Path:
        run = [sys.executable, "-c", show]
        out = subprocess.run(run, env=dict(os.environ), capture_output=True, text=True, check=True)
        return Path(out.stdout.strip())

    assert child_home() == (tmp_path / "home").resolve()
    monkeypatch.undo()
    home = child_home()
    assert home == home_guard.SESSION["home"]
    assert home_guard.real_home_of(home) is None


# -- nested pytest runs: their inherited ORCH_HOME is a stand-in for a real one --------------

UNDOING = """
    import os
    from pathlib import Path

    import home_guard
    import pytest

    from orchestrator import config
    from orchestrator.service import Orchestrator
    from orchestrator.store import Store

    INHERITED = Path(os.environ["STAND_IN_HOME"]).resolve()


    @pytest.mark.parametrize("n", range(4))
    def test_undo_falls_back_to_a_temporary_home(monkeypatch, n):
        monkeypatch.setenv("ORCH_HOME", str(INHERITED.parent / "patched"))
        monkeypatch.undo()
        home = config.home().resolve()
        assert home == home_guard.SESSION["home"] != INHERITED
        Store()
        Orchestrator()
        assert (home / "orch.db").exists()
        assert home_guard.real_home_of(INHERITED) == INHERITED
        worker = os.environ.get("PYTEST_XDIST_WORKER", "main")
        if worker != "main":  # it inherited the controller's temporary home, not a real one
            controller = Path(home_guard.SESSION["inherited"]).resolve()
            assert home_guard.real_home_of(controller) is None
        (Path(os.environ["NOTES"]) / f"{n}").write_text(f"{worker}\\n{home}")


    def test_the_inherited_home_is_refused(monkeypatch):
        for home in (INHERITED, INHERITED / "inside"):
            with monkeypatch.context() as patch:
                patch.setenv("ORCH_HOME", str(home))
                with pytest.raises(home_guard.RealHomeError):
                    config.home()
"""

LEAKING = """
    import os
    from pathlib import Path

    import home_guard
    import pytest

    OUTSIDE = os.environ["OUTSIDE_HOME"]


    @pytest.fixture
    def inherited_home(monkeypatch):
        monkeypatch.setenv("ORCH_HOME", os.environ["STAND_IN_HOME"])


    def test_plain():
        pass


    def test_starts_in_the_inherited_home(inherited_home):
        pass


    def test_ends_outside(monkeypatch):
        monkeypatch.setenv("ORCH_HOME", OUTSIDE)


    def test_leaks_outside():
        os.environ["ORCH_HOME"] = OUTSIDE


    def test_after_the_leak():
        assert os.environ["ORCH_HOME"] == str(home_guard.SESSION["home"])
"""


class Nested:
    """A pytest run of one generated test file with the guard as its plugin, in tmp_path:
    its inherited ORCH_HOME is the stand-in, an empty directory."""

    def __init__(self, tmp_path: Path) -> None:
        self.dir = tmp_path / "nested"
        self.dir.mkdir()
        (self.dir / "pytest.ini").write_text("[pytest]\n")
        self.stand_in = tmp_path / "inherited-home"
        self.stand_in.mkdir()
        self.outside = tmp_path / "elsewhere"
        self.notes = tmp_path / "notes"
        self.notes.mkdir()
        self.basetemp = tmp_path / "basetemp"

    def run(self, source: str, *args: str) -> subprocess.CompletedProcess[str]:
        test = self.dir / "test_nested.py"
        test.write_text(textwrap.dedent(source))
        env = {k: v for k, v in os.environ.items() if not k.startswith(("PYTEST_", "COV_CORE_"))}
        env.update(
            ORCH_HOME=str(self.stand_in),
            STAND_IN_HOME=str(self.stand_in),
            OUTSIDE_HOME=str(self.outside),
            NOTES=str(self.notes),
            PYTHONPATH=str(TESTS),
            ORCH_DESKTOP_NOTIFICATIONS="off",
        )
        command = [sys.executable, "-m", "pytest", "-p", "home_guard", "-p", "no:cacheprovider"]
        command += ["-q", "-rA", f"--basetemp={self.basetemp}", f"--rootdir={self.dir}"]
        return subprocess.run(
            [*command, *args, str(test)],
            cwd=self.dir,
            env=env,
            capture_output=True,
            text=True,
            timeout=120,
        )

    def notes_by_test(self) -> dict[str, list[str]]:
        return {note.name: note.read_text().splitlines() for note in self.notes.iterdir()}


@pytest.mark.parametrize("workers", [[], ["-n", "2"]], ids=["one-process", "xdist"])
def test_a_run_never_reaches_its_inherited_home(tmp_path: Path, workers: list[str]):
    nested = Nested(tmp_path)
    result = nested.run(UNDOING, *workers)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "5 passed" in result.stdout
    assert list(nested.stand_in.iterdir()) == []
    notes = nested.notes_by_test()
    assert sorted(notes) == ["0", "1", "2", "3"]
    names = {worker for worker, _ in notes.values()}
    assert names <= {"gw0", "gw1"} if workers else names == {"main"}
    for _, home in notes.values():  # each process's own temporary home, removed at the end
        assert not Path(home).exists()
        assert not Path(home).is_relative_to(nested.stand_in.resolve())


def test_the_per_test_guard_fails_a_test_outside_the_temporary_homes(tmp_path: Path):
    nested = Nested(tmp_path)
    result = nested.run(LEAKING)
    out = result.stdout
    assert result.returncode == 1, out + result.stderr
    assert "3 failed, 2 passed, 1 error" in out
    real = f"the home is a real Spartito home: {nested.stand_in.resolve()}"
    outside = f"outside pytest's temporary directories: {nested.outside.resolve()}"
    assert f"{real} (at the start of the test)" in out
    assert f"{outside} (at the end of the test)" in out
    assert f"{outside} (after the test's teardown)" in out
    for name in ("starts_in_the_inherited_home", "ends_outside", "leaks_outside"):
        assert f"FAILED test_nested.py::test_{name}" in out
    assert "ERROR test_nested.py::test_leaks_outside" in out
    assert "PASSED test_nested.py::test_after_the_leak" in out
    assert not nested.outside.exists()
    assert list(nested.stand_in.iterdir()) == []
