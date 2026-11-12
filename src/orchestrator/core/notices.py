"""Desktop-notification notices (see desktop.py) for a dashboard page and for Spartito's own
notifications on macOS (gui/notifier.py): what they show with a click action, where they
start, how one is taken (only as desktop.route left it: desktop.may_take), what of them is worth
showing (`worth_showing`: decisions still open, all fresh notices, a bounded count of those
that waited; one rule for the window, the launcher's posting process and a page), how a page says it is there, and the trace of each
(`notice_trace`, for `spartito notices`)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Protocol

from .. import decisions, desktop
from ..store import Store

PAGE_MAX_AGE_SECONDS = 600.0  # a page skips older notices (app.js NOTICE_MAX_AGE_MS)
TAKERS = (desktop.WINDOW, desktop.HELPER_APP, desktop.PAGE, desktop.BANNER)
SHOW_AT_MOST = 3  # finished notices that waited: a taker shows the newest this many one by one


class Facade(Protocol):
    """What these functions use of the facade (they are its methods)."""

    store: Store


def notices(orch: Facade, after: int = 0) -> list[dict[str, Any]]:
    """The unclaimed notices with an id above `after`, oldest first, each as
    {id, kind ('decision' | 'finished'), agent_id, decision_id (None for 'finished'), text,
    created_at, route (whom desktop.route left it to; None: older code)}."""
    return orch.store.notices(after)


def age_of(notice: dict[str, Any]) -> float | None:
    try:
        created = datetime.fromisoformat(str(notice["created_at"]))
    except ValueError:
        return None
    if created.tzinfo is None:
        created = created.replace(tzinfo=UTC)
    return (datetime.now(UTC) - created).total_seconds()


def page_notices(orch: Facade, after: int) -> list[dict[str, Any]]:
    """What a polling page may take, oldest first, as `worth_showing` keeps it: the notices
    above `after` it may take, and those left to waiting or to a page from before `after`
    (escalated after the page passed them) younger than PAGE_MAX_AGE_SECONDS. Spooled
    notices are recorded first."""
    desktop.ingest_spool(orch.store)
    found = []
    for notice in orch.store.notices(0):
        route = notice.get("route")
        if not desktop.may_take(desktop.PAGE, route):
            continue
        late = route in (desktop.WAITING, desktop.PAGE)
        if notice["id"] > after or (late and (age_of(notice) or 0) < PAGE_MAX_AGE_SECONDS):
            found.append(notice)
    return worth_showing(orch, found, desktop.PAGE)[0]


def notice_cursor(orch: Facade) -> int:
    """The highest notice id so far (0: none); a page starting here replays nothing older."""
    return orch.store.notice_cursor()


def claim_notice(orch: Facade, notice_id: int, taker: str = desktop.PAGE, detail: str = "") -> bool:
    """Take a notice to show it, as `taker` (a page; Spartito's window: 'window' or 'banner';
    its posting process: 'helper'); True only for the first caller, and only when the notice
    is left to this taker or a later one (desktop.may_take)."""
    if taker not in TAKERS:
        raise ValueError(f"unknown taker: {taker}")
    return desktop.claim(orch.store, notice_id, taker, detail=detail, who=desktop.actor(taker))


def notice_step(
    orch: Facade, notice_id: int, kind: str, path: str | None, detail: str, taker: str
) -> None:
    """Trace a step a taker took with a notice (shown, a fallback)."""
    desktop.note(orch.store, notice_id, desktop.step(kind, path, detail, desktop.actor(taker)))


def release_notice(orch: Facade, notice_id: int, why: str, taker: str) -> bool:
    """A notice `taker` claimed but could not show: it waits again (why, in the trace)."""
    return desktop.release_to_wait(orch.store, notice_id, why, desktop.actor(taker))


def leave_waiting(orch: Facade, notice_id: int, why: str, taker: str) -> bool:
    """A notice left to `taker` that it cannot take now: it waits (why, in the trace)."""
    item = desktop.step("release", desktop.PATHS[desktop.WAITING], why, desktop.actor(taker))
    if orch.store.reroute_notice(notice_id, desktop.WAITING, item, was=(taker,)):
        desktop.logged(notice_id, [item])
        return True
    return False


def hand_over_notice(orch: Facade, notice: dict[str, Any], taker: str = desktop.WINDOW) -> None:
    """A notice Spartito's window leaves as it closes: one it claimed but did not get shown is
    released (only its own claim); one still left to it is routed again without the window
    (desktop.redirect). A notice another took meanwhile stays as it is: shown once."""
    desktop.redirect(
        orch.store,
        notice,
        "the window closed without showing it",
        skip=(desktop.WINDOW, desktop.BANNER),
        who=desktop.actor(taker),
    )


def waited(notice: dict[str, Any]) -> bool:
    """A notice that waited: its recorded route is `waiting` (nobody could show it when it was
    decided, or a taker gave it back); one routed to a taker is fresh, however old."""
    return notice.get("route") == desktop.WAITING


def worth_showing(
    orch: Facade, found: list[dict[str, Any]], taker: str
) -> tuple[list[dict[str, Any]], int]:
    """The one rule for what a taker shows of the notices it may take (`found`, oldest first),
    for Spartito's window, the launcher's posting process and a page alike. Every fresh
    notice is kept. Of those that `waited`: a decision notice is kept while its decision is
    open (never cut), else dropped; of the finished ones only the newest SHOW_AT_MOST are
    kept, the older ones are claimed here and counted: in the window's count banner when
    `taker` is the window (their number: the second value), else in the text of the newest
    one kept ("(<n> more waited) …"). Kept: the ones for `taker` to claim and show, oldest
    first. Each one claimed here is traced with its reason."""
    keep = []
    for notice in found:
        number = notice.get("decision_id")
        if waited(notice) and number is not None:
            decision = decisions.get(orch.store, number)
            state = decision.state if decision is not None else "gone"
        else:
            state = decisions.OPEN
        if state != decisions.OPEN:
            settle(orch, notice, taker, "drop", desktop.DROPPED, f"decision #{number} is {state}")
            continue
        keep.append(notice)
    backlog = [n for n in keep if waited(n) and n.get("decision_id") is None]
    older, newer = backlog[:-SHOW_AT_MOST], backlog[-SHOW_AT_MOST:]
    window = taker in (desktop.WINDOW, desktop.BANNER)
    counted = 0
    for notice in older:
        if window:
            why = (
                f"waited for a window; counted in its banner (the newest {SHOW_AT_MOST} finished"
                " notices that waited are shown one by one)"
            )
            counted += settle(orch, notice, taker, "shown", desktop.PATHS[desktop.BANNER], why)
        else:
            why = (
                f"waited; only the newest {SHOW_AT_MOST} finished notices that waited are shown,"
                f" this one is counted in the text of #{newer[-1]['id']}"
            )
            counted += settle(orch, notice, taker, "drop", desktop.DROPPED, why)
    cut = {notice["id"] for notice in older}
    keep = [notice for notice in keep if notice["id"] not in cut]
    if counted and not window:
        last = newer[-1]["id"]
        keep = [
            {**n, "text": f"({counted} more waited) {n['text']}"} if n["id"] == last else n
            for n in keep
        ]
    return keep, counted


def settle(
    orch: Facade, notice: dict[str, Any], taker: str, kind: str, path: str, why: str
) -> bool:
    """Claim a notice `taker` does not show one by one, with the step that says so."""
    item = desktop.step(kind, path, why, desktop.actor(taker))
    routes = desktop.takeable(taker)
    if orch.store.claim_notice(notice["id"], routes=routes, by=item.actor, step=item):
        desktop.logged(notice["id"], [item])
        return True
    return False


def notice_backlog(orch: Facade, before: int, taker: str) -> tuple[list[dict[str, Any]], int]:
    """The notices up to id `before` still unclaimed as Spartito's window opens (they waited
    for it) that `taker` may take, through `worth_showing`: those to show, oldest first, and
    how many were counted instead."""
    found = [
        notice
        for notice in orch.store.notices(0)
        if notice["id"] <= before and desktop.may_take(taker, notice.get("route"))
    ]
    return worth_showing(orch, found, taker)


def ingest_notices(orch: Facade) -> list[int]:
    """Record the notices a busy store left in the spool (desktop.ingest_spool)."""
    return desktop.ingest_spool(orch.store)


def notice_trace(orch: Facade, limit: int = 20) -> list[dict[str, Any]]:
    """The newest `limit` notices, newest first, each with its `steps` and a `summary`
    (desktop.summary: path, actor, at, reason)."""
    found = orch.store.notice_trace(limit)
    for notice in found:
        notice["summary"] = desktop.summary(notice)
    return found


def page_present(orch: Facade) -> None:
    """A page able to show notifications is polling: desktop.route counts it present for
    desktop.PAGE_FRESH_SECONDS."""
    desktop.page_present()
