"""`orch menubar`. Cocoa is never loaded: `show_item` is faked wherever `run` gets that far,
and `subprocess.Popen` is faked for clicks."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest
from typer.testing import CliRunner

from orchestrator import cli
from orchestrator.gui import menubar, window
from orchestrator.gui.menubar import DECISION, OPEN, QUIT, SEPARATOR, Bar, Counts, Entry
from orchestrator.service import Orchestrator

GUI_MODULES = ("AppKit", "Foundation", "objc", "PyObjCTools", "webview")
REAL_LOAD_COCOA = menubar.load_cocoa


@pytest.fixture(autouse=True)
def no_cocoa(monkeypatch: pytest.MonkeyPatch) -> list[Bar]:
    """macOS, a Cocoa that loads but is never used, and a `show_item` that only notes its
    bar and ends at once. Returns the bars shown."""
    shown: list[Bar] = []
    monkeypatch.setattr(menubar, "this_platform", lambda: "darwin")
    monkeypatch.setattr(menubar, "load_cocoa", lambda: {})
    monkeypatch.setattr(menubar, "show_item", lambda bar, cocoa: shown.append(bar))
    return shown


@pytest.fixture
def launched(monkeypatch: pytest.MonkeyPatch) -> list[tuple[tuple, dict]]:
    """Processes started (not really)."""
    calls: list[tuple[tuple, dict]] = []

    class Proc:
        def __init__(self, *args, **kwargs) -> None:
            calls.append((args, kwargs))

        def wait(self) -> int:
            return 0

    monkeypatch.setattr(menubar.subprocess, "Popen", Proc)
    return calls


def decisions(n: int) -> tuple[tuple[int, str], ...]:
    return tuple((i, f"Question {i}?") for i in range(1, n + 1))


def invoke(*args: str):
    return CliRunner().invoke(cli.app, ["menubar", *args])


# -- what it shows -----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("running", "open_", "expected"),
    [(0, 0, "♪"), (2, 0, "♪ 2 · 0"), (0, 1, "♪ 0 · 1"), (3, 12, "♪ 3 · 12")],
)
def test_the_title_shows_running_agents_and_open_decisions(
    running: int, open_: int, expected: str
) -> None:
    assert menubar.title(Counts(running, decisions(open_))) == expected


def test_the_menu_without_decisions() -> None:
    assert menubar.menu(Counts(2)) == [
        Entry("Running agents: 2"),
        Entry("Open decisions: 0"),
        SEPARATOR,
        Entry("Open Spartito", OPEN),
        Entry("Quit", QUIT),
    ]


def test_the_menu_lists_at_most_ten_decisions_with_questions_cut() -> None:
    long = "Which database should the service use for its\nsessions, given " + "x" * 80
    counts = Counts(1, ((40, long), *decisions(11)))

    entries = menubar.menu(counts)

    assert entries[:3] == [Entry("Running agents: 1"), Entry("Open decisions: 12"), SEPARATOR]
    first = entries[3]
    assert first.action == DECISION and first.decision == 40
    question = first.title.removeprefix("#40  ")
    assert len(question) == 60 and question.endswith("…") and "\n" not in question
    assert question.startswith("Which database should the service use for its sessions,")
    assert [e.decision for e in entries[3:13]] == [40, *range(1, 10)]
    assert entries[4].title == "#1  Question 1?"
    assert entries[13:] == [
        Entry("… and 2 more"),
        SEPARATOR,
        Entry("Open Spartito", OPEN),
        Entry("Quit", QUIT),
    ]


def test_exactly_ten_decisions_need_no_more_line() -> None:
    titles = [e.title for e in menubar.menu(Counts(0, decisions(10)))]
    assert not any(t.startswith("…") for t in titles)
    assert menubar.cut("x" * 60) == "x" * 60


def test_the_printed_lines() -> None:
    assert menubar.lines(Counts(1, ((7, "Ship it?"),))) == [
        "title: ♪ 1 · 1",
        "  Running agents: 1",
        "  Open decisions: 1",
        "  ---",
        "  #7  Ship it?",
        "  ---",
        "  Open Spartito",
        "  Quit",
    ]


# -- reading -----------------------------------------------------------------------------


def test_a_failed_read_keeps_the_last_counts(capsys: pytest.CaptureFixture[str]) -> None:
    answers: list[Counts | Exception] = [
        Counts(2, decisions(1)),
        OSError("database is locked"),
        RuntimeError("again"),
        Counts(2, decisions(1)),
        Counts(1, ()),
    ]

    def read() -> Counts:
        answer = answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer

    printed: list[str] = []
    bar = Bar(read=read, echo=printed.append)

    def refresh() -> bool:
        bar.read_once()
        return bar.pick_up()

    assert refresh() is True
    assert refresh() is False and bar.counts == Counts(2, decisions(1))
    assert refresh() is False and bar.counts == Counts(2, decisions(1))
    assert refresh() is False  # works again, nothing changed
    assert refresh() is True and bar.counts == Counts(1, ())
    assert [text.splitlines()[0] for text in printed] == ["title: ♪ 2 · 1", "title: ♪ 1 · 0"]
    err = capsys.readouterr().err.splitlines()
    assert err == ["cannot read the counts, keeping the last ones: database is locked"]


def test_the_first_read_is_printed_even_when_all_is_zero() -> None:
    printed: list[str] = []
    bar = Bar(read=Counts, echo=printed.append)
    assert bar.pick_up() is False and printed == []  # nothing read yet
    bar.read_once()
    assert bar.pick_up() is True
    assert printed == ["\n".join(menubar.lines(Counts()))]


def test_the_counts_come_from_the_facade(
    orch: Orchestrator, project: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    read = menubar.FacadeReader()
    assert read() == Counts(0, ())
    facade = read.orch
    agent, _ = orch.spawn(project, "SLEEP 30", worktree=False)
    deadline = time.monotonic() + 20
    while orch.running_count() != 1:
        assert time.monotonic() < deadline
        time.sleep(0.05)
    first = orch.request_decision(agent.id, "Which database?")
    second = orch.request_decision(agent.id, "Ship it?")

    assert read() == Counts(1, ((first.id, "Which database?"), (second.id, "Ship it?")))
    assert read.orch is facade  # made once, reused

    orch.stop(agent.id)
    while orch.running_count():
        assert time.monotonic() < deadline
        time.sleep(0.05)
    orch.withdraw_decision(first.id, agent.id)
    assert read() == Counts(0, ((second.id, "Ship it?"),))

    def broken(self) -> int:
        raise OSError("database is locked")

    with monkeypatch.context() as patch:
        patch.setattr(Orchestrator, "running_count", broken)
        with pytest.raises(OSError):
            read()
    assert read.orch is None  # a new facade at the next read
    assert read() == Counts(0, ((second.id, "Ship it?"),)) and read.orch is not facade


# -- what a click does -------------------------------------------------------------------


def test_open_starts_a_detached_orch_app(launched: list) -> None:
    Bar().click(Entry("Open Spartito", OPEN))

    ((argv,), kwargs) = launched[0]
    assert list(argv) == [sys.executable, "-m", "orchestrator", "app"]
    assert kwargs["start_new_session"] is True and kwargs["stdin"] == subprocess.DEVNULL


def test_a_decision_starts_orch_app_at_that_decision(launched: list) -> None:
    """Always through `orch app`: it opens its window there, or brings the open one forward
    there; the default browser is never used."""
    Bar().click(Entry("#4  Ship it?", DECISION, 4))
    ((argv,), kwargs) = launched[0]
    assert list(argv) == [sys.executable, "-m", "orchestrator", "app", "--decision", "4"]
    assert kwargs["start_new_session"] is True
    assert not hasattr(menubar, "webbrowser")


def test_open_says_so_when_orch_app_cannot_start(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def refuse(*args, **kwargs):
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(menubar.subprocess, "Popen", refuse)
    Bar().click(Entry("Open Spartito", OPEN))
    assert capsys.readouterr().err == "orch app did not start: Permission denied\n"


def test_quit_ends_only_the_item(launched: list) -> None:
    bar = Bar()
    bar.click(Entry("Quit", QUIT))
    assert bar.done.is_set() and launched == []
    bar.click(Entry("Running agents: 0"))  # an information line does nothing


def test_a_started_orch_app_is_reaped(monkeypatch: pytest.MonkeyPatch) -> None:
    """One that exits at once (its window is already open) does not stay a zombie."""
    monkeypatch.setattr(menubar, "APP_ARGV", (sys.executable, "-c", "pass"))
    proc = menubar.open_app()
    assert proc is not None
    deadline = time.monotonic() + 10
    while proc.returncode is None:  # set by the waiting thread
        assert time.monotonic() < deadline
        time.sleep(0.02)
    with pytest.raises(ChildProcessError):
        os.waitpid(proc.pid, os.WNOHANG)  # already reaped


def test_close_after_ends_it() -> None:
    bar = Bar(close_after=0.0)
    assert bar.ending() and bar.done.is_set()
    assert not Bar(close_after=60).ending()


# -- the command -------------------------------------------------------------------------


def test_the_command_runs_the_item_and_prints_on_changes(no_cocoa: list[Bar]) -> None:
    result = invoke("--print", "--close-after", "5")

    assert result.exit_code == 0, result.output
    (bar,) = no_cocoa
    assert bar.close_after == 5.0 and bar.echo is not None
    bar.read = lambda: Counts(1, ())
    bar.read_once()
    bar.pick_up()
    assert invoke().exit_code == 0 and no_cocoa[1].echo is None


def test_one_item_per_home(no_cocoa: list[Bar]) -> None:
    lock = window.single_instance("menubar.lock")
    assert lock is not None
    try:
        result = invoke()
    finally:
        lock.close()
    assert result.exit_code == 0, result.output
    assert result.output == "the menu-bar item of this home is already running\n"
    assert no_cocoa == []
    assert invoke().exit_code == 0 and len(no_cocoa) == 1  # free again


def test_sigterm_ends_it_with_its_code(monkeypatch: pytest.MonkeyPatch) -> None:
    """The signal reaches it while the (here faked) Cocoa loop holds the main thread."""

    def loop(bar: Bar, cocoa: dict) -> None:
        threading.Timer(0.3, os.kill, (os.getpid(), signal.SIGTERM)).start()
        assert bar.done.wait(10)

    monkeypatch.setattr(menubar, "show_item", loop)
    before = signal.getsignal(signal.SIGTERM)
    result = invoke()
    assert result.exit_code == 128 + signal.SIGTERM, result.output
    assert signal.getsignal(signal.SIGTERM) is before  # restored


@pytest.mark.parametrize("how", ["close-after", "sigterm", "quit"])
def test_a_stuck_read_holds_up_nothing(how: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """A read that waits on the database (a writer holds it) blocks only the reading thread:
    the main thread still shows the item and ends at once."""
    entered, release = threading.Event(), threading.Event()

    def stuck() -> Counts:
        entered.set()
        release.wait(60)
        return Counts(9, ())

    updates: list[float] = []

    def loop(bar: Bar, cocoa: dict) -> None:  # what Cocoa's timer does, every TICK
        assert entered.wait(10)  # the read is stuck from here on
        if how == "sigterm":
            threading.Timer(0.2, os.kill, (os.getpid(), signal.SIGTERM)).start()
        while not bar.tick(lambda: updates.append(time.monotonic())):
            if how == "quit":
                bar.click(Entry("Quit", QUIT))
            time.sleep(0.05)

    monkeypatch.setattr(menubar, "FacadeReader", lambda: stuck)
    monkeypatch.setattr(menubar, "show_item", loop)
    start = time.monotonic()
    try:
        result = invoke("--print", *(["--close-after", "0.5"] if how == "close-after" else []))
        took = time.monotonic() - start
    finally:
        release.set()

    expected = 128 + signal.SIGTERM if how == "sigterm" else 0
    assert result.exit_code == expected, result.output
    assert took < 3
    assert len(updates) == 1 and updates[0] - start < 2  # shown at once, with zeros
    assert result.stdout == ""  # nothing was read, so nothing printed


def test_without_the_extra_it_names_it(monkeypatch: pytest.MonkeyPatch, no_cocoa: list) -> None:
    monkeypatch.setattr(menubar, "load_cocoa", REAL_LOAD_COCOA)
    for name in ("AppKit", "Foundation"):
        monkeypatch.setitem(sys.modules, name, None)  # `import AppKit` raises ImportError

    result = invoke()

    assert result.exit_code == 1
    assert result.stderr == (
        "error: pyobjc-framework-Cocoa is not installed (install the extra orchestrator[menubar])\n"
    )
    assert no_cocoa == []


def test_off_macos_one_line_and_no_gui_import(tmp_path: Path) -> None:
    script = (
        "import json, sys\n"
        "from orchestrator import cli\n"
        "from orchestrator.gui import menubar\n"
        "menubar.this_platform = lambda: 'linux'\n"
        "try:\n"
        "    cli.app(['menubar'], prog_name=cli.config.CLI_NAME)\n"
        "except SystemExit as exc:\n"
        "    code = exc.code\n"
        f"loaded = [m for m in {GUI_MODULES!r} if m in sys.modules]\n"
        "print(json.dumps([code, loaded]))\n"
    )
    done = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, timeout=60, check=False
    )

    assert json.loads(done.stdout) == [1, []]
    assert done.stderr == "error: orch menubar runs on macOS only\n"
