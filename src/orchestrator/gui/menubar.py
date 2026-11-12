"""`spartito menubar`: a status item in the macOS menu bar.

Its title shows the running agents and the open decisions of this home ("♪ 2 · 1"; "♪" alone
when both are zero). Its menu: those two counts, the open decisions (a click starts
`spartito app --decision <id>`), "Open Spartito" (`spartito app`) and "Quit". `spartito app` is started
detached; with its window open already, it brings that forward (at the decision).

The counts are read through the facade in this process every READ_EVERY seconds, in a
thread of their own: Cocoa's main thread never waits on the database (a writer may hold it
for long), it only shows the latest values. A failed read keeps the last ones. Everything but `show_item` is plain Python; `show_item` alone
imports Cocoa (pyobjc-framework-Cocoa, the extra `orchestrator[menubar]`). One item per home
(`run/menubar.lock`); SIGTERM, SIGHUP and SIGINT end it as "Quit" does.
"""

from __future__ import annotations

import signal
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from .. import config
from . import window

GLYPH = "♪"
READ_EVERY = 5.0  # seconds between two reads of the counts
TICK = 0.25  # seconds between two looks at new counts, a stop signal and `close_after`
QUESTION_WIDTH = 60  # characters of a question shown in the menu
SHOWN_DECISIONS = 10
INSTALL_HINT = "install the extra orchestrator[menubar]"
STOP_SIGNALS = (signal.SIGTERM, signal.SIGHUP, signal.SIGINT)
APP_ARGV = (sys.executable, "-m", "orchestrator", "app")
# What a menu entry does when clicked; None: it only informs.
OPEN, DECISION, QUIT = "open", "decision", "quit"


class MenubarError(Exception):
    """A clear one-line reason `spartito menubar` cannot run."""


def say(line: str) -> None:
    print(line, file=sys.stderr, flush=True)


def this_platform() -> str:
    return sys.platform


@dataclass(frozen=True)
class Counts:
    running: int = 0
    decisions: tuple[tuple[int, str], ...] = ()  # (id, question) of each open decision


@dataclass(frozen=True)
class Entry:
    """One line of the menu: a separator when `title` is empty."""

    title: str = ""
    action: str | None = None
    decision: int | None = None


SEPARATOR = Entry()


class FacadeReader:
    """Reads the counts of this home through one facade, made at the first read and again
    after a failed one."""

    def __init__(self) -> None:
        self.orch: Any = None

    def __call__(self) -> Counts:
        try:
            if self.orch is None:
                from ..service import Orchestrator

                self.orch = Orchestrator()
            return Counts(
                self.orch.running_count(),
                tuple((d["id"], d["question"]) for d in self.orch.list_decisions()),
            )
        except Exception:
            self.orch = None
            raise


def title(counts: Counts) -> str:
    if not counts.running and not counts.decisions:
        return GLYPH
    return f"{GLYPH} {counts.running} · {len(counts.decisions)}"


def cut(text: str, width: int = QUESTION_WIDTH) -> str:
    """`text` on one line, at most `width` characters (an ellipsis marks a cut)."""
    line = " ".join(text.split())
    return line if len(line) <= width else line[: width - 1].rstrip() + "…"


def menu(counts: Counts) -> list[Entry]:
    entries = [
        Entry(f"Running agents: {counts.running}"),
        Entry(f"Open decisions: {len(counts.decisions)}"),
    ]
    if counts.decisions:
        entries.append(SEPARATOR)
        for decision_id, question in counts.decisions[:SHOWN_DECISIONS]:
            entries.append(Entry(f"#{decision_id}  {cut(question)}", DECISION, decision_id))
        if (more := len(counts.decisions) - SHOWN_DECISIONS) > 0:
            entries.append(Entry(f"… and {more} more"))
    entries += [SEPARATOR, Entry(f"Open {config.PROJECT_NAME}", OPEN), Entry("Quit", QUIT)]
    return entries


def lines(counts: Counts) -> list[str]:
    """The title and the menu as text (what `--print` writes)."""
    return [f"title: {title(counts)}"] + [f"  {e.title or '---'}" for e in menu(counts)]


def open_app(decision: int | None = None) -> subprocess.Popen | None:
    """Start `spartito app` for this home (at `decision`) as a detached process of its own; a
    thread waits for its end, so it does not stay a zombie child."""
    argv = [*APP_ARGV, "--decision", str(decision)] if decision is not None else [*APP_ARGV]
    try:
        proc = subprocess.Popen(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except OSError as exc:
        say(f"{config.CLI_NAME} app did not start: {exc.strerror or exc}")
        return None
    threading.Thread(target=proc.wait, name="orch-menubar-reap", daemon=True).start()
    return proc


@dataclass
class Bar:
    """The state of one `spartito menubar`, without Cocoa: the counts read, the counts shown and
    when it ends. `read` runs only in the reading thread (`start_reading`); the rest on the
    main thread."""

    read: Callable[[], Counts] = field(default_factory=FacadeReader)
    close_after: float | None = None
    echo: Callable[[str], None] | None = None  # `--print`: gets the lines whenever they change
    latest: Counts | None = None  # the last counts read; None until a read worked
    counts: Counts | None = None  # the counts shown; None until a read was picked up
    updated: bool = False  # the item was shown at least once
    done: threading.Event = field(default_factory=threading.Event)
    signal: int | None = None  # the signal that ended it
    started: float = field(default_factory=time.monotonic)
    failed: bool = False  # the last read failed (said once until one works again)

    def shown(self) -> Counts:
        return self.counts or Counts()

    def read_once(self) -> None:
        """Read the counts into `latest`. A failed read keeps the last ones."""
        try:
            counts = self.read()
        except Exception as exc:
            if not self.failed:
                say(f"cannot read the counts, keeping the last ones: {window.first_line(exc)}")
            self.failed = True
            return
        self.failed = False
        self.latest = counts

    def keep_reading(self) -> None:
        while not self.done.is_set():
            self.read_once()
            self.done.wait(READ_EVERY)

    def start_reading(self) -> threading.Thread:
        """The reading thread: a daemon, so a read stuck on the database never holds up the
        end of the process."""
        thread = threading.Thread(target=self.keep_reading, name="orch-menubar-read", daemon=True)
        thread.start()
        return thread

    def pick_up(self) -> bool:
        """Take the latest counts read; True when they differ from the shown ones."""
        latest = self.latest
        if latest is None or latest == self.counts:
            return False
        self.counts = latest
        if self.echo:
            self.echo("\n".join(lines(latest)))
        return True

    def tick(self, update: Callable[[], None]) -> bool:
        """One look on the main thread, without waiting on anything: show new counts with
        `update`; True when the item should end."""
        try:
            if self.pick_up() or not self.updated:
                self.updated = True
                update()
        except Exception as exc:  # never end the item over one bad update
            say(f"cannot update the menu: {window.first_line(exc)}")
        return self.ending()

    def ending(self) -> bool:
        if self.close_after is not None and time.monotonic() - self.started >= self.close_after:
            self.done.set()
        return self.done.is_set()

    def click(self, entry: Entry) -> None:
        if entry.action == OPEN:
            open_app()
        elif entry.action == DECISION and entry.decision is not None:
            open_app(entry.decision)
        elif entry.action == QUIT:
            self.done.set()


def load_cocoa() -> dict[str, Any]:
    """The Cocoa names `show_item` uses; ImportError without the extra."""
    import AppKit  # the optional extra: imported here and nowhere else
    import Foundation

    return {"AppKit": AppKit, "Foundation": Foundation}


def show_item(bar: Bar, cocoa: dict[str, Any]) -> None:
    """Show the status item and run Cocoa's loop on this (the main) thread until `bar` ends."""
    AppKit, Foundation = cocoa["AppKit"], cocoa["Foundation"]
    app = AppKit.NSApplication.sharedApplication()
    app.setActivationPolicy_(AppKit.NSApplicationActivationPolicyAccessory)  # no Dock icon
    item = AppKit.NSStatusBar.systemStatusBar().statusItemWithLength_(
        AppKit.NSVariableStatusItemLength
    )
    shown: list[Entry] = []

    class OrchMenubarTarget(Foundation.NSObject):
        def tick_(self, _timer) -> None:
            if bar.tick(update):
                app.stop_(None)  # noticed at the next event: post one
                app.postEvent_atStart_(wake_event(), True)

        def clicked_(self, sender) -> None:
            try:
                bar.click(shown[sender.tag()])
            except Exception as exc:
                say(f"cannot do that: {window.first_line(exc)}")
            if bar.done.is_set():
                self.tick_(None)

    target = OrchMenubarTarget.alloc().init()
    status_menu = AppKit.NSMenu.alloc().init()
    status_menu.setAutoenablesItems_(False)
    item.setMenu_(status_menu)

    def update() -> None:
        item.button().setTitle_(title(bar.shown()))
        status_menu.removeAllItems()
        shown[:] = menu(bar.shown())
        for index, entry in enumerate(shown):
            if entry == SEPARATOR:
                status_menu.addItem_(AppKit.NSMenuItem.separatorItem())
                continue
            line = AppKit.NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
                entry.title, "clicked:" if entry.action else None, ""
            )
            line.setTag_(index)
            line.setTarget_(target)
            line.setEnabled_(entry.action is not None)
            status_menu.addItem_(line)

    def wake_event():
        return AppKit.NSEvent.otherEventWithType_location_modifierFlags_timestamp_windowNumber_context_subtype_data1_data2_(
            AppKit.NSEventTypeApplicationDefined, (0, 0), 0, 0, 0, None, 0, 0, 0
        )

    timer = Foundation.NSTimer.timerWithTimeInterval_target_selector_userInfo_repeats_(
        TICK, target, "tick:", None, True
    )
    # Common modes: it also fires while the menu is open.
    Foundation.NSRunLoop.currentRunLoop().addTimer_forMode_(timer, Foundation.NSRunLoopCommonModes)
    target.tick_(None)
    try:
        app.run()
    finally:
        timer.invalidate()
        AppKit.NSStatusBar.systemStatusBar().removeStatusItem_(item)


def run(close_after: float | None = None, echo: Callable[[str], None] | None = None) -> int:
    """`spartito menubar`: show the item until "Quit", a stop signal or `close_after`; the exit
    code (128 + the signal when one ended it). MenubarError when it cannot run here."""
    if this_platform() != "darwin":
        raise MenubarError(f"{config.CLI_NAME} menubar runs on macOS only")
    try:
        cocoa = load_cocoa()
    except ImportError:
        raise MenubarError(f"pyobjc-framework-Cocoa is not installed ({INSTALL_HINT})") from None
    lock = window.single_instance("menubar.lock")
    if lock is None:
        print("the menu-bar item of this home is already running")
        return 0
    bar = Bar(FacadeReader(), close_after, echo)
    try:
        with window.stopped_by_signals(bar, STOP_SIGNALS):
            bar.start_reading()
            show_item(bar, cocoa)
    finally:
        bar.done.set()  # the reading thread stops after its current read
        lock.close()
    return 128 + bar.signal if bar.signal else 0
