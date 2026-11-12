# Task board across projects

The task board of every project in one view of the dashboard, with the live state of each
item's agent and branch, items made from a conversation's message, the decisions that block
an item, its history, and the automatic move to `done` when its branch is merged. The basics
of the board (items, states, dispatch of a project's `ready` items) are in
[agents.md](agents.md#task-board).

- [The view](#the-view)
- [Automatic move to done](#automatic-move-to-done)
- [Command line, MCP and API](#command-line-mcp-and-api)
- [Known limitations](#known-limitations)
- [Checklist for the owner](#checklist-for-the-owner)

## The view

**Board** in the dashboard's left column opens it. Routes:

| route | shows |
|---|---|
| `#board:` | every registered project's items, each column grouped by project |
| `#board:<project>` | one project's items |
| `#board:<project>/<id>` | the same, with item `<id>`'s detail open (an item of another project is refused) |

Four columns, one per state. The filter in the header switches between all projects and one;
**Show done** shows the `done` cards (remembered in the browser); **Add item** adds one. A
click on a card, or Enter, opens its detail: the item's fields, and below them its live
state, source, blockers, history and runs. The board button in a project's row still opens
that project's own board dialog, unchanged.

**Moving items.** An item moves only within its own project:

- drag a card to another place or column (a group of another project refuses the drop);
- with the card focused: Alt+Up and Alt+Down move it within its state, Alt+Left and
  Alt+Right to the end of the previous or next state; Up and Down go to the card above or below;
- the card's **⋯** menu: move up, move down, move to a state.

The result is read out to screen readers ("#12 → Ready, 2 of 5").

**Selection and dispatch.** `idea` and `ready` cards have a checkbox (Space on a focused card,
or **Select for dispatch** in its menu). **Dispatch** in the selection bar asks first, naming
which lead gets which items, then sends exactly the selected items, one message per project to
its lead (they become `in_progress`, linked to the lead). An item that is no longer `idea` or
`ready` is skipped and said so. The selection keeps only items the view shows: changing the
filter to one project drops the selected items of other projects.

**Live state.** From the server's cache of the last git check (the header says when it ran,
"git state checked … ago"); the view itself never runs git.

- The linked agent: a status dot and word (running, waiting, done, failed, stopped, archived,
  gone) on the card; a click opens its conversation.
- The branch: `↑3` commits ahead of the project's base branch, `↓1` behind, `dirty`
  (uncommitted changes in the agent's worktree), `merged ✓`, `no branch` (the branch does not
  exist), or `git error` (the last check failed; the tooltip says why). Nothing while the
  branch has not been checked yet. Only `ready` and `in_progress` items are checked.
- A place for the state of the branch's pull request is reserved on the card and in the
  detail; it is not part of this batch and shows nothing.

**Items from a message.** Each message in an agent's conversation (not an error) has **Add to
board** when hovered or focused. It opens a form with the
message's first line as title and the message as description; the item goes to the agent's
project (the orchestrator's conversation asks which). The item keeps a link to the message:
"from `<agent>`" on the card and the source in the detail. A message already on the board
shows **On the board: #id** instead of a second item.

**Blocking decisions.** An open decision blocks an item when it is linked to it; a decision
linked to no item blocks every item of its agent that is not `done`. Answered or withdrawn
decisions block nothing. A blocked card has a marked edge and a "⚑ N decisions" badge; the
detail lists the decisions (each opens in the decisions dialog), unlinks a linked one, and
links another open decision: the project's or the orchestrator's (a decision of another
project cannot be linked).

**History.** The detail lists every change of the item, newest first: when, by whom (you, an
agent, or the automatic merge rule) and what (added, edited, state, moved, placed, dispatched,
linked, unlinked a decision, restored). A change made by older code that keeps no history shows as a
gap ("not recorded"). The runs the history names (a dispatch, an agent's turn) are listed
below and open their agent.

## Automatic move to done

An item in `ready` or `in_progress` whose branch is found merged into the project's base
branch moves to `done`, with a history entry by `merged` naming the commit. Merged means:
nothing on the branch is missing from the base, the agent's worktree on it (if any) is clean,
and the work a previous check saw ahead of the base is in the base, as those commits or as
patch-equivalent ones (so a rebased or reworded branch counts); an item moves once per merged
tip, so one moved back by hand stays until new work is seen ahead and merged.

The check runs at most once a minute across all processes and spends about 2 seconds; there
is no background process: it runs inside the first board or overview read after that minute
(the dashboard's polling, `orch board --live`, `list_board(live=true)`). Not
detected: a squash merge of more than one commit, and commits whose content changed between
the last check that saw them and the merge. Such items are moved by hand.

## Command line, MCP and API

```sh
orch board --live                    # each item with its agent's status and branch state
orch board dispatch 12 15 21         # those items (idea or ready, any project) to their leads
orch board dispatch -p my-app        # the project's ready items
orch board show 12                   # an item with its source, blockers, history and runs
orch board link 12 7                 # decision 7 blocks item 12 (--remove: unlink)
```

`orch board --live` shows the branch as `+3 -1 dirty`, `even`, `merged`, `no branch` or
`error`.

MCP tools: `list_board(project, state, live)` (`live` adds agent and git status),
`dispatch_board(project, items)` (named items, default all `ready`; the top-level orchestrator
only), `request_decision(question, options, recommendation, item)` (`item`: the board item
the decision blocks, linked at once).

HTTP API (the dashboard's; writes need the page's own origin and a JSON body):

| route | does |
|---|---|
| `GET /api/board/view?project=&state=` | items with `live`, `blockers`, `source`; the projects; when git was last checked |
| `POST /api/board/<id>/place` | `{state, before}`: into a state just before another item (null: at the end) |
| `POST /api/board/dispatch-items` | `{ids}`: those items to their leads, one message per project |
| `GET /api/board/<id>/detail` | the item with its history, runs, live state, blockers and source |
| `POST /api/board/from-message` | `{agent_id, event_id, title?, description?, state?, project?}`: a new item from a message |
| `POST /api/board/<id>/blockers` | `{decision_id, linked}`: link or unlink a decision; the item's blockers |

**Database.** The batch adds four tables: `board_history`, `board_git` (the git cache),
`board_sources` and `board_blockers`. They are additive: older code keeps working on the same
database and does not know them (a change it makes shows as a gap in the history).

## Known limitations

- While an item's detail is open, a change of its live state or blockers rebuilds the lower
  part of the dialog: keyboard focus there and a chosen Link option are lost (a typed title
  and the other fields are kept).
- A board route naming an unknown project repeats a failing request every 2 seconds.
- Changing the project filter drops the selected items of other projects.

## Checklist for the owner

Manual checks in a visible browser. They use the code of the checkout under test, a temporary
home and port 18788, so your real home (`~/.orchestrator`) and a live dashboard (port 8787)
are not involved.

**Shell A**, in the checkout of the branch under test:

```sh
uv sync --extra app
check="$(mktemp -d)"
cat > "$check/env.sh" <<EOF
export CHECK_DIR="$check"
export ORCH_HOME="$check/home"
export PORT=18788
export ORCH_BIN="$PWD/.venv/bin/orch"
orch_test() { "\$ORCH_BIN" "\$@"; }
EOF
. "$check/env.sh"
echo ". '$check/env.sh'"    # copy this line
```

**Shell B**: open a second terminal and paste the line printed above; it loads the same
environment. Every command below calls `orch_test` (the checkout's `orch`), never a bare
`orch`. **Guard**, before each step in either shell: `echo "$ORCH_HOME $PORT"` prints
`<temporary directory>/home 18788`; if it prints nothing or `~/.orchestrator`, load the
environment first.

Two projects and three items (**shell B**), then the dashboard (**shell A**, left running):

```sh
for p in alpha beta; do
  git init -q -b main "$CHECK_DIR/$p" && git -C "$CHECK_DIR/$p" commit -q --allow-empty -m init
  orch_test project add "$CHECK_DIR/$p" --name "$p"
done
orch_test board add -p alpha "First task" --state ready
orch_test board add -p alpha "Second task" --state idea
orch_test board add -p beta "Third task" --state ready
```

```sh
orch_test web --port "$PORT"            # shell A; open http://127.0.0.1:18788/#board:
```

The numbered steps run in **shell B** unless they name shell A.

1. **Board** in the left column opens `#board:` with both projects grouped in each column;
   the filter shows one project and back.
2. Drag "First task" by hand to **In progress**, then within its column above another card;
   a drop on beta's group is refused and says why.
3. Keyboard only: Tab to a card, Alt+Right twice moves it two states, Alt+Up moves it up, Enter
   opens the detail, Escape closes it and the focus is back on the card.
4. The **⋯** menu opens from the keyboard, Up and Down move between entries, Escape closes it
   and the focus is back on **⋯**.
5. Select an alpha and a beta item, **Dispatch**: the confirmation names both leads; afterwards
   both items are `in_progress` and linked to their leads (needs a provider; one short turn each).
6. With one project selected in the filter, the other project's selected item leaves the
   selection.
7. A real merge: `git -C "$CHECK_DIR/alpha" switch -c feat/x`, commit a file
   (`echo x > "$CHECK_DIR/alpha/x.txt"; git -C "$CHECK_DIR/alpha" add x.txt;
   git -C "$CHECK_DIR/alpha" commit -m x`), `git -C "$CHECK_DIR/alpha" switch main`;
   `orch_test board edit <id> --branch feat/x` on a `ready` item; after a minute the card shows
   `↑1`; `git -C "$CHECK_DIR/alpha" merge --no-ff -m "merge feat/x" feat/x`; after another
   minute the item is in **Done** and its history names `merged` and the commit.
8. `orch_test board edit <id> --branch feat/none` on a `ready` item: after a minute it shows
   `no branch`, on the card and in the detail (`orch_test board --live` says the same).
9. Send any message to a lead; **Add to board** on a message opens the form; after **Add** the
   focus is on **On the board: #id**; reload the conversation and click **Add to board** on the
   same message again: no second item, the link is shown.
10. Open the new item: the source names the agent and opens its conversation.
11. Ask Maestro to record a decision with `request_decision`; in an item's detail link it,
    see it under **Blocked by** and as the card's badge; unlink it; link and answer it: it goes.
12. Switch the theme (moon button): cards, chips, menu and detail are readable in both.
13. A narrow window (about 400 px wide): one column, nothing overlaps, cards still open.
14. Shell A: stop `orch_test web` (Ctrl+C), then `orch_test app --port "$PORT"`: the board
    works the same in the window.
15. Done: close the window, then in shell B `echo "$CHECK_DIR"` (a temporary directory) and
    `rm -rf "$CHECK_DIR"`.
