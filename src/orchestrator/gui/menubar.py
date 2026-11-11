"""`orch menubar`: a status item in the macOS menu bar.

Its title shows the running agents and the open decisions of this home ("♪ 2 · 1"; "♪" alone
when both are zero). Its menu: those two counts, the open decisions (a click opens one in the
running dashboard, else `orch app`), "Open Spartito" (`orch app`, detached) and "Quit".

The counts are read through the facade in this process every READ_EVERY seconds; a failed
read keeps the last values. Everything but `show_item` is plain Python; `show_item` alone
imports Cocoa (pyobjc-framework-Cocoa, the extra `orchestrator[menubar]`). One item per home
(`run/menubar.lock`); SIGTERM, SIGHUP and SIGINT end it as "Quit" does.
"""

from __future__ import annotations

import signal
import subprocess
import sys
import threading
import time
import webbrowser
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from .. import config
from . import locate, window

GLYPH = "♪"
READ_EVERY = 5.0  # seconds between two reads of the counts
TICK = 0.25  # seconds between two looks at a stop signal and `close_after`
QUESTION_WIDTH = 60  # characters of a question shown in the menu
SHOWN_DECISIONS = 10
INSTALL_HINT = "install the extra orchestrator[menubar]"
STOP_SIGNALS = (signal.SIGTERM, signal.SIGHUP, signal.SIGINT)
APP_ARGV = (sys.executable, "-m", "orchestrator", "app")
# What a menu entry does when clicked; None: it only informs.
OPEN, DECISION, QUIT = "open", "decision", "quit"


class MenubarError(Exception):
    """A clear one-line reason `orch menubar` cannot run."""


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


def read_counts() -> Counts:
    """The counts of this home, through the facade."""
    from ..service import Orchestrator

    orch = Orchestrator()
    return Counts(
        orch.running_count(),
        tuple((d["id"], d["question"]) for d in orch.list_decisions()),
    )


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


def open_app() -> None:
    """Start `orch app` for this home as a detached process of its own."""
    try:
        subprocess.Popen(
            APP_ARGV,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except OSError as exc:
        say(f"orch app did not start: {exc.strerror or exc}")


def open_decision(decision_id: int) -> None:
    """Show a decision: in the default browser when this home's dashboard server runs,
    otherwise in `orch app`."""
    url = locate.running_server()
    if url:
        try:
            if webbrowser.open(f"{url}#decision:{decision_id}"):
                return
        except webbrowser.Error:
            pass
    open_app()


@dataclass
class Bar:
    """The state of one `orch menubar`, without Cocoa: the last counts and when it ends."""

    read: Callable[[], Counts] = read_counts
    close_after: float | None = None
    echo: Callable[[str], None] | None = None  # `--print`: gets the lines whenever they change
    counts: Counts | None = None  # None until a read worked
    done: threading.Event = field(default_factory=threading.Event)
    signal: int | None = None  # the signal that ended it
    started: float = field(default_factory=time.monotonic)
    last_read: float | None = None
    failed: bool = False  # the last read failed (said once until one works again)

    def shown(self) -> Counts:
        return self.counts or Counts()

    def refresh(self) -> bool:
        """Read the counts; True when they changed (or were read for the first time). A failed
        read keeps the last ones."""
        self.last_read = time.monotonic()
        try:
            counts = self.read()
        except Exception as exc:
            if not self.failed:
                say(f"cannot read the counts, keeping the last ones: {window.first_line(exc)}")
            self.failed = True
            return False
        self.failed = False
        if counts == self.counts:
            return False
        self.counts = counts
        if self.echo:
            self.echo("\n".join(lines(counts)))
        return True

    def due(self) -> bool:
        return self.last_read is None or time.monotonic() - self.last_read >= READ_EVERY

    def ending(self) -> bool:
        if self.close_after is not None and time.monotonic() - self.started >= self.close_after:
            self.done.set()
        return self.done.is_set()

    def click(self, entry: Entry) -> None:
        if entry.action == OPEN:
            open_app()
        elif entry.action == DECISION and entry.decision is not None:
            open_decision(entry.decision)
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
            try:
                if bar.due() and bar.refresh() or not shown:
                    update()
            except Exception as exc:  # never end the item over one bad update
                say(f"cannot update the menu: {window.first_line(exc)}")
            if bar.ending():
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
    """`orch menubar`: show the item until "Quit", a stop signal or `close_after`; the exit
    code (128 + the signal when one ended it). MenubarError when it cannot run here."""
    if this_platform() != "darwin":
        raise MenubarError("orch menubar runs on macOS only")
    try:
        cocoa = load_cocoa()
    except ImportError:
        raise MenubarError(f"pyobjc-framework-Cocoa is not installed ({INSTALL_HINT})") from None
    lock = window.single_instance("menubar.lock")
    if lock is None:
        print("the menu-bar item of this home is already running")
        return 0
    bar = Bar(close_after=close_after, echo=echo)
    try:
        with window.stopped_by_signals(bar, STOP_SIGNALS):
            show_item(bar, cocoa)
    finally:
        lock.close()
    return 128 + bar.signal if bar.signal else 0
