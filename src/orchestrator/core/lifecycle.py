"""The end of a run: the steps that follow a turn, in their order, in one place.

Two entry points, because one step must come before the run is stored as ended and the
others after it:

- `turn_done`, for a turn that succeeded (not a compaction), called by its runner BEFORE it
  stores the run as done, while the turn still holds the agent, so no queued run can take
  the turn first: the automatic compaction check (compaction.auto_compact). A turn that
  failed or was stopped never gets here, nor does a compaction.
- `run_ended`, AFTER a run has ended (done, failed, stopped or skipped) or was deleted
  without a turn, from every path that ends one: its runner (always, however the turn
  ended), Store.reconcile (`on_run_end`: a runner that died, or one that died before it
  got here) and the facade (a stop, a withdrawal). In order:
  1. only from the runner of a compaction (`compaction`): the note that its outcome pauses
     automatic compaction (compaction.compaction_ended);
  2. the notifications (notify.run_ended): give back what a notification turn that did not
     succeed claimed, check the agent itself when due, then its parents and further up, and
     mark the run checked;
  3. the desktop notification (desktop.turn_ended).
  A step that fails is printed and the next still runs: run_ended never raises.

A runner that is killed outright runs neither; the reconcile of some later process finds the
run and calls `run_ended`. No module here imports the service: a runner process uses this
without the facade, with `attach` in place of what Orchestrator(store) sets up.
"""

from __future__ import annotations

import sys

from .. import desktop, notify
from ..models import Run
from ..store import Store
from . import compaction, launcher
from .compaction import Launch


def turn_done(store: Store, run_id: int, launch: Launch) -> Run | None:
    """Before a succeeded turn is stored as done: queue (and launch) the automatic
    compaction that is due, if any (compaction.auto_compact, which may raise)."""
    return compaction.auto_compact(store, run_id, launch)


def run_ended(
    store: Store,
    run_id: int,
    agent_id: str,
    launch: Launch,
    recheck: bool = False,
    compacted: bool = False,
) -> None:
    """After a run has ended (or was deleted): the steps in the module doc. `recheck` as in
    notify.run_ended; `compacted`: the run is a compaction and this is its runner. Never
    raises."""
    if compacted:
        try:
            compaction.compaction_ended(store, run_id)
        except Exception as exc:
            print(f"checking the compaction's outcome failed: {exc!r}", file=sys.stderr)
    try:
        notify.run_ended(store, run_id, agent_id, launch, recheck)
    except Exception as exc:
        print(f"notifying the parents of {agent_id} failed: {exc!r}", file=sys.stderr)
    desktop.turn_ended(store, run_id, agent_id)  # never raises


def launch_for(store: Store) -> Launch:
    """How a process without the facade starts a runner (launcher.launch, looked up at each
    call, as the facade's _launch does)."""
    return lambda run: launcher.launch(store, run)


def attach(store: Store) -> Launch:
    """Set up a store for a process without the facade (a runner) as Orchestrator(store)
    does: the runs its reconcile ends go through run_ended. Returns the launch to use."""
    launch = launch_for(store)
    store.on_run_end = lambda run_id, agent_id, recheck: run_ended(
        store, run_id, agent_id, launch, recheck
    )
    return launch
