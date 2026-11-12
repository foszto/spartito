# Desktop app: manual checks

These checks need a person in front of a screen: a real window, a real notification and a
real login cannot be tested automatically. They describe macOS; the Linux equivalents are
noted where they differ. Background: [desktop.md](desktop.md).

## Your own install, after a merge

This section, and only this one, acts on your real install and home, on purpose. Run it in a
new terminal **without** the environment of the setup below, in the checkout that was merged:

1. `uv tool install --editable '.[app,menubar]'` (no `--force` needed over an existing install;
   the tool `orchestrator` of an older install is replaced instead:
   [upgrading](usage.md#upgrading-to-the-distribution-name-spartito)).
2. `spartito app install`: two lines, `installed Spartito (local.spartito.app): …` and
   `spartito-python: a copy of …, signed as local.spartito.app, …`.
3. `spartito app`: the window opens; Dock and app switcher show *Spartito* with its icon.
4. Allow *Spartito* in the prompt that appears (or later: **System Settings › Notifications ›
   Spartito**).
5. In any browser tab of the dashboard you use, click **Enable notifications** in the header
   and allow it.

Repeat step 2 after every Python upgrade (Homebrew or uv).

### After a merge that changes notifications

`spartito app install` is **not** needed again when the merge changes neither the launcher nor the
dependencies: the launcher only starts `spartito app` from the tool environment, which with
`--editable` is the merged checkout. It is needed after a Python upgrade, or when the state
line below says `run spartito app install again`. Nor is a restart of the dashboard service: it
reads the page (`index.html`) and its static files from disk on each request, so a running
service already serves the merged page; the notices are taken by the window process, which
the restart below replaces. Steps, on the real app:

1. Quit Spartito (Cmd-Q).
2. Start Spartito from the Dock or `~/Applications`. Under the agent list, at the bottom of
   the sidebar, a quiet line says the notification state, for example `notifications:
   Spartito shows them; a click opens it there`. The same lines are in
   `~/.orchestrator/logs/app.log`. If macOS shows a prompt for *Spartito*, allow it.
3. Click another app's window so Spartito is in the background, and have a lead record a
   decision. (The open window comes before a page allowed to notify, so browser tabs may stay
   open.)
4. Expect one notification titled *Spartito*: `Decision needed (<project>): …`. Click it:
   the Spartito window comes to the front with the **Decisions** window on that decision. No
   Script Editor. `spartito notices -n 1` shows `window-system  window:<pid>`.
5. **System Settings › Notifications › Spartito**: switch **Allow notifications** off. The
   state line changes within a few seconds to `… not allowed (System Settings › Notifications
   › Spartito); notices are shown in this window`.
6. With Spartito in the background again, trigger another decision: no macOS notification;
   the Dock icon bounces once and shows the badge `1`; in the window a banner at the top right
   shows the decision. Click the Dock icon: the badge goes. Click the banner: the decision
   opens and the banner closes.
7. Switch **Allow notifications** on again.

If a step fails, send the step number, what you saw, the output of `spartito notices -v -n 5`
(each notice's way, process and reason; it includes the notices' texts, so shorten them if
needed), and `~/.orchestrator/logs/app.log` (the window's notification state lines, each
click on a notification with its target, whether the page did not take notices and how many
were handed over at close, why the window ended, and why a native window could not open; no
notice text). `~/.orchestrator/logs/notices.log` has the same steps as `spartito notices`,
also for notices the database was too busy to take at once.

**Any notification from Script Editor (`osascript`) while the window was open** is what this
trace is for: run `spartito notices -v -n 10` right after it and send the lines of that
notice. Its `route` step says which process decided and what it saw.

**Verified so far on a live installation:** a click on a notification Spartito posted opened
Spartito at its target (step 4). Steps 5 and 6 (permission off: banner, bounce, badge) have
not been seen on a live installation yet.

## Setup

Everything runs with the code of the checkout under test, a temporary home and port 18787, so
the installed `spartito`, your real home (`~/.orchestrator`) and a live dashboard (port 8787 by
default) are not involved.

**Shell A**, in the checkout of the branch under test:

```sh
uv sync --extra app --extra menubar
check="$(mktemp -d)"
cat > "$check/env.sh" <<EOF
export CHECK_DIR="$check"
export ORCH_HOME="$check/home"
export PORT=18787
export SPARTITO_BIN="$PWD/.venv/bin/spartito"
spartito_test() { "\$SPARTITO_BIN" "\$@"; }
EOF
. "$check/env.sh"
echo ". '$check/env.sh'"    # copy this line
```

**Shell B**: open a second terminal and paste the line printed above. It loads the same
environment. Whenever a step names a shell, use that shell, after loading this environment.
After a logout, or in any new terminal, paste the line again before going on.

**Guard.** Before each step, in the shell it names:

```sh
echo "$ORCH_HOME $PORT"
```

This must print `<temporary directory>/home 18787`. If it prints nothing, or `~/.orchestrator`,
stop and load the environment first. Every command below calls `spartito_test` (the checkout's
`spartito`) or `"$SPARTITO_BIN"`, never a bare `spartito`.

For steps 3 to 5, register a scratch repository as a project (**shell B**):

```sh
git init -q "$CHECK_DIR/scratch" && git -C "$CHECK_DIR/scratch" commit -q --allow-empty -m init
spartito_test project add "$CHECK_DIR/scratch"
```

## 1. `spartito app`

Long-running commands go in **shell A**, the others in **shell B**.

| shell | do | expect |
|---|---|---|
| A | `spartito_test app --port "$PORT"` | `web UI: http://127.0.0.1:18787/`; a window titled *Spartito* with the dashboard, no browser tabs or address bar |
| — | Work in the window: select Maestro, open **Models and effort**, switch the theme | it behaves like the dashboard in a browser |
| B | `spartito_test app` (no `--port`: it stops at the lock) | `the dashboard window of this home is already open: brought it to the front`; no second window |
| — | Click another application, then in B: `spartito_test app --decision <an open decision id>` (or `--agent <id>`) | the window comes to the front showing that decision; B prints `…: brought it to the front, showing decision:<id>` |
| — | Close the window | in shell A, `spartito app` returns; `echo $?` prints `0` |
| B | `ls "$ORCH_HOME/run"; lsof -nP -iTCP:"$PORT" -sTCP:LISTEN` | no `web.json`; `lsof` prints nothing |
| A | `"$SPARTITO_BIN" app --port "$PORT" & app_pid=$!` | the window opens again |
| A | `kill -TERM "$app_pid"; wait "$app_pid"; echo $?` | the window closes; `143` |
| A | `spartito_test web --port "$PORT"` | `web UI: http://127.0.0.1:18787` |
| B | `spartito_test app` (no `--port`: it uses the running server) | `web UI: http://127.0.0.1:18787/ (the running server)` |
| — | Close the window | `spartito app` in shell B returns; `spartito web` in shell A keeps running |
| A | Ctrl-C | `spartito web` ends |

Remembered port: **shell A** `spartito_test app`, note the port it prints, close the window, run
`spartito_test app` again: the same port, and the theme you chose is still set. Close it.

Without the extra (**shell A**, in the checkout):

| shell | do | expect |
|---|---|---|
| A | `uv sync` (removes the extras), then `spartito_test app --port "$PORT"` | `no native window: pywebview is not installed (install the extra spartito[app], then run \`spartito app install\`; …)`, then a Chrome (or Edge, Brave, Chromium) window in app mode, with the browser's name and icon in the Dock |
| — | Close that window | `spartito app` returns, and the browser instance it started ends (your own browser windows stay) |
| A | `uv sync --extra app --extra menubar` | the extras are back |

## 1b. Launcher, name and icon (macOS)

A launcher of its own name, *Spartito Check*, so the real `~/Applications/Spartito.app` is not
touched; it carries the temporary home in its script. **Shell B**:

| do | expect |
|---|---|
| `spartito_test app install --name "Spartito Check"` | `installed Spartito Check (local.spartito-check-<8 hex>.app): …/Applications/Spartito Check.app` and the `spartito-python: a copy of …` line |
| `codesign -dv ~/Applications/"Spartito Check.app"/Contents/MacOS/spartito-python 2>&1 \| grep -E 'Identifier\|Signature'` | `Identifier=local.spartito-check-<8 hex>.app`, `Signature=adhoc` |
| `open -a ~/Applications/"Spartito Check.app"` | the window opens; Dock and app switcher (Cmd-Tab) show *Spartito Check* with the tree icon, not *Python*; a prompt asks to allow notifications for *Spartito Check*: allow |
| `lsappinfo info -only bundleID,name $(lsappinfo find bundleID=$(defaults read ~/Applications/"Spartito Check.app"/Contents/Info CFBundleIdentifier))` | the name *Spartito Check* and its identifier; close the window afterwards |
| **Shell A**: `spartito_test app --name "Spartito Check"` | the window opens as *Spartito Check* (same Dock icon), from the terminal; stderr has no `the Dock shows this window as Python` line |

Close the window afterwards.

## 2. Installing the page

**Shell A**: `spartito_test web --port "$PORT"`, and leave it running for steps 2 and 3. The
browsers need no environment: they open `http://127.0.0.1:18787`, not your live dashboard.

| do | expect |
|---|---|
| Chrome: open `http://127.0.0.1:18787`, click Install in the address bar | the prompt names *Spartito* and shows the tree icon; the installed app opens in its own window, with the same icon in the Dock |
| Safari: open the same address, **File › Add to Dock** | the suggested name is *Spartito* and the icon is the tree mark; the app opens in its own window |
| Both: look at the icon at small sizes (Dock, app switcher, tab) | the mark is legible and not cut off |

Remove both afterwards: Chrome under `chrome://apps` (right-click › Remove), Safari's app from
`~/Applications`. Take care to remove only the apps for port 18787.

## 3. Notifications

`spartito web` from step 2 still runs in **shell A**; the commands below run in **shell B**. Make
sure notifications are on: `spartito_test config desktop-notifications on`.

To trigger a notification, message an agent directly and let it finish, or have the project
lead record a decision. Both run a real, short Claude Code turn with your login:

```sh
spartito_test spawn -p scratch "Reply with the word done."
spartito_test ask scratch "Record a decision asking me to choose A or B, then stop."
```

**The page.** In Chrome, open `http://127.0.0.1:18787` (a fresh profile, or one that never
allowed this address):

| do | expect |
|---|---|
| Look at the header | an **Enable notifications** button under the title |
| Click it, allow | the button is gone |
| Switch to another app, trigger a notification | exactly one notification, from Chrome, titled *Spartito*; no second one from macOS |
| Click it | the dashboard comes to the front on that agent, or with the **Decisions** window open on that decision |
| Repeat with the page in a background tab | still one notification from Chrome |
| Close the Chrome pages, repeat in Safari (tab or **Add to Dock** app) | one notification from Safari; a click opens the agent or decision |

**Spartito itself** (macOS, the *Spartito Check* launcher of step 1b, allowed there). Close
every dashboard page first and wait 20 s.

| do | expect |
|---|---|
| `open -a ~/Applications/"Spartito Check.app"`, switch to another app, trigger a notification | one notification from *Spartito Check*; `ls "$ORCH_HOME/run"` lists `app-notifier` and `app-notify.json` |
| Click it | the window comes to the front at the agent or decision |
| Close the window, trigger another | one notification from *Spartito Check*, no Dock icon appears for it; `spartito_test notices -n 1` shows `launcher-helper  notify:<pid>` |
| Click it | *Spartito Check* starts and opens the window at the agent or decision; close it |
| Catch the hidden start: trigger another and at once run `for i in $(seq 400); do a=$(lsappinfo find bundleID=$(defaults read ~/Applications/"Spartito Check.app"/Contents/Info CFBundleIdentifier)); [ -n "$a" ] && { lsappinfo info -only applicationtype $a; break; }; sleep 0.25; done` | `type="UIElement"` |
| Open the window, look under the agent list | the quiet line `notifications: Spartito Check shows them; a click opens it there`; the same line is in `$ORCH_HOME/logs/app.log` |
| Turn *Spartito Check* off in **System Settings › Notifications** | within a few seconds the line reads `notifications from Spartito Check: not allowed (…); notices are shown in this window`; `app-notify.json` is gone |
| Switch to another app, trigger another | no macOS notification and no `osascript` one; the Dock icon bounces once with the badge `1`; a banner at the top right of the window; clicking the Dock icon removes the badge; clicking the banner opens the agent or decision and closes it |
| Trigger four more, then reload the window (Cmd-R) | at most three banners, the newest; closed ones stay closed |
| Close the window, trigger another | a plain macOS notification ending with `— open Spartito to see it` (or `to answer`): the launcher may not post any more (`app-notify.json` is gone) |
| `spartito_test notices -v -n 3` | the last line's path is `osascript`, its reason starts `no window, no permitted page, no launcher (no grant file`; the one before is `window-banner` from `window:<pid>` |
| `spartito_test config desktop-notifications off`, trigger another | no notification at all; then `spartito_test config desktop-notifications on` |

If Safari blocked the prompt earlier: Safari **Settings › Websites › Notifications**.

On Linux (with `notify-send`), close every page: a notification titled *Spartito* with the
icon; with libnotify 0.7.10 or later a click opens `spartito app` at the agent or decision,
otherwise the text ends with `— open it: spartito app --decision <n>`.

On Linux, the native window of `spartito app` (Qt):

| do | expect |
|---|---|
| Look at the header | no **Enable notifications** button |
| **Models and effort** › **Notifications** | a line saying that this window does not ask to show notifications and Spartito shows them itself (on Linux with notify-send); no **Allow in this browser** |
| Switch to another app, trigger a notification | one notification from `notify-send`, titled *Spartito*; a click brings the window to the front at the agent or decision; no error in the terminal or in `$ORCH_HOME/logs/app.log` |
| `grep 'Qt permissions' "$ORCH_HOME/logs/app.log"` | `Qt permissions: pywebview's handler replaced (media granted, the rest denied)` |

Leave one decision open for step 4. Stop `spartito web` in **shell A** with Ctrl-C.

## 4. Menu-bar item

macOS only. `spartito menubar` and the `spartito app` it starts take the environment of the shell it
runs in, so they use the temporary home; the `spartito app` it starts picks a free port.

| shell | do | expect |
|---|---|---|
| A | `spartito_test menubar` | `♪` (or `♪ <running> · <decisions>`) appears in the menu bar; no Dock icon |
| — | Open its menu | `Running agents: …`, `Open decisions: …`, one `#<id>  <question>` line per open decision, **Open Spartito**, **Quit** |
| — | Click an open decision | the `spartito app` window opens on that decision |
| — | Click another app, then the decision again | the same window comes to the front on that decision; no second window |
| — | Close that window, click **Open Spartito** | the `spartito app` window opens; the item stays |
| — | Click **Quit** | the item disappears and `spartito menubar` returns in shell A; the `spartito app` window stays open |
| — | Close the `spartito app` window | it closes, and the server it started stops with it |

## 5. Background service

**Shell B.** The temporary home gives the service its own label
(`local.spartito-<8 hex>.web`), so the service of your real home is not touched. The unit file
itself goes where the system expects it (`~/Library/LaunchAgents/`, on Linux
`~/.config/systemd/user/`), next to the real one; the last row removes it. Nothing else may
listen on port 18787 (stop the `spartito web` of the earlier steps).

| do | expect |
|---|---|
| `spartito_test service install --port "$PORT"` | `installed local.spartito-<8 hex>.web: http://127.0.0.1:18787`, the unit file and the log path |
| `spartito_test service status` | `state:   loaded, pid <pid>` and `web UI:  http://127.0.0.1:18787: answers` |
| Open `http://127.0.0.1:18787` | the dashboard |
| `kill <the pid shown by status>`, wait about 10 s, `spartito_test service status` | a new pid; the dashboard answers again |
| Log out and in again; open a terminal, paste the environment line, run the guard, then `spartito_test service status` | the service is running and answers |
| `spartito_test service logs` | the log path and its last lines |
| `spartito_test service install --port "$PORT"` again | the service restarts (a new pid); still one service |
| `spartito_test service uninstall` | `removed local.spartito-<8 hex>.web: <unit file>` |
| `spartito_test service status` | `unit:    <unit file> (not installed)`, `state:   not loaded` and the `logs:` line; no `web UI` line |

The label comes from `ORCH_HOME`: run `uninstall` with the same environment as `install`. On
Linux the state reads `active` (after uninstall `not active`), the logs are in
`journalctl --user -u <label>.service`, and a killed service is restarted after about 2 s only
when it ended with an error (`kill -9 <pid>`).

## Cleanup

**Shell B**, after the guard:

```sh
spartito_test service uninstall      # if step 5 was interrupted; says "not installed" otherwise
id=$(defaults read ~/Applications/"Spartito Check.app"/Contents/Info CFBundleIdentifier)
spartito_test app uninstall --name "Spartito Check"
rm -rf ~/Library/WebKit/"$id" ~/Library/Caches/"$id"
rm -rf "$CHECK_DIR"
```

The *Spartito Check* entry stays in **System Settings › Notifications**; turn it off there.

Then close both shells (the environment goes with them).
