# Desktop app: manual checks

These checks need a person in front of a screen: a real window, a real notification and a
real login cannot be tested automatically. They describe macOS; the Linux equivalents are
noted where they differ. Background: [desktop.md](desktop.md).

## Setup

Everything runs with the code of the checkout under test, a temporary home and port 18787, so
the installed `orch`, your real home (`~/.orchestrator`) and a live dashboard (port 8787 by
default) are not involved.

**Shell A**, in the checkout of the branch under test:

```sh
uv sync --extra app --extra menubar
check="$(mktemp -d)"
cat > "$check/env.sh" <<EOF
export CHECK_DIR="$check"
export ORCH_HOME="$check/home"
export PORT=18787
export ORCH_BIN="$PWD/.venv/bin/orch"
orch_test() { "\$ORCH_BIN" "\$@"; }
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
stop and load the environment first. Every command below calls `orch_test` (the checkout's
`orch`) or `"$ORCH_BIN"`, never a bare `orch`.

For steps 3 to 5, register a scratch repository as a project (**shell B**):

```sh
git init -q "$CHECK_DIR/scratch" && git -C "$CHECK_DIR/scratch" commit -q --allow-empty -m init
orch_test project add "$CHECK_DIR/scratch"
```

## 1. `orch app`

Long-running commands go in **shell A**, the others in **shell B**.

| shell | do | expect |
|---|---|---|
| A | `orch_test app --port "$PORT"` | `web UI: http://127.0.0.1:18787/`; a window titled *Spartito* with the dashboard, no browser tabs or address bar |
| — | Work in the window: select Maestro, open **Models and effort**, switch the theme | it behaves like the dashboard in a browser |
| B | `orch_test app` (no `--port`: it stops at the lock) | `the dashboard window of this home is already open`; no second window |
| — | Close the window | in shell A, `orch app` returns; `echo $?` prints `0` |
| B | `ls "$ORCH_HOME/run"; lsof -nP -iTCP:"$PORT" -sTCP:LISTEN` | no `web.json`; `lsof` prints nothing |
| A | `"$ORCH_BIN" app --port "$PORT" & app_pid=$!` | the window opens again |
| A | `kill -TERM "$app_pid"; wait "$app_pid"; echo $?` | the window closes; `143` |
| A | `orch_test web --port "$PORT"` | `web UI: http://127.0.0.1:18787` |
| B | `orch_test app` (no `--port`: it uses the running server) | `web UI: http://127.0.0.1:18787/ (the running server)` |
| — | Close the window | `orch app` in shell B returns; `orch web` in shell A keeps running |
| A | Ctrl-C | `orch web` ends |
| B | `uv sync --project "$CHECK_DIR/.." 2>/dev/null; ` — see the next row | |

Without the extra (**shell A**, in the checkout):

| shell | do | expect |
|---|---|---|
| A | `uv sync` (removes the extras), then `orch_test app --port "$PORT"` | `no native window: pywebview is not installed …`, then a Chrome (or Edge, Brave, Chromium) window in app mode |
| — | Close that window | `orch app` returns, and the browser instance it started ends (your own browser windows stay) |
| A | `uv sync --extra app --extra menubar` | the extras are back |

## 2. Installing the page

**Shell A**: `orch_test web --port "$PORT"`, and leave it running for steps 2 and 3. The
browsers need no environment: they open `http://127.0.0.1:18787`, not your live dashboard.

| do | expect |
|---|---|
| Chrome: open `http://127.0.0.1:18787`, click Install in the address bar | the prompt names *Spartito* and shows the tree icon; the installed app opens in its own window, with the same icon in the Dock |
| Safari: open the same address, **File › Add to Dock** | the suggested name is *Spartito* and the icon is the tree mark; the app opens in its own window |
| Both: look at the icon at small sizes (Dock, app switcher, tab) | the mark is legible and not cut off |

Remove both afterwards: Chrome under `chrome://apps` (right-click › Remove), Safari's app from
`~/Applications`. Take care to remove only the apps for port 18787.

## 3. Notifications

`orch web` from step 2 still runs in **shell A**; the commands below run in **shell B**.

In the dashboard at `http://127.0.0.1:18787` (a Chrome tab or the installed app): **Models
and effort** › **Notifications** › **Allow in this browser**, and accept the browser's
prompt. Then make sure notifications are on: `orch_test config desktop-notifications on`.

To trigger a notification, message an agent directly and let it finish, or have the project
lead record a decision. Both run a real, short Claude Code turn with your login:

```sh
orch_test spawn -p scratch "Reply with the word done."
orch_test ask scratch "Record a decision asking me to choose A or B, then stop."
```

| do | expect |
|---|---|
| Switch to another app, trigger a notification | exactly one notification, from Chrome, titled *Spartito*; no second one from macOS |
| Click it | the dashboard comes to the front on that agent, or with the **Decisions** window open on that decision |
| Repeat with the page in a background tab | still one notification from Chrome |
| Close the Chrome pages, repeat in Safari (tab or **Add to Dock** app) | one notification from Safari; a click opens the agent or decision |
| Close every dashboard page, wait 20 s, trigger another | the plain macOS notification (Script Editor's), with no click action |
| `orch_test config desktop-notifications off`, trigger another | no notification at all; then `orch_test config desktop-notifications on` |

If Safari blocked the prompt earlier: Safari **Settings › Websites › Notifications**. On
Linux, the last-but-one row shows no notification at all.

Leave one decision open for step 4. Stop `orch web` in **shell A** with Ctrl-C.

## 4. Menu-bar item

macOS only. `orch menubar` and the `orch app` it starts take the environment of the shell it
runs in, so they use the temporary home; the `orch app` it starts picks a free port.

| shell | do | expect |
|---|---|---|
| A | `orch_test menubar` | `♪` (or `♪ <running> · <decisions>`) appears in the menu bar; no Dock icon |
| — | Open its menu | `Running agents: …`, `Open decisions: …`, one `#<id>  <question>` line per open decision, **Open Spartito**, **Quit** |
| B | `orch_test web --port "$PORT"` | `web UI: http://127.0.0.1:18787` |
| — | Click an open decision | the default browser opens `http://127.0.0.1:18787/#decision:<id>` with that decision |
| B | Ctrl-C (stops `orch web`) | |
| — | Click the decision again | the `orch app` window opens (on the dashboard, not on that decision) |
| — | Close that window, click **Open Spartito** | the `orch app` window opens; the item stays |
| — | Click **Quit** | the item disappears and `orch menubar` returns in shell A; the `orch app` window stays open |
| — | Close the `orch app` window | |

## 5. Background service

**Shell B.** The temporary home gives the service its own label
(`local.spartito-<8 hex>.web`), so the service of your real home is not touched. The unit file
itself goes where the system expects it (`~/Library/LaunchAgents/`, on Linux
`~/.config/systemd/user/`), next to the real one; the last row removes it. Nothing else may
listen on port 18787 (stop the `orch web` of the earlier steps).

| do | expect |
|---|---|
| `orch_test service install --port "$PORT"` | `installed local.spartito-<8 hex>.web: http://127.0.0.1:18787`, the unit file and the log path |
| `orch_test service status` | `state:   loaded, pid <pid>` and `web UI:  http://127.0.0.1:18787: answers` |
| Open `http://127.0.0.1:18787` | the dashboard |
| `kill <the pid shown by status>`, wait about 10 s, `orch_test service status` | a new pid; the dashboard answers again |
| Log out and in again; open a terminal, paste the environment line, run the guard, then `orch_test service status` | the service is running and answers |
| `orch_test service logs` | the log path and its last lines |
| `orch_test service install --port "$PORT"` again | the service restarts (a new pid); still one service |
| `orch_test service uninstall` | `removed local.spartito-<8 hex>.web: <unit file>` |
| `orch_test service status` | `unit:    <unit file> (not installed)`, `state:   not loaded` and the `logs:` line; no `web UI` line |

The label comes from `ORCH_HOME`: run `uninstall` with the same environment as `install`. On
Linux the state reads `active` (after uninstall `not active`), the logs are in
`journalctl --user -u <label>.service`, and a killed service is restarted after about 2 s only
when it ended with an error (`kill -9 <pid>`).

## Cleanup

**Shell B**, after the guard:

```sh
orch_test service uninstall      # if step 5 was interrupted; says "not installed" otherwise
rm -rf "$CHECK_DIR"
```

Then close both shells (the environment goes with them).
