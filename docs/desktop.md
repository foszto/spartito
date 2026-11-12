# Desktop app

The dashboard can be used like a desktop application. None of this is needed to use
Spartito; each piece works on its own.

- [Installing the extras](#installing-the-extras)
- [Platform support](#platform-support)
- [Background service](#background-service)
- [Installable page](#installable-page)
- [Notifications with click-through](#notifications-with-click-through)
- [Own window](#own-window)
- [Launcher: name and icon](#launcher-name-and-icon)
- [Menu-bar item](#menu-bar-item)
- [Security](#security)
- [Optional dependencies](#optional-dependencies)
- [Known limitations](#known-limitations)

## Installing the extras

The background service, the installable page and the notifications need nothing beyond the
default install. The native window of `spartito app` needs the optional extra `app`, the
menu-bar item (macOS) the extra `menubar`:

```sh
uv tool install --editable '.[app,menubar]'    # instead of `uv tool install --editable .`
uv sync --extra app --extra menubar            # in a checkout, for `uv run spartito …`
```

Over an existing install, the same `uv tool install` line adds the extras; no `--force` is
needed. An install from before the distribution was renamed to `spartito` (the tool
`orchestrator` in `uv tool list`) is replaced instead:
[upgrading](usage.md#upgrading-to-the-distribution-name-spartito). On macOS, run `spartito app install` afterwards (see
[Launcher](#launcher-name-and-icon)).

Without the extra, `spartito app` falls back to a browser (see [Own window](#own-window)). On
Linux the extra installs PyQt6 and PyQt6-WebEngine, which are GPL-3.0-only, and the Qt
libraries they bring, which are LGPL-3.0 (see
[Optional dependencies](#optional-dependencies)). On Linux the extra `menubar` installs
nothing.

## Platform support

| | macOS | Linux |
|---|---|---|
| background service (`spartito service`) | launchd agent | systemd user unit |
| installable page | Chrome (and other Chromium-family browsers), Safari | Chromium-family browsers |
| notification click-through | the Spartito window; else Spartito itself (launcher installed and allowed); else an open dashboard page; else, while the Spartito window is open, a banner inside it; else waiting for one of these, or `osascript` (no launcher), whose text says where to look | an open dashboard page; else `notify-send`, with a click action from libnotify 0.7.10 |
| launcher (`spartito app install`) | `~/Applications/Spartito.app` | desktop entry and icons |
| `spartito app` native window | Cocoa (WebKit), extra `app` | Qt WebEngine, extra `app` |
| `spartito app` browser fallback | Chromium-family browser in app mode, else the default browser | the same |
| menu-bar item (`spartito menubar`) | yes, extra `menubar` | no |

## Background service

```sh
spartito service install [--host 127.0.0.1] [--port 8787]
spartito service status
spartito service logs [--lines|-n 20]
spartito service uninstall
```

`spartito service install` runs `spartito web` in the background: started at login and restarted when
it stops. It writes the unit file and (re)starts the service; running it again replaces the
unit and restarts the service, so it is also the way to apply a changed `--host` or `--port`,
or to restart after an upgrade. It prints the label, the URL, the unit file and where the logs
are. `--host` and `--port` are those of `spartito web`; a non-loopback `--host` prints the same
warning as `spartito web`.

| | macOS | Linux |
|---|---|---|
| kind | launchd agent in the `gui/<uid>` domain | systemd user unit, `WantedBy=default.target` |
| unit file | `~/Library/LaunchAgents/<label>.plist` | `$XDG_CONFIG_HOME/systemd/user/<label>.service` (default `~/.config/systemd/user/`) |
| started | at login (`RunAtLoad`) | with the user's service manager (`systemctl --user enable --now`) |
| restarted | whenever it exits (`KeepAlive`), at most about every 10 s (launchd's throttle) | when it fails (`Restart=on-failure`, after 2 s) |
| output | `$ORCH_HOME/logs/web.log` | the journal: `journalctl --user -u <label>.service` |

**Label.** `local.spartito.web` for the default home (`~/.orchestrator`); for any other
`ORCH_HOME`, `local.spartito-<8 hex>.web`, from a hash of the resolved home path. A service
for a temporary home therefore never replaces the one for your real home. Every
`spartito service` command acts on the service of the current `ORCH_HOME`.

**What the service runs.** `<python> -m orchestrator web --host H --port P`, with the Python
interpreter of the install and `$ORCH_HOME` as working directory. Its environment is fixed at
install time and holds only these variables of the shell you install from: `PATH`, `LANG`,
`LC_*`, `ORCH_*` (with `ORCH_HOME` set to the resolved home), `CLAUDE_CONFIG_DIR` and
`CODEX_HOME`. Consequences:

- Run `spartito service install` again after changing `PATH` (for example a new location of
  `claude` or `codex`), a setting in these variables, or after moving or reinstalling the
  Python environment.
- Other variables, such as an API key exported in your shell, are not carried into the unit;
  the agents the service starts see only what the service sees.
- Unit files are readable by other users of the machine: keep secrets out of the variables
  above.

**Logs.** On macOS the log file is emptied at install when it is larger than 5 MB; launchd
does not rotate it otherwise. `spartito service logs` prints the log file and its last lines
(`-n`, default 20). On Linux it prints the `journalctl` command.

**Status.** `spartito service status` shows the label and platform, the unit file (or
`(not installed)`), the state (`loaded` or `active`, with the pid) and whether the dashboard
answers at the unit's address: `answers` only when `GET /api/ping` reports this tool with this
home; otherwise `something else answers (…)` (another program, or this tool for another
`ORCH_HOME`) or `nothing answers`.

**Linux without a login.** A systemd user service runs while you are logged in. To keep it
running without a session (for example after a reboot), enable lingering for your user once:
`loginctl enable-linger`. `spartito service` does not do this.

**Failures.** A first install that fails removes the unit file it wrote; when a unit existed
before, the new one stays as written. `spartito service uninstall` stops the service and removes
its unit file. If it cannot stop it, the unit file is kept and the command fails with exit
code 1: `error: could not unload <label>: …` on macOS, `error: could not stop <label>: …` on
Linux. On Linux, when the systemd user manager cannot be reached, the file is removed anyway,
a `warning: the systemd user manager could not be reached (…)` line says so, and the exit
code is 0. On other platforms every `spartito service` command fails with `the background service
is supported on macOS and Linux, not <platform>`.

## Installable page

The dashboard page has a web app manifest (`/manifest.webmanifest`, name *Spartito*,
`display: standalone`) and its own icon, so a browser can install it as an app:

- **Chrome** (and other Chromium-family browsers): the Install button in the address bar.
- **Safari** (macOS): **File › Add to Dock**.

The installed app opens the address it was installed from, for example
`http://127.0.0.1:8787`; a server must be listening there, which the
[background service](#background-service) provides. A page installed from another port is
another app.

**`GET /api/ping`** tells who serves on a port, for `spartito app` and `spartito service status`:

```json
{"tool": "orchestrator", "version": "0.1.0", "home": "3f9a0c1d2e4b5a69"}
```

`home` is the first 16 hex characters of the SHA-256 of the resolved `ORCH_HOME` path, never
the path itself. The call does not open the database.

**Icons.** The files in `src/orchestrator/web/static/` (`icon.svg`, `icon-192.png`,
`icon-512.png`, `icon-maskable-512.png`, `icon-apple-180.png`) are committed. They are drawn by
a script that uses only the standard library and writes the same bytes on every run:

```sh
uv run python scripts/make_icons.py [OUT_DIR]    # default: src/orchestrator/web/static
```

## Notifications with click-through

Notifications are shown on two occasions: a decision starts waiting for you, and an agent you
messaged directly has finished together with all its agents (details:
[configuration.md](configuration.md#desktop-notifications-desktop_notifications)). Each one is
recorded as a *notice* in the database; notices are kept for one day.

**Who shows a notice.** One decision (`desktop.route`) picks the way from what is there at
that moment; every process that records, hands over or takes a notice asks it, and each claims
the notice first, so it is shown once. On macOS, in this order:

1. **The Spartito window** (the native window of `spartito app`) while its process runs and
   it may post notifications, or has not had its answer yet. Allowed (started through the
   launcher, permission given), it posts the notification itself; a click brings the window
   to the front at the agent or decision. A post the system refuses is shown inside the
   window instead. The window counts as present while its process runs, however late its
   marker is (see [How the page and the window are found](#notifications-with-click-through)).
2. **The Spartito app, started to post** (launcher installed and allowed: the grant file
   `run/app-notify.json` names a bundle whose interpreter copy is current): a background
   helper starts the app hidden, without a window or Dock icon; it posts the pending notices
   and exits. A click starts Spartito and opens the window at the agent or decision.
3. **An open dashboard page** allowed to show notifications (a tab, the installed page, or the
   browser window of `spartito app`): a Web Notification titled *Spartito*. A click brings the
   page to the front and opens the agent, or the **Decisions** window on that decision with
   its answer box focused; a decision that is no longer open selects the agent that asked it.
4. **The Spartito window that may not post** (not allowed, refused by macOS, not available):
   it shows the notice inside the window: see [Notices inside the window](#notices-inside-the-window).
5. **Waiting**: none of these can take it now, but a launcher that may post exists, or a
   window runs. The notice stays unclaimed: the next app started to post or the next
   permitted page takes it (within 10 minutes), else the window shows it when it next opens.
   Whoever takes notices that waited (their recorded route is `waiting`) takes them alike
   (the window, the launcher's app, a page): a decision notice is shown while its decision
   is open, never cut, and dropped once it is not; of the finished ones only the newest
   three are shown one by one. The older ones are counted, not lost: in the count banner of
   the window, or, by the launcher's app and a page, in the text of the newest one shown
   (`(<n> more waited) <text>`, counting also those a taker racing it claimed); the trace says
   so for each. Notices routed to a taker just now are never cut, however many come at once.
6. **`osascript`**: only with no launcher that may post, no window and no permitted page. Its
   click opens Script Editor and cannot be changed, so the text ends with
   ` — open Spartito to answer` (a decision) or ` — open Spartito to see it`.

On Linux: a permitted page, else `notify-send` (see [Linux](#linux)), else the notice waits
for a page. The native window of `spartito app` is no such page there: Spartito shows the
notices itself with `notify-send`, and a click brings the window forward at the target.

**When the chosen way does not take it.** A background helper watches each notice left to
the window, the launcher's app or a page. The window or a page that has not claimed it
within 20 seconds, or the launcher's app within 15 seconds after it was started (or that
could not be started: `open` failed or took over 10 seconds), is skipped and the decision is
made again without it: the window's notice goes to the launcher's app, then a page, and so
on. The launcher's app never falls back to `osascript`: when it may not post (permission
withdrawn, or the system did not answer within 5 seconds) or a post fails, the notice waits
(step 5). With the window open, `osascript` is never used.

**Nothing waits for it, nothing is lost silently.** A turn or the decision tool waits at
most 0.25 seconds for the database to record the notice (about 0.3 seconds in all, measured),
and as long again for each later step it writes (`osascript` started: a step the database
does not take then is only in the log). When the database is busy longer, the notice is
written to `run/notices/<id>.json` and the helper records it as soon as the database is
free: it keeps trying for 10 minutes, opening the database again on each try, and its step
says how late (`recorded from the spool, <n> s after it was spooled`); a poll of the
window, of a page or of the posting app records such files too. How the helper ends is in
`logs/notices.log` either way: recorded, recorded by a poll, `gave up after <n> s … left in
the spool for the next poll`, or an `error` step with the error's text. The notice is then
decided as usual, so a busy database no longer sends it to `osascript` unrecorded. Every step is traced:
see [Reading the trace](#reading-the-trace).

**Allowing the page.** Once per browser and address: the **Enable notifications** button in
the dashboard's header (shown while the browser has not decided), or **Models and effort** ›
**Notifications** › **Allow in this browser**; then accept the browser's prompt. If the
browser blocks notifications for the page, allow them in its site settings (Chrome: the icon
left of the address; Safari: **Settings › Websites › Notifications**). Before the header
button, the only control sat inside the settings dialog, so a fresh browser profile never
asked and the page never showed a notification. The permission belongs to the address, port
included; `spartito app` therefore reuses the port of its last own server (`run/app-port`).
The native window of `spartito app` (opened with `?app=<id>&native=1`) has no **Enable
notifications** button and asks for nothing: the page there leaves the Web Notifications
API alone, and **Notifications** in its settings says that Spartito shows them itself. On
macOS its WebKit has no such API and the window process takes the notices; on Linux Qt
WebEngine has no dependable way to show them, so `notify-send` does (see [Linux](#linux)).
The browser window of `spartito app` (a Chromium-family browser in app mode) is a page like any
other and can be allowed.

**Allowing Spartito** (macOS). The window of `spartito app` started through the launcher asks
for permission for *Spartito* once it is shown, the first time it runs; allow it in the
prompt or later under **System Settings › Notifications › Spartito**. It asks again at each
start (the system prompts only once) and reads the authorization status every 2 seconds, so
a permission given or withdrawn later in System Settings, or an answer to the request that
never arrives (see [Why notices went to osascript](#why-notices-went-to-osascript)),
is seen within seconds. When allowed it writes `run/app-notify.json` (the bundle's identifier
and path): from then on step 2 is used when no window is open. A posting app that finds the
permission withdrawn removes the file and leaves the notices waiting (the next ones then go
to a page or `osascript`); one that gets no answer from the system in time leaves them waiting
and keeps the file.
Spartito posts notifications itself only through the interpreter copy of the launcher (see
[Launcher](#launcher-name-and-icon)): the notification centre refuses a process that only
names the bundle through `CFProcessPath`.

**Where the window's notification state shows.** One line, each time it changes, in three
places: under the agent list of the window (a quiet line), on the stderr of `spartito app` when
it runs in a terminal, and in `$ORCH_HOME/logs/app.log` (every line the window process says,
with time and pid; emptied at the start of a window when above 1 MB), which is where to look
when Spartito was started from the Dock or Finder. The lines (`<app>` is the launcher's name):

```
notifications from <app>: asked macOS for permission; until allowed, notices are shown in this window
notifications from <app>: asked macOS for permission — no answer yet (look for its prompt in Notification Centre); until allowed, notices are shown in this window
notifications from <app>: no answer to the request; the system's authorization status is <n>
notifications: <app> shows them; a click opens it there
notifications from <app>: not allowed (System Settings › Notifications › <app>); notices are shown in this window
notifications from <app>: macOS refused the request (<the system's error>); notices are shown in this window
notifications from <app>: not available (<why>); notices are shown in this window
```

`<why>` is `no launcher: run `spartito app install``, `not started as the launcher's app`,
`started without the launcher's copy of Python: run `spartito app install` again`,
`UserNotifications is not installed (install the extra spartito[app])`, or the error of
the notification centre. The second line comes once, 30 seconds after asking without an
answer; the third when the status read, not the request's answer, told the decision.
Besides these, `logs/app.log` gets `a click on a notification: opening <target>` for each
click that reaches the window, and `window ended: <why>` (a stop signal, `--close-after`, the
window closed, or the GUI loop ended) when the window goes.

**How the page and the window are found.** A permitted page polls the server every 4 seconds
and touches `run/page-notifier`; it counts as present for 15 seconds after its last poll. The
native window's process (macOS) writes `run/app-notifier` (its pid) and
`run/app-notifier.json` (its pid, the process's start time and its notification state) every
2 seconds, allowed to post or not, and removes both when it closes. It counts as present
while that process runs, however late the files are (a window that macOS throttles in the
background, or a poll that stalls): a pid whose process is gone, or that another process
started later now has, does not count. A window of older code (no `app-notifier.json`)
counts while its marker is under 15 seconds old. Some cases in detail:

- **Hidden tabs.** The poll runs from a small worker, so the throttling of hidden tabs does
  not stop it. A tab the browser freezes (for example Chrome's Energy Saver) stops polling;
  after 15 seconds the next path takes over.
- **Reloads.** A reloaded page carries on from the last notice it handled, so a notice
  recorded during the reload is still shown. A notice older than 10 minutes when a page first
  sees it is skipped, unless it was left to a page later (an escalation) or waits; so does
  the posting app of step 2.
- **The native window** shows no Web Notifications (its WebKit has none); it takes the notices
  itself (step 1 or 4) and writes its markers whether or not it may post them.
- **Off.** With `spartito config desktop-notifications off`, nothing is recorded and nothing is
  shown.
- **Off for one shell.** With `ORCH_DESKTOP_NOTIFICATIONS=off` in the environment (for CI and
  manual runs of a checkout in a temporary home), no path shows anything; each notice is still
  recorded and `spartito notices` gives the path `not-shown` with the reason.
- **Nested homes.** A Spartito started from an agent's turn that works on another home than
  the agent's own (for example `ORCH_HOME=<temporary> spartito spawn …` from an agent's shell)
  shows nothing either, and neither does anything it starts: the runner gives every turn its
  home in `ORCH_TURN_HOME`, and the outermost one is kept. Such a notice is recorded with the
  reason "started by an agent of another home".

**What a notification carries** (steps 1 and 2): the text, and a `userInfo` of
`{"home": <the home digest of /api/ping>, "target": "decision:<n>" | "<agent id>"}`. A click is
acted on only for the notification itself (not a dismissal), for this home, and for a target
that passes the deep-link check; it then becomes a deep-link request to the window (see
[Own window](#own-window)).

**`spartito app --notify`** is internal: the hidden start of step 2 (`open -n -g -j -a <bundle>
--args --notify`). It claims and posts the pending notices left to it or waiting (younger than
10 minutes; of those that waited, as in step 5), waits up to 5 seconds for the system, and exits; a click that reaches it in that
time opens the window at the target. A notice it cannot post waits.

### Notices inside the window

While the native window of `spartito app` is open on macOS and may not post notifications, it
shows the notices left to it itself (step 4: no launcher's app and no page took them), so
`osascript` (whose click opens Script Editor) stays quiet:

- a **banner** at the top right of the page: *Spartito* and the text. A click on it goes to the
  decision (the **Decisions** window on it) or the agent and closes the banner; **×** closes
  it. A closed banner stays closed after a reload of the page. There are three banners: open
  decisions take them first (the newest three), finished notices the ones left (the newest).
  A finished notice never pushes a decision out; one that finds only decisions there is not
  shown one by one but counted. On each poll (every 2 seconds) a notice whose decision is no
  longer open (answered, withdrawn), and one closed in the page (its banner's click or ×, or
  the count banner's for all it counts), leave the window; the banners they free go to what
  the count holds, by the same rule;
- a **count banner** above them for every notice in the window that has no banner: those that
  did not fit, those pushed out by newer ones, and the backlog's older finished ones (see
  below). It says how many, how many of them are open decisions, and that `spartito notices`
  lists them. A click on it opens the **Decisions** window on the newest open decision among
  them (it lists all open decisions); the finished ones are in `spartito notices`. A click
  or **×** closes the count: the notices it counted leave the window, and it comes back only
  for newer ones, counting only those;
- the **Dock**: when Spartito is not the active application, its icon bounces once and gets a
  badge with the number of notices shown since; the badge goes when the window becomes
  active (clicking it, the Dock icon, or a banner).

The window process takes each notice once, as a page does, so nothing else shows it again. A
notice it is allowed to post but the system refuses is shown inside the window too.

**Notices that waited for a window.** On its first poll the window takes the notices that are
still unclaimed (step 5: the launcher's app failed, nobody else could post) and shows them
inside, whether or not it may post (step 5's rule): open decisions and the newest three
finished notices, placed in the banners as above (decisions first), the other finished ones,
and those of the kept that find no banner, in the count banner, with one bounce of the Dock
icon. A decision notice whose decision is
no longer open is not shown (the trace says `dropped`). `logs/app.log` says `<n> notice(s) waited for a
window; shown in it`.

While the page does not take the notices (no hook in a page, `evaluate_js` failing), the window
keeps trying on each poll and keeps their number on the Dock's badge, also once the window is
active; after 10 seconds `logs/app.log` says `the window's page did not take <n> notice(s)`.
No `osascript` while the window is open.

**When the window closes** (its close button, Cmd-Q, a stop signal, `--close-after`), it claims
nothing more from that moment, removes its markers, and then hands over: the notices it took
but its page did not show (it releases only its own claims), and those still left to it. Each
is decided again without the window (the launcher's app, a permitted page, else `osascript`
with its clause when no launcher may post). A notice that a page or the launcher's app
claimed meanwhile stays theirs, so none comes twice; `logs/app.log` says `the window closed
with <n> notice(s) it had not shown; handed over (`spartito notices` shows where)`.

### Reading the trace

Every notice of the last day keeps its steps: in the database (table `notice_steps`) and,
one line per step, in `$ORCH_HOME/logs/notices.log` (moved to `notices.log.1` above 1 MB). The
log also has what the database did not take: a notice spooled while it was busy (named
`spool:<file>` until it is recorded), and a step it refused. `spartito notices` shows them:

```
#41  2026-11-12T09:14:03+00:00  decision  window-system  window:5120  2026-11-12T09:14:04.210+00:00  — the window process takes it (allowed); window pid 5120 (allowed, polled 0.4 s ago, running); no page; launcher ready
#40  2026-11-12T09:02:11+00:00  finished  waiting  notify:4987  2026-11-12T09:02:13.902+00:00  — post failed: UNErrorDomain 1: Notifications are not allowed for this application
```

Each line: the notice, when it was recorded, its kind, **how it was shown** (the path),
**by which process** (`<role>:<pid>`: `runner`, `mcp`, `web`, `cli`, `window`, `notify` for
the hidden posting app, `helper` for the background helper, `page` for a page through the
server), when that step happened, and **why**: the last fallback, escalation, release, drop
or error of the final outcome (one before a release that was followed by a new claim
belonged to an earlier attempt: it stays in the steps), else the claim's reason, else
the reason of the decision with what was present (the window's pid, state, the age of its
last poll and whether its process runs; the age of the page's poll; whether the launcher may
post, or why not).

The paths: `window-system` (posted by the window), `window-banner` (shown in a banner inside
the window), `window-count` (counted in the window's count banner: not shown one by one, or
pushed out of its banner by newer notices, the step before says it was shown),
`launcher-helper` (posted by the app started to post), `page`, `linux` (`notify-send`),
`osascript`, `waiting` (nobody took it yet), `dropped` (a decision answered before it was
shown; an older finished one of many that waited, counted in another's text; or `osascript`
could not be started), `not-shown` (the environment said so: `ORCH_DESKTOP_NOTIFICATIONS=off`,
or started by an agent of another home); `… (not taken yet)` while it is still left to that way; `not
recorded` for a notice a process of older code claimed (it records no steps: the line has
its `claimed_by`, else `unknown (older code)`, and when it was claimed). A notice of older code has no steps.

`spartito notices -v` adds the text and each step: `record` (at once, or `spooled: the store
was busy for <n> s`, then `recorded from the spool, <n> s after it was spooled`), `route`
(the decision), `escalate` (who did not take it in time, and the new decision), `claim`,
`shown`, `counted` (in the window's count banner, with why; one cut from what waited for the
window is claimed with a `claim` step on the path `window-count` first, so older code reads
that path too), `leave` (out of the window:
its decision is no longer open, or the page closed it), `fallback` (a post the system refused: shown inside instead; a helper that did not
start), `release` (back to waiting, with the error or the permission status), `drop`,
`error` (the background helper ended with an error, its text). `--json` gives the same as data.
So for a notice that came from `osascript` while the window was open, the line says which
process decided and what it saw: a window process not running, a stale window of older code,
or no launcher that may post and why.

### Why notices went to osascript

Before this change the window asked for permission during its launch and took notices only
once the answer to that request said "allowed"; until then, and whenever no answer came, it
left them to the next path, `osascript`, without a word. So with the launcher installed and
the window open, a decision notice still came from `osascript`, and its click opened Script
Editor. Measured with test launchers in `~/Applications` (the unsigned bundle with its ad hoc
signed interpreter copy, started by `open` as the Dock does), reading the authorization
status from a second process of the same bundle:

- **The identity is accepted; signing is not the cause.** In that process the notification
  centre is there, the status reads 0 (not determined), and a provisional request, which never
  prompts, was granted at once (status 0 → 3). With a decided status the old window code
  recorded the grant at once. So neither the unsigned bundle, nor the interpreter's signature
  that does not bind `Info.plist`, nor `LSUIElement` keeps the system from accepting the app;
  `spartito app install` therefore still signs only the interpreter copy and is unchanged.
- **The system can decide without the request's answer ever reaching the process, in both
  directions.** With the old code the status turned to 2 (allowed) within 8 seconds of the
  request, but the answer never arrived in the 35 seconds the window ran: no grant was
  written and every notice went to `osascript`. With the new code another test launcher's
  status turned to 1 (denied) two seconds after its request, again without an answer. On the
  installation where the defect was seen, the bundle's status was still 0 more than ten
  minutes after the window had opened in front.
- **Asking after the launch is not proven to help.** Asked once the window was shown, one
  test launcher got its answer within a second, the other none (above).

The window therefore no longer depends on the answer: it takes the notices from its start,
whatever its permission; reads the authorization status on each poll (every 2 seconds), so a
decision the system made without answering is seen; shows a notice inside the window
whenever it may not post it; and says each state in the places listed under
[Where the window's notification state shows](#notifications-with-click-through). It still
asks once the window is shown (in front, after the launch), as the moment that gave the
prompt the best chance in the runs, not as the fix.

### Linux

When `notify-send` is on `PATH`, a detached helper shows the notice:
`notify-send --app-name=Spartito --icon=<icon> --hint=string:desktop-entry:spartito -- Spartito
<text>`. The icon is the one `spartito app install` put into the hicolor theme, else the
package's PNG.

- **With click action**: when this `notify-send` lists `--action` and `--wait` in its help
  (libnotify 0.7.10 or later) and `DISPLAY` or `WAYLAND_DISPLAY` is set, the helper adds
  `--wait --action=default=Open` and, when the default action is invoked, starts
  `spartito app --decision <n>` or `spartito app --agent <id>`. It waits at most 30 minutes; a click
  after that does nothing.
- **Without**: the text ends with ` — open it: spartito app --decision <n>` (or `--agent <id>`).
- Without `DBUS_SESSION_BUS_ADDRESS` (a service started without it) the helper uses
  `unix:path=$XDG_RUNTIME_DIR/bus`.

Without `notify-send`, Linux shows none; a page in a browser still can, the native window of
`spartito app` cannot (install `notify-send`: the `libnotify-bin` package on Debian and
Ubuntu, `libnotify` on Fedora and Arch).

**Links.** `#<agent-id>` selects an agent and `#decision:<id>` opens a decision, for example
`http://127.0.0.1:8787/#decision:12`.

## Own window

```sh
spartito app [--port PORT] [--agent ID | --decision N]
```

`spartito app` shows the dashboard in its own window, at the agent or decision when given, and
returns when the window is closed.

**Server.** If a `spartito web` (or the background service) of this home is running, `spartito app`
uses it and leaves it running. It finds it through `$ORCH_HOME/run/web.json`, which a running
dashboard server keeps, and accepts it only when `/api/ping` on the loopback address at that
port reports this tool and this home. Otherwise `spartito app` serves the dashboard itself on
`127.0.0.1` and stops that server when the window is closed. Without `--port` it uses the port
of its last own server, kept in `run/app-port`, when nothing listens there (on 127.0.0.1 or
::1); otherwise a free one, which it then keeps. The page's preferences and notification
permission belong to its address, so they carry over. It prints `web UI: <url>`, followed by
` (the running server)` when it uses one.

**Window.** The first of these that works; each step that fails says so in one line on stderr
and moves on:

1. **Native window** (pywebview, the extra `app`): Cocoa with WebKit on macOS, Qt WebEngine on
   Linux.

   ```
   no native window: pywebview is not installed (install the extra spartito[app], then run `spartito app install`; see docs/desktop.md); trying a Chromium-family browser in app mode
   no native window: pywebview cannot start its GUI backend (<reason>); trying a Chromium-family browser in app mode
   no native window: pywebview failed (<first line of the error>); trying a Chromium-family browser in app mode
   ```

   The `then run `spartito app install`` part is left out when the launcher is installed. On
   macOS without the launcher, the Dock shows the window as *Python* with the tool's icon set
   at run time, and stderr says:
   ``the Dock shows this window as Python: `spartito app install` gives it Spartito's name and icon``.

2. **A Chromium-family browser in app mode** (a window without tabs or address bar): on macOS
   Google Chrome, Chromium, Microsoft Edge or Brave Browser in `/Applications` or
   `~/Applications`; on Linux `google-chrome`, `google-chrome-stable`, `chromium`,
   `chromium-browser`, `microsoft-edge` or `brave-browser` on `PATH`, in that order. It runs
   with its own profile in `$ORCH_HOME/run/app-profile`, separate from your browser profile
   (its own notification permission, no extensions or logins). The window carries the
   browser's name and icon, not Spartito's; the page in it can show Web Notifications (step 1
   of [Notifications](#notifications-with-click-through)), but Spartito posts none itself.
   When its window is closed, `spartito app` ends the browser.

   ```
   no Chromium-family browser found (Chrome, Chromium, Edge or Brave); opening the default browser
   <browser> did not start (<reason>); opening the default browser
   <browser> did not start (it exited at once, code N); opening the default browser
   ```

   When `spartito app` serves the dashboard itself, these lines end with
   `; serving <url> until Ctrl-C`.

3. **The default browser.** With its own server, `spartito app` keeps serving until Ctrl-C;
   with a running server it opens the page and returns. If no browser can be opened:
   `no browser could be opened: open <url>`, plus ` (serving until Ctrl-C)` with its own
   server.

On Linux without a display none of these is tried:
`no display (DISPLAY and WAYLAND_DISPLAY are unset): open <url> in a browser`, followed by
`, serving it until Ctrl-C` with its own server.

**One window per home; deep links.** A second `spartito app` for the same home asks the open
window to come to the front and, with `--agent` or `--decision`, to show that target, then
exits with code 0 after one line:

```
the dashboard window of this home is already open: brought it to the front[, showing <target>]
the dashboard window of this home is already open: asked its browser to bring it to the front[, showing <target>]
the dashboard window of this home is already open, but it could not be brought to the front (<why>)[; asked it to show <target>]
```

The request goes through `run/app-open.json` (`{"id", "target"}`, the target being
`decision:<n>`, an agent id or empty, checked against a strict pattern wherever it is read).
The `spartito app` that holds the window watches it, brings the window forward (the native window
on macOS counts only once it is the active application; the browser window through its
DevTools endpoint, not checked) and answers in `run/app-open.done`, which the second call
waits up to 5 seconds for. The page in the window, opened with `?app=<last id>`, gets requests
younger than 15 seconds with `/api/state` and applies each newer one once. A `spartito app` that
only serves (the default browser, or no display) does not hold the window lock, so the next
`spartito app` opens its own window on that server.

**Ending it.** Closing the window ends `spartito app` with exit code 0. SIGTERM and SIGHUP close
the window too, with exit code 128 + the signal (143, 129). If `--port` is taken:
`error: cannot listen on 127.0.0.1:<port>: <reason>` (exit code 1).

### Linux requirements for the native window

- **Size and glibc.** The extra installs PyQt6, PyQt6-WebEngine and the Qt libraries as
  wheels, about 200 MB. They need glibc 2.34 or later on x86_64, 2.39 or later on aarch64.
- **System libraries** (Debian and Ubuntu package names; Qt's "Qt for Linux – Requirements"):
  `libxcb-cursor0` (the one most often missing), `libxcb-icccm4`, `libxcb-image0`,
  `libxcb-keysyms1`, `libxcb-render-util0`, `libxcb-randr0`, `libxcb-shape0`,
  `libxcb-xfixes0`, `libxcb-xkb1`, `libxcb-sync1`, `libxcb-shm0`, `libxkbcommon0`,
  `libxkbcommon-x11-0`, `libfontconfig1`, `libfreetype6`, `libx11-6`, `libx11-xcb1`,
  `libxext6`, `libxrender1`, `libsm6`, `libice6`, `libglib2.0-0`. Qt WebEngine usually also
  needs `libgl1`, `libegl1`, `libnss3`, `libxcomposite1`, `libxdamage1`, `libxrandr2`,
  `libxtst6`, `libdbus-1-3` and `libasound2` (`libasound2t64` on newer Ubuntu). A run on
  Debian trixie (aarch64) showed `libQt6WebEngineCore.so.6` also needs `libsnappy1v5`,
  `libopus0`, `libxfixes3`, `libwebpdemux2`, `libwebpmux3`, `libminizip1t64` and
  `liblcms2-2`. To list what is missing:
  `ldd <venv>/lib/python3.*/site-packages/PyQt6/Qt6/lib/libQt6WebEngineCore.so.6 | grep 'not found'`.
- **Sandbox.** Qt WebEngine's sandbox needs seccomp-bpf and unprivileged user namespaces,
  which Ubuntu's AppArmor rules may restrict. It can be turned off with
  `QTWEBENGINE_DISABLE_SANDBOX=1` or `QTWEBENGINE_CHROMIUM_FLAGS=--no-sandbox`; `spartito app`
  never turns it off itself. In a container, a small `/dev/shm` can crash it (raise Docker's
  `--shm-size`), and Chromium refuses to run as root unless the sandbox is off.
- **Permissions.** pywebview 6.2's Qt handler for a page's permission requests passes plain
  numbers where PyQt6 wants its `PermissionPolicy` enum, so any request (such as a page asking
  to show notifications) ended in a `TypeError` in the window process. `spartito app` replaces
  that handler, once pywebview has loaded its Qt backend and before the window's page is made,
  with one that answers the same way (microphone and camera allowed, everything else,
  notifications included, denied) with the enum; `logs/app.log` says
  `Qt permissions: pywebview's handler replaced …`, or `… not found; left as it is` when a
  later pywebview has no such handler.
- **Backend.** `spartito app` tells pywebview to use Qt, unless `PYWEBVIEW_GUI` chooses another
  backend (for example `gtk`, whose packages the extra does not install). The backend is
  first started in a child process, because Qt ends the whole process when it cannot load its
  platform plugin; this adds about one Qt start-up to each `spartito app` (at most 60 s). When it
  fails, `spartito app` falls back to a browser.

## Launcher: name and icon

```sh
spartito app install
spartito app uninstall
```

`spartito app install` adds a launcher that starts `spartito app` like an application, with
Spartito's name and icon; running it again rewrites it. `spartito app uninstall` removes exactly
what install wrote (macOS: also `lsregister -u`, so the system forgets the bundle).

**Identifier.** `local.spartito.app` for the default home (`~/.orchestrator`), else
`local.spartito-<8 hex>.app` from a hash of the resolved `ORCH_HOME` (the label rule of the
service). Each command acts on the launcher of the current home; a launcher of another home
at the same place is replaced, never removed.

**Environment.** The launcher sets the variables `spartito service` captures (`PATH`, `LANG`,
`LC_*`, `ORCH_*`, `CLAUDE_CONFIG_DIR`, `CODEX_HOME`, as in the shell you install from); names
that are not valid shell names are left out. Run `spartito app install` again after changing them
or after moving or reinstalling the Python environment.

**macOS:** `~/Applications/Spartito.app`, a minimal bundle:

| file | what |
|---|---|
| `Contents/Info.plist` | name, identifier, icon, version; `LSUIElement` (no Dock icon until a window is shown) |
| `Contents/Resources/Spartito.icns` | the icon |
| `Contents/MacOS/Spartito` | a shell script that sets the environment and starts `spartito app` |
| `Contents/MacOS/spartito-python` | a copy of the Python interpreter, signed ad hoc with the bundle's identifier |
| `Contents/Resources/interpreter.json` | what the copy was made from: source, size, time, environment, Python version |

The copy is the binary that actually runs: for a framework build (python.org, Homebrew) the
one inside its `Python.app`, otherwise the interpreter the environment's `python` resolves to
(for example a uv-managed one). Install signs it (`codesign -s - --force --identifier <id>`),
runs it once to check that it finds this environment, and prints:

```
installed Spartito (local.spartito.app): ~/Applications/Spartito.app
spartito-python: a copy of <source> (Python <version>), signed as local.spartito.app, so Spartito can post notifications itself; run `spartito app install` again after upgrading Python
```

The script runs the copy with `__PYVENV_LAUNCHER__` naming the environment's interpreter, so
`sys.prefix` and the packages are the environment's, and the process is the bundle's own
executable: macOS shows it as *Spartito* with its icon, and the notification centre accepts
it. When no working copy can be made, install says
`notifications from Spartito itself are not available (<why>); without a dashboard page allowed to notify, an open window shows them inside itself, else osascript`
and the script runs the environment's interpreter with `CFProcessPath` set to the script:
CoreFoundation then takes the script for the process's executable, which gives name, icon and
identifier, but no notifications of its own.

**After a Python upgrade** the copy is stale: a Homebrew upgrade removes the versioned library
(`…/Cellar/python@3.14/<version>/…`) the copy loads, and any upgrade changes what the record
names. The script tries the copy first (a trial start); one that cannot start makes it start
the window the earlier way (`CFProcessPath`), so the window still opens, and that window says
on stderr
`the launcher's copy of Python is stale (<why>): run `spartito app install` again; until then Spartito posts no notifications itself`.
The script decides only by whether the copy starts: a copy that is stale by
`interpreter.json` but still starts is used, and that window posts Spartito's notifications
itself. A stale copy is refused by the hidden posting process (so with no window open the
notifications come from `osascript`) and by a terminal `spartito app`, which then uses
`CFProcessPath`. Running `spartito app install` again replaces it and says
`replaced the stale copy of the Python interpreter (<why>)`.

**From a terminal.** With this home's launcher installed and pywebview present, `spartito app`
re-executes itself as the launcher's app (same pid, terminal and exit code): through the copy
when it is current, else with `CFProcessPath`. Both variables naming the bundle are removed
after start, so agents and browsers started from the window do not take its identity. In the
real runs, `lsappinfo` showed the launcher's name, bundle identifier and bundle path for both
a Finder-style start (`open`) and a terminal start, type `Foreground` for the window and
`UIElement` (no Dock icon) for the hidden `--notify` start.

**Linux:** `$XDG_DATA_HOME/applications/spartito.desktop` (default `~/.local/share`), whose
`Exec=` runs `env <variables> <python> -m orchestrator app` (quoted as the Desktop Entry
specification asks), with `Icon=spartito`, `StartupWMClass=Spartito` and
`X-Orchestrator-Id=<identifier>`, and the icon at
`icons/hicolor/{192x192,512x512}/apps/spartito.png` of the same data home. The Qt window
reports the class `Spartito` and the desktop file name `spartito`.

Messages: `installed <name> (<identifier>): <path>`, `removed <name>: <path>`,
`<name> is not installed (<path>)`. Errors (exit code 1):
`error: <path> exists and was not written by `spartito app install`; it is left alone` and
`error: the launcher is supported on macOS and Linux, not <platform>`.

## Menu-bar item

```sh
spartito menubar
```

macOS only, with the extra `menubar`. `spartito menubar` shows a status item in the menu bar, with
no Dock icon, and runs until you quit it. Its title is `♪` when no agent is running and no
decision is open, otherwise `♪ <running agents> · <open decisions>`, for example `♪ 2 · 1`.

Its menu, from the top:

- `Running agents: N` and `Open decisions: M`;
- one line per open decision, `#<id>  <question>` with the question cut to 60 characters; at
  most 10, then `… and K more`;
- **Open Spartito** starts `spartito app` for the same home, as a separate process (or brings its
  window to the front);
- **Quit** ends the menu-bar item only; a `spartito app` window it opened stays open.

A click on a decision starts `spartito app --decision <id>`: it opens the window there, or brings
the open window to the front at that decision.

The counts are read from the home's database every 5 seconds; no dashboard server is needed
for them. One item per home: a second `spartito menubar` prints
`the menu-bar item of this home is already running` and exits with code 0. SIGTERM, SIGHUP and
Ctrl-C end it like **Quit**, with exit code 128 + the signal. It is not started automatically
at login; start it by hand.

Errors, each with exit code 1:

```
error: spartito menubar runs on macOS only
error: pyobjc-framework-Cocoa is not installed (install the extra spartito[menubar])
```

## Security

- **Loopback by default.** `spartito web` and `spartito service install` listen on `127.0.0.1` unless
  `--host` says otherwise, and warn when it is not a loopback address; the server of
  `spartito app` always listens on `127.0.0.1`. The dashboard still has no login, and the Host and
  origin checks are unchanged (see [usage.md](usage.md#dashboard)).
- **Notifications.** The poll and the claim of a notice are same-origin JSON POSTs, so another
  website cannot keep `osascript` quiet or take notices. A clicked notification's target, like
  a deep link's, is checked against a strict pattern before it is used, and one for another
  home is ignored. Texts from agents reach `osascript` and `notify-send` as arguments, never
  as script source.
- **Launcher.** The script and the desktop entry hold the captured variables in clear text, as
  the service's unit does; keep secrets out of them. The interpreter copy is signed ad hoc
  (no developer identity) and runs only this environment's code.
- **`/api/ping`** reports the tool, its version and a digest of the home, never a path.
- **`spartito app`** contacts only the loopback address of a recorded server, never the host
  written in `web.json`, and its requests to local servers ignore proxy settings and do not
  follow redirects.
- **Chromium app mode** opens a DevTools endpoint on a free loopback port, for the browser
  instance of `spartito app` only (its own profile). While the window is open, any local process
  can reach that endpoint and control that instance, as it can already reach the dashboard.
- **Unit files** of the service are readable by other users; see
  [What the service runs](#background-service).

## Optional dependencies

The extras `app` and `menubar` are not part of the default install, and none of these
packages is part of this repository.

| platform | package | licence |
|---|---|---|
| macOS, Linux | pywebview | BSD-3-Clause |
| macOS, Linux | bottle, proxy-tools (pulled by pywebview) | MIT |
| macOS | pyobjc-core and the pyobjc frameworks Cocoa, Quartz, WebKit, Security, UniformTypeIdentifiers | MIT |
| macOS | pyobjc-framework-UserNotifications 13.0 (extra `app`) | MIT |
| Linux | QtPy | MIT |
| Linux | packaging | Apache-2.0 or BSD-2-Clause |
| Linux | PyQt6, PyQt6-WebEngine | GPL-3.0-only |
| Linux | PyQt6-Qt6, PyQt6-WebEngine-Qt6 (the Qt libraries) | LGPL-3.0 |
| Linux | PyQt6-sip | BSD-2-Clause |

**Installing the extra on Linux brings GPL-3.0-only software onto the machine** (PyQt6 and
PyQt6-WebEngine), together with the Qt libraries under LGPL-3.0, about 200 MB in all. On
macOS no Qt package is installed.

The extra `menubar` installs pyobjc-framework-Cocoa (with pyobjc-core), MIT, on macOS only; on
other platforms it installs nothing.

## Known limitations

- **Real runs** (test launchers, a temporary home, a fake `osascript`): a test launcher asked,
  was allowed and posted a decision notice as itself (listed among the bundle's delivered
  notifications, `osascript` not called); clicks on posted notifications were logged in
  `logs/app.log` and brought the window to the front at the decision; the delegate, driven
  with a delivered notification's `userInfo`, did the same; banners inside a real WebKit
  window without a launcher appeared one per notice. On a live installation, a click on a
  notification Spartito had posted opened Spartito at its target. **Not verified by a real
  run:** the Dock
  bounce and badge as seen on screen, banners in the window of a launcher whose permission is
  off, and Spartito started by a click while no window is open (step 2).
- A denied permission for *Spartito* stays until it is changed in System Settings ›
  Notifications; meanwhile the open window shows the notices inside itself, and with no window
  open they come from a permitted page or `osascript`. A notice that waited because the
  launcher's app could not post it is shown in the window when it next opens, not by
  `osascript`.
- The system may decide the permission without answering the request (see
  [Why notices went to osascript](#why-notices-went-to-osascript)); the window then sees the
  decision within 2 seconds through the status, but its state line says "no answer to the
  request" first.
- A notice the window's page does not take (a page without its hook, `evaluate_js` failing)
  shows no banner while the window is open: it stays on the Dock's badge, `logs/app.log` says
  so, and it is decided again (the launcher's app, a page or `osascript`) when the window
  closes.
- A window that is force-quit hands nothing over: what it took but its page had not shown
  stays claimed and is not shown. What was left to it but not yet taken, at most what was
  recorded since its last poll (2 seconds), is moved on by the helper 20 seconds later; from
  the moment its process is gone, new notices take the next way at once.
- While the launcher's app keeps failing (it starts but cannot post, or `open` fails), each
  notice waits for the window, a page or the next start of that app; the trace says why.
- A launcher bundle under a temporary folder (`/tmp`, `/var/folders`) is refused by the
  notification centre.
- In the Chromium fallback window Spartito posts no notifications itself; the page in it can.
- A click on an `osascript` notification opens Script Editor and cannot be redirected; its text
  says where to look instead.
- After a Python upgrade, until `spartito app install` runs again, Spartito posts notifications
  itself only from a window whose stale copy still starts; otherwise an open window shows them
  inside itself, and with no window open `osascript` shows them.
- A notice recorded from the spool keeps the time it was first recorded; one that waited
  longer than 10 minutes there is skipped by pages and the posting app (the window still
  shows it when it opens). A database that stays busy beyond the helper's 10 minutes leaves
  the notice in the spool until the next poll of the window, a page or the posting app.
- On Linux with `xdg-open` but no browser installed, `spartito app` prints `xdg-open`'s errors
  instead of the "no browser could be opened" line.
- `spartito app uninstall` on Linux leaves the empty directories that install created.
- A Spartito started by a click while a `spartito app` that is not the launcher's app holds the
  window lock does not pass the click's target on.
- With the launcher, the native window's WebKit data is under
  `~/Library/WebKit/<identifier>` and `~/Library/Caches/<identifier>`, so the page's
  preferences start afresh once; `spartito app uninstall` does not remove them. Without it, they
  are under `org.python.python`, shared by every Python app that uses WebKit.
- One launcher place per machine and name: the launcher of another home at the same path is
  replaced.
- Qt's raise can un-maximize a maximized window.
- If a link in the browser window of `spartito app` opens another window, `spartito app` keeps running
  until that one is closed too.
- `spartito app` finds a running server only through `run/web.json`: a `spartito web` started before
  this version is found once it has been restarted.
- On Linux the backend check adds start-up time to every `spartito app`, and a stop signal that
  arrives during the check is acted on only after it.
- On macOS, Ctrl-C in the terminal of a `spartito app` with a native window ends the process
  without removing `run/web.json`; the stale record is harmless, as it is never reused.
- The systemd unit does not support a Python interpreter path that contains `$$` or `${`.
- On macOS, a re-install whose `launchctl bootstrap` fails leaves the service stopped until
  the next login or a successful install.
- The menu-bar item is not started at login.
- pywebview does not list Python 3.14 among its supported versions.
- The page's `theme-color` follows the system's colour scheme, not the dashboard's theme
  switch.
- There is no `/favicon.ico` or `/apple-touch-icon.png` at the root; browsers that ask for them
  get a 404. Which icon Safari uses for **Add to Dock** is not documented; the page offers the
  manifest icons and an `apple-touch-icon`.
- Linux paths (the desktop entry, the Qt window's class, `notify-send` with actions) are
  covered by tests with fakes only, not by a run on a Linux desktop.
