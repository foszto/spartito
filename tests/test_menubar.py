"""`orch menubar`. Cocoa is never loaded: `show_item` is faked wherever `run` gets that far,
and `subprocess.Popen`, `webbrowser.open` and `locate.running_server` are faked for clicks."""

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
    monkeypatch.setattr(menubar.subprocess, "Popen", lambda *a, **kw: calls.append((a, kw)))
    return calls


@pytest.fixture
def opened(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """URLs opened in the default browser (not really)."""
    urls: list[str] = []
    monkeypatch.setattr(menubar.webbrowser, "open", lambda url: urls.append(url) or True)
    return urls


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

    assert bar.refresh() is True
    assert bar.refresh() is False and bar.counts == Counts(2, decisions(1))
    assert bar.refresh() is False and bar.counts == Counts(2, decisions(1))
    assert bar.refresh() is False  # works again, nothing changed
    assert bar.refresh() is True and bar.counts == Counts(1, ())
    assert [text.splitlines()[0] for text in printed] == ["title: ♪ 2 · 1", "title: ♪ 1 · 0"]
    err = capsys.readouterr().err.splitlines()
    assert err == ["cannot read the counts, keeping the last ones: database is locked"]


def test_the_first_read_is_printed_even_when_all_is_zero() -> None:
    printed: list[str] = []
    bar = Bar(read=Counts, echo=printed.append)
    assert bar.refresh() is True
    assert printed == ["\n".join(menubar.lines(Counts()))]


def test_the_counts_come_from_the_facade(orch: Orchestrator, project: str) -> None:
    assert menubar.read_counts() == Counts(0, ())
    agent, _ = orch.spawn(project, "SLEEP 30", worktree=False)
    deadline = time.monotonic() + 20
    while orch.running_count() != 1:
        assert time.monotonic() < deadline
        time.sleep(0.05)
    first = orch.request_decision(agent.id, "Which database?")
    second = orch.request_decision(agent.id, "Ship it?")

    assert menubar.read_counts() == Counts(
        1, ((first.id, "Which database?"), (second.id, "Ship it?"))
    )

    orch.stop(agent.id)
    while orch.running_count():
        assert time.monotonic() < deadline
        time.sleep(0.05)
    orch.withdraw_decision(first.id, agent.id)
    assert menubar.read_counts() == Counts(0, ((second.id, "Ship it?"),))


# -- what a click does -------------------------------------------------------------------


def test_open_starts_a_detached_orch_app(launched: list, opened: list[str]) -> None:
    Bar().click(Entry("Open Spartito", OPEN))

    ((argv,), kwargs) = launched[0]
    assert list(argv) == [sys.executable, "-m", "orchestrator", "app"]
    assert kwargs["start_new_session"] is True and kwargs["stdin"] == subprocess.DEVNULL
    assert opened == []


def test_a_decision_opens_in_the_running_dashboard(
    launched: list, opened: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(menubar.locate, "running_server", lambda: "http://127.0.0.1:18431/")
    Bar().click(Entry("#4  Ship it?", DECISION, 4))
    assert opened == ["http://127.0.0.1:18431/#decision:4"]
    assert launched == []


def test_a_decision_without_a_dashboard_starts_orch_app(
    launched: list, opened: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(menubar.locate, "running_server", lambda: None)
    Bar().click(Entry("#4  Ship it?", DECISION, 4))
    assert opened == [] and len(launched) == 1


def test_a_decision_falls_back_to_orch_app_when_no_browser_opens(
    launched: list, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(menubar.locate, "running_server", lambda: "http://127.0.0.1:18431/")
    monkeypatch.setattr(menubar.webbrowser, "open", lambda url: False)
    Bar().click(Entry("#4  Ship it?", DECISION, 4))
    assert len(launched) == 1


def test_open_says_so_when_orch_app_cannot_start(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def refuse(*args, **kwargs):
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(menubar.subprocess, "Popen", refuse)
    Bar().click(Entry("Open Spartito", OPEN))
    assert capsys.readouterr().err == "orch app did not start: Permission denied\n"


def test_quit_ends_only_the_item(launched: list, opened: list[str]) -> None:
    bar = Bar()
    bar.click(Entry("Quit", QUIT))
    assert bar.done.is_set() and launched == [] and opened == []
    bar.click(Entry("Running agents: 0"))  # an information line does nothing


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
    bar.refresh()
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
        "    cli.app(['menubar'], prog_name='orch')\n"
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
