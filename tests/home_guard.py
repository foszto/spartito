"""No test may reach a real Spartito home: a pytest plugin, loaded by conftest.py (and with
`-p home_guard` by the nested runs of test_home_guard.py).

Three layers, all at session level, outside the `monkeypatch` fixture, so that a test that
undoes its own patches (`monkeypatch.undo()`) cannot remove them:

1. The environment: before any test or fixture runs, every pytest process (each xdist worker
   too) sets ORCH_HOME in os.environ to a temporary directory of its own, removed at the end
   (and again at exit). A test that undoes the fixtures' patches falls back to it, and so
   does a child that inherits os.environ. A child started with an environment of its own
   gets neither this home nor the refusal (2. works in this process only): such a child must
   be given ORCH_HOME. HOME is left as it is: with ORCH_HOME always set, `~/.orchestrator` is
   never the home unless a test deletes ORCH_HOME itself, and then the real user's default
   is refused (2.). ORCH_TURN_HOME is left as inherited: it only keeps the children quiet.
2. The refusal: config.home() raises RealHomeError, before creating anything, when the home
   is a real one or inside one: the ORCH_HOME the pytest run inherited (remembered in
   REAL_HOMES_ENV, which carries it to the xdist workers, whose inherited ORCH_HOME is the
   controller's temporary one) and the default `~/.orchestrator` of the real user (from the
   password database, not from a patched HOME). Store() and Orchestrator() resolve their
   home through config.home(). Paths are compared on disk too (`identities`), so another
   spelling of a real home (other capitals, a firmlink or symlink alias) is refused as well.
   Neither this nor ORCH_HOME is undone at the end of the session: a thread a test left
   behind still finds the guard.
3. The per-test guard: a test fails when the home is real or outside pytest's temporary
   directories (and this session's home) right before its body, right after it (before any
   fixture's teardown), and once all its fixtures are torn down (a change of os.environ
   that no fixture restores); the last also puts the session's home back. The checks after
   the body and after the teardown run also when these raise, and add to their failure.
"""

from __future__ import annotations

import atexit
import os
import shutil
import tempfile
from pathlib import Path

import pytest

from orchestrator import config as orch_config

# The real homes, os.pathsep-separated: set by the first pytest process of a run, inherited
# by its xdist workers and by every child of a test (a nested pytest adds its own ORCH_HOME).
REAL_HOMES_ENV = "ORCH_TEST_REAL_HOMES"
DEFAULT_HOME = "~/.orchestrator"  # config.home() without ORCH_HOME

REAL_HOMES: list[Path] = []  # the homes no test may resolve (a test adds a stand-in)
ALLOWED: list[Path] = []  # this session's home and pytest's base temporary directory
SESSION: dict[str, object] = {}  # the session's home, and the inherited ORCH_HOME
# Passed on as they are, never wrapped in a home failure: an interruption is no test failure.
INTERRUPTIONS = (KeyboardInterrupt, SystemExit, GeneratorExit)


class RealHomeError(RuntimeError):
    """A test resolved a real Spartito home."""


class HomeGuardFailure(Exception):
    """The home was wrong after a test body or teardown that failed on its own as well."""


def resolve_home() -> Path:
    """The home config.home() would use now (resolved), without creating it."""
    return Path(os.environ.get("ORCH_HOME", DEFAULT_HOME)).expanduser().resolve()


def user_default_home() -> Path:
    """`~/.orchestrator` of the real user, whatever HOME says."""
    try:
        import pwd

        user_home = Path(pwd.getpwuid(os.getuid()).pw_dir)
    except ImportError, KeyError:
        user_home = Path.home()
    return (user_home / ".orchestrator").resolve()


def identities(path: Path) -> list[tuple[object, tuple[str, ...]]]:
    """Where `path` and each of its ancestors (root first) are on disk: (device, inode) of the
    deepest one of them that exists, and the names below it, case-folded. Two spellings of
    one directory (other capitals on a case-insensitive volume, a firmlink, a symlink) get
    the same identity, also before the directory exists."""
    found: list[tuple[object, tuple[str, ...]]] = []
    base: object = None
    rest: tuple[str, ...] = ()
    for part in reversed([path, *path.parents]):
        try:
            stat = os.stat(part) if not rest else None
        except OSError:
            stat = None
        if stat is not None:
            base, rest = (stat.st_dev, stat.st_ino), ()
        else:
            rest = (*rest, part.name.casefold())
        found.append((base, rest))
    return found


def real_home_of(path: Path) -> Path | None:
    """The real home `path` is or is inside of, if any: by its path, or on disk."""
    if real := next((real for real in REAL_HOMES if path.is_relative_to(real)), None):
        return real
    reals = {identities(real)[-1]: real for real in REAL_HOMES}
    return next((reals[i] for i in identities(path) if i in reals), None)


def refuse_real(path: Path) -> None:
    if real_home_of(path) is not None:
        raise RealHomeError(
            f"a test resolved a real Spartito home ({path}): tests run in a temporary "
            "ORCH_HOME and must not undo the suite's environment patches"
        )


def guarded(home):
    def home_in_tests() -> Path:
        refuse_real(resolve_home())
        return home()

    home_in_tests.__wrapped__ = home
    return home_in_tests


def pytest_configure(config: pytest.Config) -> None:
    inherited = os.environ.get("ORCH_HOME")
    homes = [Path(p) for p in os.environ.get(REAL_HOMES_ENV, "").split(os.pathsep) if p]
    if not hasattr(config, "workerinput"):  # an xdist worker inherits the controller's home
        homes.append(resolve_home())
    homes.append(user_default_home())
    REAL_HOMES[:] = list(dict.fromkeys(homes))
    os.environ[REAL_HOMES_ENV] = os.pathsep.join(str(p) for p in REAL_HOMES)
    session_home = Path(tempfile.mkdtemp(prefix="spartito-test-home-")).resolve()
    SESSION.update(home=session_home, inherited=inherited)
    ALLOWED[:] = [session_home]
    os.environ["ORCH_HOME"] = str(session_home)
    orch_config.home = guarded(orch_config.home)
    atexit.register(shutil.rmtree, session_home, ignore_errors=True)  # a late thread's leftovers


def pytest_unconfigure(config: pytest.Config) -> None:
    """Removes the session's home; the guard and ORCH_HOME stay until the process exits."""
    if "home" in SESSION:
        shutil.rmtree(SESSION["home"], ignore_errors=True)


@pytest.fixture(scope="session", autouse=True)
def home_guard_temp_dirs(tmp_path_factory: pytest.TempPathFactory) -> None:
    """pytest's base temporary directory (this worker's) is where the tests' homes live."""
    ALLOWED.append(tmp_path_factory.getbasetemp().resolve())


def home_problem() -> str:
    """What is wrong with the home config.home() would resolve now ("" if nothing)."""
    home = resolve_home()
    if real_home_of(home) is not None:
        return f"the home is a real Spartito home: {home}"
    if not any(home.is_relative_to(root) for root in ALLOWED):
        return f"the home is outside pytest's temporary directories: {home}"
    return ""


def reset_home() -> str:
    """home_problem(); with one, ORCH_HOME is this session's home again."""
    if problem := home_problem():
        os.environ["ORCH_HOME"] = str(SESSION["home"])
    return problem


def fail_with(problem: str, when: str, failure: BaseException | None = None) -> None:
    """Fails the step on a home problem; when the step failed on its own (`failure`), raises
    from it, so that both are reported."""
    if not problem:
        return
    if failure is None:
        pytest.fail(f"{problem} ({when})", pytrace=False)
    raise HomeGuardFailure(f"{problem} ({when})") from failure


@pytest.hookimpl(wrapper=True)
def pytest_runtest_call(item: pytest.Item):
    when = "at the end of the test"
    fail_with(home_problem(), "at the start of the test")
    try:
        result = yield
    except INTERRUPTIONS:
        raise
    except BaseException as failure:  # a failing test (pytest's outcomes are BaseExceptions)
        fail_with(home_problem(), when, failure)
        raise
    fail_with(home_problem(), when)
    return result


@pytest.hookimpl(wrapper=True)
def pytest_runtest_teardown(item: pytest.Item, nextitem: pytest.Item | None):
    # reset_home in every case: the next test starts in this session's home.
    when = "after the test's teardown"
    try:
        result = yield
    except BaseException as failure:
        problem = reset_home()
        if not isinstance(failure, INTERRUPTIONS):
            fail_with(problem, when, failure)
        raise
    fail_with(reset_home(), when)
    return result
