"""Desktop-notification notices (see desktop.py) for a dashboard page and for Spartito's own
notifications on macOS (gui/notifier.py): what they show with a click action, where they
start, how one is taken, and how a page says it is there."""

from __future__ import annotations

from typing import Any, Protocol

from .. import desktop
from ..store import Store


class Facade(Protocol):
    """What these functions use of the facade (they are its methods)."""

    store: Store


def notices(orch: Facade, after: int = 0) -> list[dict[str, Any]]:
    """The unclaimed notices with an id above `after`, oldest first, each as
    {id, kind ('decision' | 'finished'), agent_id, decision_id (None for 'finished'), text,
    created_at}."""
    return orch.store.notices(after)


def notice_cursor(orch: Facade) -> int:
    """The highest notice id so far (0: none); a page starting here replays nothing older."""
    return orch.store.notice_cursor()


def claim_notice(orch: Facade, notice_id: int) -> bool:
    """Take a notice to show it; True only for the first caller (a page, Spartito's window or
    posting process, the Linux helper or osascript)."""
    return orch.store.claim_notice(notice_id)


def hand_over_notice(orch: Facade, notice: dict[str, Any]) -> None:
    """A notice Spartito's window took but did not show, or left unclaimed, as it closes: shown
    as if no window were open (desktop.without_window), still only once."""
    orch.store.release_notice(notice["id"])
    desktop.without_window(orch.store, notice["id"], str(notice["text"]), notice.get("decision_id"))


def page_present(orch: Facade) -> None:
    """A page able to show notifications is polling: osascript leaves notices to it for
    desktop.PAGE_FRESH_SECONDS."""
    desktop.page_present()
