"""Desktop-notification notices for a dashboard page (see desktop.py): what it shows with a
click action, where it starts, how it takes one, and how it says it is there."""

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
    """Take a notice to show it; True only for the first caller (a page or osascript)."""
    return orch.store.claim_notice(notice_id)


def page_present(orch: Facade) -> None:
    """A page able to show notifications is polling: osascript leaves notices to it for
    desktop.PAGE_FRESH_SECONDS."""
    desktop.page_present()
