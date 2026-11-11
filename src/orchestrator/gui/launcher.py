"""`orch app install` / `orch app uninstall`: a launcher that starts `orch app` like an
application, with the tool's name and icon.

- macOS: a minimal bundle `~/Applications/Spartito.app`: `Contents/Info.plist`,
  `Contents/Resources/Spartito.icns` and `Contents/MacOS/Spartito`, a shell script that sets
  the environment and execs `<python> -m orchestrator app`. It exports `CFProcessPath` (the
  path of that script): CoreFoundation then takes it for the process's executable, so the
  Python process counts as this bundle (main bundle, bundle identifier, name and icon in the
  Dock), started from Finder or from a terminal (`relaunch_env`).
- Linux: `$XDG_DATA_HOME/applications/spartito.desktop` (default `~/.local/share`), whose
  `Exec=` runs the same command through `env` (a desktop entry has no environment of its
  own), and the icon in the hicolor theme of the same data home.

The bundle identifier, `local.spartito.app` for the default home and `local.spartito-<8 hex>.app`
for another ORCH_HOME (the label rule of `orch service`), is written into the launcher, so a
launcher is known as this home's, another home's or none of this tool's. The environment is
the one `orch service` captures (autostart.captured_env).
"""

from __future__ import annotations

import contextlib
import os
import plistlib
import re
import shlex
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from .. import __version__, autostart, config

GUI = Path(__file__).parent
ICNS = GUI / "icon.icns"
STATIC = GUI.parent / "web" / "static"
LINUX_ICONS = {"192x192": STATIC / "icon-192.png", "512x512": STATIC / "icon-512.png"}
WM_CLASS = config.PROJECT_NAME  # what the Qt window reports as its class (window.name_qt_app)
ID_KEY = "X-Orchestrator-Id"  # the identifier in the desktop entry
NAME_RULE = re.compile(r"[A-Za-z0-9][A-Za-z0-9 ._-]{0,63}")
LSREGISTER = Path(
    "/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework"
    "/Support/lsregister"
)
# Characters that make an Exec= argument need quotes (Desktop Entry spec, "The Exec key").
EXEC_RESERVED = frozenset(" \t\n\"'\\><~|&;$*?#()`")


class LauncherError(Exception):
    """A clear one-line reason the launcher cannot be installed or removed."""


def this_platform() -> str:
    return sys.platform


def kind_of(platform: str | None = None) -> str:
    platform = platform or this_platform()
    if platform == "darwin":
        return "mac"
    if platform.startswith("linux"):
        return "linux"
    raise LauncherError(f"the launcher is supported on macOS and Linux, not {platform}")


def slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "app"


def identifier(name: str, home: Path) -> str:
    """`local.<slug of name>.app` for the default home, `local.<slug>-<8 hex>.app` otherwise."""
    if home == Path(autostart.DEFAULT_HOME).expanduser().resolve():
        return f"local.{slug(name)}.app"
    return f"local.{slug(name)}-{autostart.home_hash(home)[:8]}.app"


def default_dir(kind: str) -> Path:
    if kind == "mac":
        return Path.home() / "Applications"
    return Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")


@dataclass(frozen=True)
class Launcher:
    kind: str  # "mac" or "linux"
    name: str  # what the system shows
    root: Path  # ~/Applications, or the XDG data home
    identifier: str
    env: dict[str, str]
    argv: tuple[str, ...]

    @property
    def path(self) -> Path:
        """The launcher itself: the bundle, or the desktop entry."""
        if self.kind == "mac":
            return self.root / f"{self.name}.app"
        return self.root / "applications" / f"{slug(self.name)}.desktop"

    @property
    def executable(self) -> Path:
        """macOS: the bundle's script, which CFProcessPath names."""
        return self.path / "Contents" / "MacOS" / self.name


def launcher(
    name: str | None = None,
    directory: Path | None = None,
    platform: str | None = None,
    environ: dict[str, str] | None = None,
) -> Launcher:
    name = config.PROJECT_NAME if name is None else name
    if not NAME_RULE.fullmatch(name):
        raise LauncherError(
            f"invalid name {name!r}: use up to 64 letters, digits, spaces, '.', '_' or '-',"
            " starting with a letter or digit"
        )
    kind = kind_of(platform)
    home = autostart.home()
    return Launcher(
        kind=kind,
        name=name,
        root=Path(directory) if directory else default_dir(kind),
        identifier=identifier(name, home),
        env=autostart.captured_env(home, environ),
        argv=(sys.executable, "-m", "orchestrator", "app"),
    )


# -- what install writes ---------------------------------------------------------------------


def render_info_plist(item: Launcher) -> bytes:
    return plistlib.dumps(
        {
            "CFBundleDevelopmentRegion": "en",
            "CFBundleDisplayName": item.name,
            "CFBundleExecutable": item.name,
            "CFBundleIconFile": f"{item.name}.icns",
            "CFBundleIdentifier": item.identifier,
            "CFBundleInfoDictionaryVersion": "6.0",
            "CFBundleName": item.name,
            "CFBundlePackageType": "APPL",
            "CFBundleShortVersionString": __version__,
            "CFBundleVersion": __version__,
            "LSApplicationCategoryType": "public.app-category.developer-tools",
            "NSHighResolutionCapable": True,
        }
    )


def render_script(item: Launcher) -> str:
    exports = "".join(
        f"export {name}={shlex.quote(value)}\n"
        for name, value in {"CFProcessPath": str(item.executable), **item.env}.items()
    )
    command = " ".join(shlex.quote(arg) for arg in item.argv)
    return (
        "#!/bin/sh\n"
        f"# {item.name}: written by `orch app install`, removed by `orch app uninstall`.\n"
        "# CFProcessPath makes the Python process count as this app (name, icon, identifier).\n"
        f"{exports}"
        f'exec {command} "$@"\n'
    )


def exec_arg(arg: str) -> str:
    """One argument of an Exec= key: quoted when it holds a reserved character (`"`, `` ` ``,
    `$` and `\\` escaped inside), then written as a string value (`\\` doubled, newline and
    tab escaped) with `%` doubled, as the Desktop Entry specification asks."""
    if not arg or any(c in EXEC_RESERVED for c in arg):
        arg = '"' + re.sub(r'(["`$\\])', r"\\\1", arg) + '"'
    for plain, escaped in (("\\", "\\\\"), ("\n", "\\n"), ("\t", "\\t"), ("\r", "\\r")):
        arg = arg.replace(plain, escaped)
    return arg.replace("%", "%%")


def render_desktop(item: Launcher) -> str:
    env = [f"{name}={value}" for name, value in item.env.items()]
    command = " ".join(exec_arg(arg) for arg in ("env", *env, *item.argv))
    return (
        "[Desktop Entry]\n"
        "Type=Application\n"
        "Version=1.5\n"
        f"Name={item.name}\n"
        "Comment=The dashboard of your coding agents\n"
        f"Exec={command}\n"
        f"Icon={slug(item.name)}\n"
        "Terminal=false\n"
        "Categories=Development;\n"
        f"StartupWMClass={WM_CLASS}\n"
        f"{ID_KEY}={item.identifier}\n"
    )


def files(item: Launcher) -> dict[Path, tuple[bytes, int]]:
    """Every file install writes: path -> (content, mode)."""
    if item.kind == "mac":
        contents = item.path / "Contents"
        return {
            contents / "Info.plist": (render_info_plist(item), 0o644),
            contents / "Resources" / f"{item.name}.icns": (ICNS.read_bytes(), 0o644),
            item.executable: (render_script(item).encode(), 0o755),
        }
    icons = item.root / "icons" / "hicolor"
    written = {item.path: (render_desktop(item).encode(), 0o644)}
    for size, source in LINUX_ICONS.items():
        written[icons / size / "apps" / f"{slug(item.name)}.png"] = (source.read_bytes(), 0o644)
    return written


# -- install / uninstall ---------------------------------------------------------------------


def installed_id(item: Launcher) -> str | None:
    """The identifier written in the launcher at `item.path`: None when there is nothing,
    "" when something is there that this tool did not write."""
    if not item.path.exists():
        return None
    try:
        if item.kind == "mac":
            info = plistlib.loads((item.path / "Contents" / "Info.plist").read_bytes())
            found = str(info.get("CFBundleIdentifier", ""))
        else:
            lines = item.path.read_text().splitlines()
            found = next((x.split("=", 1)[1] for x in lines if x.startswith(f"{ID_KEY}=")), "")
    except OSError, ValueError, plistlib.InvalidFileException:
        return ""
    return (
        found
        if re.fullmatch(rf"local\.{re.escape(slug(item.name))}(-[0-9a-f]{{8}})?\.app", found)
        else ""
    )


def installed(item: Launcher) -> bool:
    """This home's launcher is there (on macOS: with its executable script)."""
    if installed_id(item) != item.identifier:
        return False
    return item.kind != "mac" or os.access(item.executable, os.X_OK)


def install(item: Launcher) -> Path:
    """Write the launcher (again: the same files, replaced); returns where it is."""
    if installed_id(item) == "":
        raise LauncherError(
            f"{item.path} exists and was not written by `orch app install`; it is left alone"
        )
    for path, (content, mode) in files(item).items():
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        temp.write_bytes(content)
        temp.chmod(mode)
        temp.replace(path)
    return item.path


def unregister(bundle: Path) -> None:
    """Forget the bundle in LaunchServices (it registers one when it is first opened); best
    effort."""
    if not LSREGISTER.exists():
        return
    with contextlib.suppress(OSError, subprocess.TimeoutExpired):
        subprocess.run(
            [str(LSREGISTER), "-u", str(bundle)], capture_output=True, timeout=30, check=False
        )


def uninstall(item: Launcher) -> Path | None:
    """Remove the files install wrote for this home (on macOS the bundle's directories too,
    once empty); returns the launcher's path, None when this home's is not installed."""
    if installed_id(item) != item.identifier:
        return None
    if item.kind == "mac":
        unregister(item.path)
    written = files(item)
    for path in written:
        path.unlink(missing_ok=True)
    if item.kind == "mac":
        contents = item.path / "Contents"
        for directory in (contents / "MacOS", contents / "Resources", contents, item.path):
            try:
                directory.rmdir()
            except OSError:
                break  # not empty: something install did not write
    return item.path
