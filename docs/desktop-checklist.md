# Desktop app: manual checks

These checks need a person in front of a screen: a real window, a real notification and a
real login cannot be tested automatically. They describe macOS; the Linux equivalents are
noted where they differ. Background: [desktop.md](desktop.md).

## Setup

Run everything from the checkout of the branch under test, with `uv run`, so the installed
`orch` and your live dashboard are not involved. Use a temporary home and a port other than
the one your live dashboard uses (8787 by default):

```sh
cd <checkout of the branch>
uv sync --extra app
export ORCH_HOME="$(mktemp -d)"
export PORT=18787
```

Keep this shell open: every step below runs in it, with this `ORCH_HOME`. For steps 3 and 5,
register a scratch repository as a project:

```sh
git init -q /tmp/scratch && git -C /tmp/scratch commit -q --allow-empty -m init
uv run orch project add /tmp/scratch
```

## 1. `orch app`

| do | expect |
|---|---|
| `uv run orch app --port "$PORT"` | `web UI: http://127.0.0.1:<port>/`; a window titled *Spartito* with the dashboard, no browser tabs or address bar |
| Work in it: select Maestro, open **Models and effort**, switch the theme | it behaves like the dashboard in a browser |
| Run `uv run orch app` in a second shell with the same `ORCH_HOME` | `the dashboard window of this home is already open`; no second window |
| Close the window | `orch app` returns with exit code 0; `$ORCH_HOME/run/web.json` is gone; nothing listens on the port |
| Start it again, then from another shell `pkill -TERM -f 'bin/orch app'` | the window closes and `orch app` ends (exit code 143) |
| Start `uv run orch web --port "$PORT"` in another shell, then `uv run orch app` | `web UI: … (the running server)`; closing the window leaves `orch web` running |
| Without the extra: `uv sync` (drops it), then `uv run orch app` | `no native window: pywebview is not installed …`, then a Chrome (or Edge, Brave, Chromium) window in app mode; closing it ends `orch app` and the browser instance |

Run `uv sync --extra app` again afterwards.

## 2. Installing the page

Start `uv run orch web --port "$PORT"` and leave it running for steps 2 and 3.

| do | expect |
|---|---|
| Chrome: open `http://127.0.0.1:<port>`, click Install in the address bar | the prompt names *Spartito* and shows the tree icon; the installed app opens in its own window, with the same icon in the Dock |
| Safari: open the same address, **File › Add to Dock** | the suggested name is *Spartito* and the icon is the tree mark; the app opens in its own window |
| Both: look at the icon at small sizes (Dock, app switcher, tab) | the mark is legible and not cut off |

Remove both afterwards: Chrome under `chrome://apps` (right-click › Remove), Safari's app from
`~/Applications`.

## 3. Notifications

In the dashboard (a Chrome tab or the installed app): **Models and effort** ›
**Notifications** › **Allow in this browser**, and accept the browser's prompt. Make sure
desktop notifications are on (`uv run orch config desktop-notifications on`).

To trigger a notification, message an agent directly and let it finish, for example
`uv run orch spawn -p scratch "Reply with the word done."`; or ask the project lead to record a
decision: `uv run orch ask scratch "Record a decision asking me to choose A or B, then stop."`

| do | expect |
|---|---|
| Switch to another app, trigger a notification | exactly one notification, from Chrome, titled *Spartito*; no second one from macOS |
| Click it | the dashboard comes to the front on that agent, or with the **Decisions** window open on that decision |
| Repeat with the page in a background tab | still one notification from Chrome |
| Close the Chrome pages, repeat in Safari (tab or **Add to Dock** app) | one notification from Safari; a click opens the agent or decision |
| Close every dashboard page, wait 20 s, trigger another | the plain macOS notification (Script Editor's), with no click action |
| `uv run orch config desktop-notifications off`, trigger another | no notification at all; turn it back on afterwards |

If Safari blocked the prompt earlier: Safari **Settings › Websites › Notifications**. On
Linux, the last-but-one row shows no notification at all.

## 4. Menu-bar item

<!-- MENUBAR: to be completed -->

To be written once the menu-bar item exists.

## 5. Background service

The temporary `ORCH_HOME` gives the service its own label (`local.spartito-<8 hex>.web`), so
the service of your real home is not touched. Stop the `orch web` of step 2 first.

| do | expect |
|---|---|
| `uv run orch service install --port "$PORT"` | `installed local.spartito-<8 hex>.web: http://127.0.0.1:<port>`, the unit file and the log path |
| `uv run orch service status` | `state: loaded, pid …` and `web UI: http://127.0.0.1:<port>: answers` |
| Open `http://127.0.0.1:<port>` | the dashboard |
| `kill <pid shown by status>`, wait about 10 s, `status` again | a new pid; the dashboard answers again |
| Note the value of `$ORCH_HOME`, log out and in again, export it again, `status` | the service is running and answers |
| `uv run orch service logs` | the log path and its last lines |
| `uv run orch service install --port "$PORT"` again | the service restarts (a new pid); still one service |
| `uv run orch service uninstall`, then `status` | `removed …`; then `(not installed)`, `not loaded`, `nothing answers` |

Run `uninstall` with the same `ORCH_HOME` as `install`: the label comes from it. On Linux the
state reads `active`, the logs are in `journalctl --user -u <label>.service`, and a killed
service is restarted after about 2 s only when it ended with an error (`kill -9`).

## Cleanup

```sh
uv run orch service uninstall          # if step 5 was interrupted
rm -rf "$ORCH_HOME" /tmp/scratch
unset ORCH_HOME PORT
```
