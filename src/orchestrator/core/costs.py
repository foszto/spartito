"""Cost reports and budgets, from the recorded usage (table run_usage, one row per run that
reported usage) and what deleted agents had spent (spent_removed).

The report groups the recorded usage by local-time day, by project, agent, model or role, and
gives the spend per day and group over its range for the dashboard's chart. Runs recorded
by code from before usage records have no record, so the report can be slightly below the
total the dashboard shows: it is "recorded usage". Codex reports no cost: its turns count with
their tokens, and a row none of whose turns reported a cost has no cost (None, shown as n/a),
never $0.00. What deleted agents had spent is kept per agent and project, without the dates of
its turns: it has its own rows (per project, per agent, or one undated row by day) and is left
out when a start or end day is asked for.

A budget is global or for one project, over a local-time day, month or all time ('total'). It
counts reported cost only; 'total' includes deleted agents' spend, a day or month does not
(it has no dates). Cost is known only once a turn has ended, and turns run in parallel, so a
budget is soft: it is checked after each turn whose usage was recorded (`usage_recorded`), and
the owner is warned once per budget and period at the threshold and once more at the amount,
through the desktop notifications (desktop.show). The hard stop is opt-in: with it, a budget
whose amount is reached refuses new message turns before they run (`refusal`, called by the
runner); a running turn is never stopped, and notification and compaction turns still run.
"""

from __future__ import annotations

import math
from datetime import UTC, date, datetime, time, timedelta
from typing import Any, Protocol

from .. import config, desktop
from ..errors import OrchestratorError
from ..models import TOKEN_FIELDS
from ..store import Store

GROUPS = ("project", "day", "agent", "model", "role")  # `--by`; the first is the default
PERIODS = ("day", "month", "total")
DEFAULT_PERIOD = "month"
DEFAULT_WARN_AT = 80  # percent
GLOBAL = ""  # the scope of the global budget
WARNED, REACHED = 1, 2  # announced levels
REMOVED_LABEL = "removed agents"
UNKNOWN_MODEL = "unknown"  # a usage record without a model
REMOVED_ROLE = "removed"  # a role not recorded: a deleted agent's
PERIOD_TEXT = {"day": "today", "month": "this month", "total": "in total"}
MAX_RANGE_DAYS = 3660  # the longest range a report covers (about ten years): one chart bar a day


class Facade(Protocol):
    """What these functions use of the facade (they are its methods)."""

    store: Store


# -- local time -------------------------------------------------------------------------------


def local_day(created_at: str) -> str:
    """The local-time day (YYYY-MM-DD) of a stored UTC time; one without a zone is UTC."""
    moment = datetime.fromisoformat(created_at)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment.astimezone().date().isoformat()


def utc_start(day: date) -> str:
    """The UTC time, as the store writes it, at which local day `day` begins."""
    begins = datetime.combine(day, time()).astimezone()  # the local zone, its offset that day
    return begins.astimezone(UTC).isoformat(timespec="seconds")


def parse_day(text: str) -> date:
    try:
        return date.fromisoformat(text.strip())
    except ValueError:
        raise OrchestratorError(f"not a date: {text!r} (expected YYYY-MM-DD)") from None


def _bound(day: date, next_day: bool = False) -> str:
    """utc_start of `day` (or of the day after it) for a report's range; a day too close to
    the edge of the calendar to convert is an error."""
    try:
        return utc_start(day + timedelta(days=1) if next_day else day)
    except OverflowError, ValueError, OSError:
        raise OrchestratorError(
            f"out of range: {day} (too close to the edge of the calendar)"
        ) from None


def current_period(period: str, today: date | None = None) -> tuple[str, date | None]:
    """The key of the period that holds `today` (local; default: now) and its first day
    (None: 'total', which has no start)."""
    today = today or datetime.now().astimezone().date()
    if period == "day":
        return today.isoformat(), today
    if period == "month":
        return f"{today.year:04d}-{today.month:02d}", today.replace(day=1)
    return "total", None


# -- the report -------------------------------------------------------------------------------


def _row(key: str | None, label: str, removed: bool = False) -> dict[str, Any]:
    return {
        "key": key,
        "label": label,
        "project": None,
        "removed": removed,
        "turns": 0,
        "unpriced": 0,  # turns without a reported cost
        "tokens": dict.fromkeys(("in", "out", "cache_read", "cache_write"), 0),
        "cost_usd": None,  # None: no turn of the row reported a cost
    }


def _add(row: dict[str, Any], record: dict[str, Any], turns: int | None) -> None:
    """Add a usage record (turns 1) or a deleted agent's sums (turns None: not known)."""
    if turns is None:
        row["turns"] = None
    elif row["turns"] is not None:
        row["turns"] += turns
    for short, key in zip(row["tokens"], TOKEN_FIELDS, strict=True):
        row["tokens"][short] += record.get(key) or 0
    cost = record.get("cost_usd")
    if cost is None:
        row["unpriced"] += turns or 0
    else:
        row["cost_usd"] = (row["cost_usd"] or 0) + cost


def _project_label(project: str | None) -> str:
    return project if project is not None else f"{config.ORCHESTRATOR_NAME} (orchestrator)"


def _cell() -> dict[str, Any]:
    """One day's spend of one group in the series."""
    return {
        "turns": 0,
        "unpriced": 0,
        "tokens": dict.fromkeys(("in", "out", "cache_read", "cache_write"), 0),
        "cost_usd": None,
    }


def _group(by: str, record: dict[str, Any]) -> tuple[Any, str]:
    """The key and label of a usage record's row when grouped `by`."""
    if by == "day":
        day = local_day(record["created_at"])
        return day, day
    if by == "project":
        return record["project"], _project_label(record["project"])
    if by == "model":
        return record["model"], record["model"] or UNKNOWN_MODEL
    if by == "role":
        return record["role"], record["role"] or REMOVED_ROLE
    return record["agent_id"], record["agent_id"]


def _removed_group(by: str, spend: dict[str, Any]) -> tuple[Any, str]:
    """The key and label of a deleted agent's row when grouped `by` (its model and role are
    not recorded)."""
    if by == "day":
        return None, f"{REMOVED_LABEL} (not dated)"
    if by == "project":
        return spend["project"], f"{_project_label(spend['project'])}: {REMOVED_LABEL}"
    if by == "model":
        return None, f"{REMOVED_LABEL} (model not recorded)"
    if by == "role":
        return None, REMOVED_ROLE
    return spend["agent_id"], f"{spend['agent_id']} (removed)"


def _days(first: date, last: date) -> list[str]:
    return [date.fromordinal(n).isoformat() for n in range(first.toordinal(), last.toordinal() + 1)]


def report(
    orch: Facade,
    by: str = GROUPS[0],
    since: str | None = None,
    project: str | None = None,
    until: str | None = None,
) -> dict[str, Any]:
    """The recorded spend grouped `by` 'project', 'day' (local time), 'agent', 'model' (as
    the usage record names it; 'unknown': none) or 'role' (the agent's), from the local day
    `since` (YYYY-MM-DD) on and up to the local day `until` included, of one `project`
    (None: everything, the orchestrator too). Returns {by, since, until, project, rows,
    total, removed_left_out, unpriced, days, series}: each row (and the total) as {key,
    label, project, removed, turns (None: not known), unpriced, tokens {in, out, cache_read,
    cache_write}, cost_usd (None: none reported)}, rounded to cents only when shown. Deleted
    agents' spend has rows of their own (removed: true); with `since` or `until` it is left
    out (removed_left_out: true), as it has no dates. `days` is every local day of the range
    (without `since`: from the first record; without `until`: to today or the last record),
    and `series` the dated spend per day and row label: {day: {label: {turns, unpriced,
    tokens, cost_usd}}}, only the days and groups with records. A range of more than
    MAX_RANGE_DAYS days, or with a day too close to the edge of the calendar, is refused."""
    if by not in GROUPS:
        raise OrchestratorError(f"unknown grouping {by!r} (choose from {', '.join(GROUPS)})")
    start = parse_day(since) if since else None
    end = parse_day(until) if until else None
    if start and end and end < start:
        raise OrchestratorError(f"the end day {end} is before the start day {start}")
    records = orch.store.usage_records(
        _bound(start) if start else None, project, _bound(end, next_day=True) if end else None
    )
    rows: dict[Any, dict[str, Any]] = {}
    series: dict[str, dict[str, dict[str, Any]]] = {}
    for record in records:
        key, label = _group(by, record)
        row = rows.setdefault(("", key), _row(key, label))
        row["project"] = record["project"]
        _add(row, record, 1)
        day = local_day(record["created_at"])
        _add(series.setdefault(day, {}).setdefault(label, _cell()), record, 1)
    dated = bool(start or end)
    removed = [] if dated else orch.store.removed_spend(project)
    for spend in removed:
        key, label = _removed_group(by, spend)
        row = rows.setdefault(("removed", key), _row(key, label, removed=True))
        row["project"] = spend["project"]
        _add(row, spend, None)

    def order(item: tuple[Any, dict[str, Any]]) -> tuple:
        (kind, key), row = item
        if by == "project":  # each project's removed agents right after it; orchestrator first
            return (key is not None, key or "", kind)
        if by == "agent":
            return (row["project"] is not None, row["project"] or "", kind, key or "")
        return (kind, key or "")

    listed = [row for _, row in sorted(rows.items(), key=order)]
    total = _row(None, "total")
    total["turns"] = sum(row["turns"] or 0 for row in listed)
    for row in listed:
        for short in total["tokens"]:
            total["tokens"][short] += row["tokens"][short]
        total["unpriced"] += row["unpriced"]
        if row["cost_usd"] is not None:
            total["cost_usd"] = (total["cost_usd"] or 0) + row["cost_usd"]
    first = start or (date.fromisoformat(min(series)) if series else None)
    last = end or max(
        [datetime.now().astimezone().date(), *(date.fromisoformat(day) for day in series)]
    )
    if first and (last - first).days + 1 > MAX_RANGE_DAYS:
        raise OrchestratorError(
            f"the range from {first} to {last} has {(last - first).days + 1:,} days, more than"
            f" {MAX_RANGE_DAYS:,} (about ten years): narrow the range with a later start"
            " day or an earlier end day"
        )
    return {
        "by": by,
        "since": start.isoformat() if start else None,
        "until": end.isoformat() if end else None,
        "project": project,
        "rows": listed,
        "total": total,
        "removed_left_out": dated,
        "unpriced": total["unpriced"],
        "days": _days(first, last) if first else [],
        "series": series,
    }


# -- budgets ----------------------------------------------------------------------------------


def scope_text(scope: str) -> str:
    return "global" if scope == GLOBAL else f"project {scope}"


def _status(store: Store, budget: dict[str, Any], today: date | None = None) -> dict[str, Any]:
    """A budget row with what is spent in its current period: spent_usd, percent (rounded
    down), state ('ok' | 'warning' | 'reached'), period_key, period_start (local day; None:
    total), announced (the level announced in this period: 0, 1 or 2)."""
    key, start = current_period(budget["period"], today)
    scope = budget["scope"]
    spent = store.spent_since(
        utc_start(start) if start else None, scope or None, removed=start is None
    )
    amount = budget["amount_usd"]
    ratio = spent / amount if amount > 0 else math.inf
    if ratio >= 1:
        state = "reached"
    elif ratio * 100 >= budget["warn_at"]:
        state = "warning"
    else:
        state = "ok"
    announced = budget["announced_level"] if budget["announced_period"] == key else 0
    return {
        "scope": scope,
        "project": scope or None,
        "amount_usd": amount,
        "period": budget["period"],
        "warn_at": budget["warn_at"],
        "hard_stop": bool(budget["hard_stop"]),
        "spent_usd": spent,
        "percent": math.floor(ratio * 100) if math.isfinite(ratio) else None,
        "state": state,
        "period_key": key,
        "period_start": start.isoformat() if start else None,
        "announced": announced,
    }


def budget_list(orch: Facade) -> list[dict[str, Any]]:
    """Every budget with what is spent in its current period (see `_status`)."""
    return [_status(orch.store, budget) for budget in orch.store.budgets()]


def set_budget(
    orch: Facade,
    amount_usd: float | None,
    project: str | None = None,
    period: str | None = None,
    warn_at: int | None = None,
    hard_stop: bool | None = None,
) -> dict[str, Any]:
    """Create or change the global budget (`project` None) or a project's. A value left None
    keeps the budget's current one; a new budget defaults to a month, a warning at 80 % and
    no hard stop. Returns its status."""
    if amount_usd is not None and not (math.isfinite(amount_usd) and amount_usd > 0):
        raise OrchestratorError("the amount must be a positive number of USD")
    if period is not None and period not in PERIODS:
        raise OrchestratorError(f"unknown period {period!r} (choose from {', '.join(PERIODS)})")
    if warn_at is not None and not 1 <= warn_at <= 100:
        raise OrchestratorError("the warning threshold is a percentage from 1 to 100")
    if project is not None and orch.store.get_project(project) is None:
        raise OrchestratorError(f"unknown project '{project}'")
    defaults = {
        "amount_usd": None,
        "period": DEFAULT_PERIOD,
        "warn_at": DEFAULT_WARN_AT,
        "hard_stop": 0,
    }
    scope = project or GLOBAL
    try:
        row = orch.store.set_budget(scope, amount_usd, period, warn_at, hard_stop, defaults)
    except ValueError:
        raise OrchestratorError(
            f"there is no {scope_text(scope)} budget yet: give an amount"
        ) from None
    return _status(orch.store, row)


def remove_budget(orch: Facade, project: str | None = None) -> None:
    """Remove the global budget (`project` None) or a project's; a project no longer
    registered may still have one."""
    scope = project or GLOBAL
    if not orch.store.remove_budget(scope):
        raise OrchestratorError(f"there is no {scope_text(scope)} budget")


def _in_scope(store: Store, project: str | None) -> list[dict[str, Any]]:
    """The budgets that count an agent of `project` (None: the orchestrator): the global one
    and its project's."""
    scopes = {GLOBAL, project} if project else {GLOBAL}
    return [budget for budget in store.budgets() if budget["scope"] in scopes]


def _money(value: float) -> str:
    return f"${value:,.2f}"


def _figures(status: dict[str, Any]) -> str:
    percent = "" if status["percent"] is None else f" ({status['percent']} %)"
    when = PERIOD_TEXT[status["period"]]
    return f"{_money(status['spent_usd'])} of {_money(status['amount_usd'])} {when}{percent}"


def warning_text(status: dict[str, Any], level: int) -> str:
    """The text of a budget warning; at the amount it says what the hard stop does."""
    scope = scope_text(status["scope"])
    if level == WARNED:
        return f"Budget warning, {scope}: {_figures(status)}"
    tail = (
        "; new turns are refused (hard stop)"
        if status["hard_stop"]
        else "; turns go on (no hard stop)"
    )
    return f"Budget reached, {scope}: {_figures(status)}{tail}"


def usage_recorded(store: Store, agent_id: str) -> list[str]:
    """After a turn of `agent_id` recorded a cost: warn the owner of each budget counting it
    whose current period reached its threshold (level 1) or its amount (level 2) for the
    first time, once per budget, period and level across all processes
    (Store.raise_announced). A jump past both at once announces only the amount. Returns the
    texts announced. Warnings go to the desktop notifications (desktop.show, a notice about
    the agent; switched off with them)."""
    agent = store.get_agent(agent_id)
    if agent is None:
        return []
    announced = []
    for budget in _in_scope(store, agent.project):
        status = _status(store, budget)
        level = {"reached": REACHED, "warning": WARNED}.get(status["state"], 0)
        if level and store.raise_announced(budget, status["period_key"], level):
            text = warning_text(status, level)
            desktop.show(store, text, agent_id)
            announced.append(text)
    return announced


def refusal(store: Store, project: str | None) -> str | None:
    """Why a new message turn of an agent of `project` (None: the orchestrator) is refused:
    a budget counting it has the hard stop on and its amount is reached in its current
    period. None: it may run (no such budget, the hard stop off, or below the amount).

    The text becomes the run's error, which reaches the parent agent: it names the budget
    but not the commands that lift it, since changing a budget is the owner's decision (the
    owner finds them in `budget list`, the docs and the dashboard)."""
    for budget in _in_scope(store, project):
        if not budget["hard_stop"]:
            continue
        status = _status(store, budget)
        if status["state"] != "reached":
            continue
        return (
            f"refused by the hard stop of the {scope_text(budget['scope'])} budget, which is"
            f" reached: {_figures(status)}. Changing a budget is the owner's decision, not an"
            ' agent\'s (see "Budgets" in docs/configuration.md).'
        )
    return None
