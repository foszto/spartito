# Desktop app

The dashboard can be used like a desktop application. None of this is needed to use
Spartito; each piece works on its own.

- [Installing the extras](#installing-the-extras)
- [Platform support](#platform-support)
- [Background service](#background-service)
- [Installable page](#installable-page)
- [Notifications with click-through](#notifications-with-click-through)
- [Own window](#own-window)
- [Menu-bar item](#menu-bar-item)
- [Security](#security)
- [Optional dependencies](#optional-dependencies)
- [Known limitations](#known-limitations)

## Installing the extras

The background service, the installable page and the notifications need nothing beyond the
default install. The native window of `orch app` needs the optional extra `app`, the
menu-bar item (macOS) the extra `menubar`:

```sh
uv tool install --editable '.[app,menubar]'    # instead of `uv tool install --editable .`
uv sync --extra app --extra menubar            # in a checkout, for `uv run orch …`
```

Without the extra, `orch app` falls back to a browser (see [Own window](#own-window)). On
Linux the extra installs PyQt6 and PyQt6-WebEngine, which are GPL-3.0-only, and the Qt
libraries they bring, which are LGPL-3.0 (see
[Optional dependencies](#optional-dependencies)). On Linux the extra `menubar` installs
nothing.

## Platform support

| | macOS | Linux |
|---|---|---|
| background service (`orch service`) | launchd agent | systemd user unit |
| installable page | Chrome (and other Chromium-family browsers), Safari | Chromium-family browsers |
| notification click-through | in an open dashboard page; without one, `osascript` without a click action | in an open dashboard page; without one, none |
| `orch app` native window | Cocoa (WebKit), extra `app` | Qt WebEngine, extra `app` |
| `orch app` browser fallback | Chromium-family browser in app mode, else the default browser | the same |
| menu-bar item (`orch menubar`) | yes, extra `menubar` | no |

## Background service

```sh
orch service install [--host 127.0.0.1] [--port 8787]
orch service status
orch service logs [--lines|-n 20]
orch service uninstall
```

`orch service install` runs `orch web` in the background: started at login and restarted when
it stops. It writes the unit file and (re)starts the service; running it again replaces the
unit and restarts the service, so it is also the way to apply a changed `--host` or `--port`,
or to restart after an upgrade. It prints the label, the URL, the unit file and where the logs
are. `--host` and `--port` are those of `orch web`; a non-loopback `--host` prints the same
warning as `orch web`.

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
`orch service` command acts on the service of the current `ORCH_HOME`.

**What the service runs.** `<python> -m orchestrator web --host H --port P`, with the Python
interpreter of the install and `$ORCH_HOME` as working directory. Its environment is fixed at
install time and holds only these variables of the shell you install from: `PATH`, `LANG`,
`LC_*`, `ORCH_*` (with `ORCH_HOME` set to the resolved home), `CLAUDE_CONFIG_DIR` and
`CODEX_HOME`. Consequences:

- Run `orch service install` again after changing `PATH` (for example a new location of
  `claude` or `codex`), a setting in these variables, or after moving or reinstalling the
  Python environment.
- Other variables, such as an API key exported in your shell, are not carried into the unit;
  the agents the service starts see only what the service sees.
- Unit files are readable by other users of the machine: keep secrets out of the variables
  above.

**Logs.** On macOS the log file is emptied at install when it is larger than 5 MB; launchd
does not rotate it otherwise. `orch service logs` prints the log file and its last lines
(`-n`, default 20). On Linux it prints the `journalctl` command.

**Status.** `orch service status` shows the label and platform, the unit file (or
`(not installed)`), the state (`loaded` or `active`, with the pid) and whether the dashboard
answers at the unit's address: `answers` only when `GET /api/ping` reports this tool with this
home; otherwise `something else answers (…)` (another program, or this tool for another
`ORCH_HOME`) or `nothing answers`.

**Linux without a login.** A systemd user service runs while you are logged in. To keep it
running without a session (for example after a reboot), enable lingering for your user once:
`loginctl enable-linger`. `orch service` does not do this.

**Failures.** A first install that fails removes the unit file it wrote; when a unit existed
before, the new one stays as written. `orch service uninstall` stops the service and removes
its unit file. If it cannot stop it, the unit file is kept and the command fails with exit
code 1: `error: could not unload <label>: …` on macOS, `error: could not stop <label>: …` on
Linux. On Linux, when the systemd user manager cannot be reached, the file is removed anyway,
a `warning: the systemd user manager could not be reached (…)` line says so, and the exit
code is 0. On other platforms every `orch service` command fails with `the background service
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

**`GET /api/ping`** tells who serves on a port, for `orch app` and `orch service status`:

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

**Shown by the page.** An open dashboard page (a tab, the installed app, or the browser
window of `orch app`) that is allowed to show notifications shows them itself as Web
Notifications, titled *Spartito*. A click brings the page to the front and opens the agent, or
the **Decisions** window on that decision with its answer box focused; a decision that is no
longer open selects the agent that asked it.

To allow it, once per browser and address: **Models and effort** (the settings button) ›
**Notifications** › **Allow in this browser**, then accept the browser's prompt. If the
browser blocks notifications for the page, allow them in its site settings (Chrome: the icon
left of the address; Safari: **Settings › Websites › Notifications**).

**Who shows a notice.** A permitted page polls the server every 4 seconds. While such a page
has polled within the last 15 seconds, new notices are left to it; each notice is shown once,
even with several pages open. Otherwise:

- macOS: `osascript` shows the plain notification, as before, without a click action;
- Linux: none is shown.

Some cases in detail:

- **Hidden tabs.** The poll runs from a small worker, so the throttling of hidden tabs does
  not stop it. A tab the browser freezes (for example Chrome's Energy Saver) stops polling;
  after 15 seconds `osascript` takes over again.
- **Reloads.** A reloaded page carries on from the last notice it handled, so a notice
  recorded during the reload is still shown. A notice older than 10 minutes when a page first
  sees it is skipped.
- **The native window of `orch app`** shows no Web Notifications: with it, macOS shows the
  plain notification and Linux none.
- **Off.** With `orch config desktop-notifications off`, nothing is recorded and nothing is
  shown, by the page or by `osascript`.

**Links.** `#<agent-id>` selects an agent and `#decision:<id>` opens a decision, for example
`http://127.0.0.1:8787/#decision:12`.

Considered and not built: clickable native notifications through `terminal-notifier`, which
would need an extra program.

## Own window

```sh
orch app [--port PORT]
```

`orch app` shows the dashboard in its own window and returns when the window is closed.

**Server.** If an `orch web` (or the background service) of this home is running, `orch app`
uses it and leaves it running. It finds it through `$ORCH_HOME/run/web.json`, which a running
dashboard server keeps, and accepts it only when `/api/ping` on the loopback address at that
port reports this tool and this home. Otherwise `orch app` serves the dashboard itself on
`127.0.0.1`, on a free port or `--port`, and stops that server when the window is closed. It
prints `web UI: <url>`, followed by ` (the running server)` when it uses one.

**Window.** The first of these that works; each step that fails says so in one line on stderr
and moves on:

1. **Native window** (pywebview, the extra `app`): Cocoa with WebKit on macOS, Qt WebEngine on
   Linux.

   ```
   no native window: pywebview is not installed (install the extra orchestrator[app]); trying a Chromium-family browser in app mode
   no native window: pywebview cannot start its GUI backend (<reason>); trying a Chromium-family browser in app mode
   no native window: pywebview failed (<first line of the error>); trying a Chromium-family browser in app mode
   ```

2. **A Chromium-family browser in app mode** (a window without tabs or address bar): on macOS
   Google Chrome, Chromium, Microsoft Edge or Brave Browser in `/Applications` or
   `~/Applications`; on Linux `google-chrome`, `google-chrome-stable`, `chromium`,
   `chromium-browser`, `microsoft-edge` or `brave-browser` on `PATH`, in that order. It runs
   with its own profile in `$ORCH_HOME/run/app-profile`, separate from your browser profile
   (its own notification permission, no extensions or logins). When its window is closed,
   `orch app` ends the browser.

   ```
   no Chromium-family browser found (Chrome, Chromium, Edge or Brave); opening the default browser
   <browser> did not start (<reason>); opening the default browser
   <browser> did not start (it exited at once, code N); opening the default browser
   ```

   When `orch app` serves the dashboard itself, these lines end with
   `; serving <url> until Ctrl-C`.

3. **The default browser.** With its own server, `orch app` keeps serving until Ctrl-C;
   with a running server it opens the page and returns. If no browser can be opened:
   `no browser could be opened: open <url>`, plus ` (serving until Ctrl-C)` with its own
   server.

On Linux without a display none of these is tried:
`no display (DISPLAY and WAYLAND_DISPLAY are unset): open <url> in a browser`, followed by
`, serving it until Ctrl-C` with its own server.

**One window per home.** A second `orch app` for the same home prints
`the dashboard window of this home is already open` and exits with code 0; it does not bring
the open window to the front. An `orch app` that only serves (the default browser, or no
display) does not hold this lock, so the next `orch app` opens its own window on that server.

**Ending it.** Closing the window ends `orch app` with exit code 0. SIGTERM and SIGHUP close
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
  `libxtst6`, `libdbus-1-3` and `libasound2` (`libasound2t64` on newer Ubuntu).
- **Sandbox.** Qt WebEngine's sandbox needs seccomp-bpf and unprivileged user namespaces,
  which Ubuntu's AppArmor rules may restrict. It can be turned off with
  `QTWEBENGINE_DISABLE_SANDBOX=1` or `QTWEBENGINE_CHROMIUM_FLAGS=--no-sandbox`; `orch app`
  never turns it off itself. In a container, a small `/dev/shm` can crash it (raise Docker's
  `--shm-size`), and Chromium refuses to run as root unless the sandbox is off.
- **Backend.** `orch app` tells pywebview to use Qt, unless `PYWEBVIEW_GUI` chooses another
  backend (for example `gtk`, whose packages the extra does not install). The backend is
  first started in a child process, because Qt ends the whole process when it cannot load its
  platform plugin; this adds about one Qt start-up to each `orch app` (at most 60 s). When it
  fails, `orch app` falls back to a browser.

## Menu-bar item

```sh
orch menubar
```

macOS only, with the extra `menubar`. `orch menubar` shows a status item in the menu bar, with
no Dock icon, and runs until you quit it. Its title is `♪` when no agent is running and no
decision is open, otherwise `♪ <running agents> · <open decisions>`, for example `♪ 2 · 1`.

Its menu, from the top:

- `Running agents: N` and `Open decisions: M`;
- one line per open decision, `#<id>  <question>` with the question cut to 60 characters; at
  most 10, then `… and K more`;
- **Open Spartito** starts `orch app` for the same home, as a separate process;
- **Quit** ends the menu-bar item only; an `orch app` window it opened stays open.

A click on a decision opens `<dashboard URL>#decision:<id>` in the default browser when a
dashboard server of this home is running (found as `orch app` finds it), otherwise it starts
`orch app`.

The counts are read from the home's database every 5 seconds; no dashboard server is needed
for them. One item per home: a second `orch menubar` prints
`the menu-bar item of this home is already running` and exits with code 0. SIGTERM, SIGHUP and
Ctrl-C end it like **Quit**, with exit code 128 + the signal. It is not started automatically
at login; start it by hand.

Errors, each with exit code 1:

```
error: orch menubar runs on macOS only
error: pyobjc-framework-Cocoa is not installed (install the extra orchestrator[menubar])
```

## Security

- **Loopback by default.** `orch web` and `orch service install` listen on `127.0.0.1` unless
  `--host` says otherwise, and warn when it is not a loopback address; the server of
  `orch app` always listens on `127.0.0.1`. The dashboard still has no login, and the Host and
  origin checks are unchanged (see [usage.md](usage.md#dashboard)).
- **Notifications.** The poll and the claim of a notice are same-origin JSON POSTs, so another
  website cannot keep `osascript` quiet or take notices.
- **`/api/ping`** reports the tool, its version and a digest of the home, never a path.
- **`orch app`** contacts only the loopback address of a recorded server, never the host
  written in `web.json`, and its requests to local servers ignore proxy settings and do not
  follow redirects.
- **Chromium app mode** opens a DevTools endpoint on a free loopback port, for the browser
  instance of `orch app` only (its own profile). While the window is open, any local process
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

- A second `orch app` does not bring the open window to the front.
- If a link in the browser window of `orch app` opens another window, `orch app` keeps running
  until that one is closed too.
- The page's preferences (such as the theme) and the notification permission belong to its
  address, port included. With `orch app` serving on a free port they start afresh each time;
  they carry over with a fixed `--port` or a running server.
- On macOS the native window's WebKit data goes to `~/Library/WebKit/org.python.python` and
  `~/Library/Caches/org.python.python`, shared by every Python app that uses WebKit.
- `orch app` finds a running server only through `run/web.json`: an `orch web` started before
  this version is found once it has been restarted.
- On Linux the backend check adds start-up time to every `orch app`, and a stop signal that
  arrives during the check is acted on only after it.
- On macOS, Ctrl-C in the terminal of an `orch app` with a native window ends the process
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
- The only place to allow notifications is the **Notifications** section of the settings.
- Notifications shown by `osascript` have no click action.
