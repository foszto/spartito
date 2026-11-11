"""`orch app install` / `orch app uninstall`: a launcher that starts `orch app` like an
application, with the tool's name and icon.

- macOS: a minimal bundle `~/Applications/Spartito.app`: `Contents/Info.plist`,
  `Contents/Resources/Spartito.icns` and `Contents/MacOS/Spartito`, a shell script that sets
  the environment and starts `orch app`. With it, a copy of the Python interpreter,
  `Contents/MacOS/spartito-python`, signed ad hoc with the bundle's identifier: the script
  runs that copy with `__PYVENV_LAUNCHER__` naming the environment's interpreter (so
  `sys.prefix` and the packages are the environment's), and the process is this bundle's
  executable: main bundle, name, icon, and an identity the notification centre accepts
  (gui/notifier.py). `Contents/Resources/interpreter.json` records what was copied; the copy
  is stale after a Python upgrade (desktop.interpreter_problem), and `orch app install` again
  replaces it. When no working copy can be made, the script exports `CFProcessPath` instead
  (CoreFoundation takes it for the process's executable): name, icon and identifier, but no
  notifications of its own. The bundle is an agent app (LSUIElement), so a process that only
  posts notifications shows no Dock icon; the window turns itself into a regular app.
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
import json
import os
import platform
import plistlib
import re
import shlex
import shutil
import subprocess
import sys
import sysconfig
from dataclasses import dataclass
from pathlib import Path

from .. import __version__, autostart, config, desktop

GUI = Path(__file__).parent
ICNS = GUI / "icon.icns"
STATIC = GUI.parent / "web" / "static"
LINUX_ICONS = {"192x192": STATIC / "icon-192.png", "512x512": STATIC / "icon-512.png"}
WM_CLASS = config.PROJECT_NAME  # what the Qt window reports as its class (window.name_qt_app)
ID_KEY = "X-Orchestrator-Id"  # the identifier in the desktop entry
ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
NAME_RULE = re.compile(r"[A-Za-z0-9][A-Za-z0-9 ._-]{0,63}")
LSREGISTER = Path(
    "/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework"
    "/Support/lsregister"
)
CODESIGN = "/usr/bin/codesign"
COPY_NAME = "spartito-python"  # the interpreter copy in Contents/MacOS
BUNDLE_VAR = "ORCH_APP_BUNDLE"  # set by the bundle's script: this process is the bundle's
CHECK_TIMEOUT = 60.0  # seconds for the first run of the interpreter copy
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

    @property
    def interpreter(self) -> Path:
        """macOS: the bundle's own copy of the Python interpreter."""
        return self.path / "Contents" / "MacOS" / COPY_NAME

    @property
    def record(self) -> Path:
        """macOS: what the copy was made from."""
        return self.path.joinpath(*desktop.INTERPRETER_RECORD)


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
            "LSUIElement": True,  # no Dock icon until a window is shown (window.py)
            "NSHighResolutionCapable": True,
        }
    )


def written_env(item: Launcher) -> dict[str, str]:
    """The variables the launcher sets: only names a shell and `env` take as names
    (ENV_NAME); another name could break the script or run as code, so it is left out."""
    return {name: value for name, value in item.env.items() if ENV_NAME.fullmatch(name)}


def render_script(item: Launcher, own_python: bool = False) -> str:
    """The bundle's executable: runs the interpreter copy as the environment's interpreter
    (`own_python`), else the environment's interpreter with CFProcessPath."""
    if own_python:
        identity = {BUNDLE_VAR: str(item.path), "__PYVENV_LAUNCHER__": item.argv[0]}
        argv = (str(item.interpreter), *item.argv[1:])
        why = "# The interpreter copy in this bundle makes the process this app (name, icon,\n"
        why += "# identifier, notifications); __PYVENV_LAUNCHER__ gives it the environment.\n"
    else:
        identity = {"CFProcessPath": str(item.executable)}
        argv = item.argv
        why = (
            "# CFProcessPath makes the Python process count as this app (name, icon, identifier).\n"
        )
    exports = "".join(
        f"export {name}={shlex.quote(value)}\n"
        for name, value in (
            {**written_env(item), **identity} if own_python else {**identity, **written_env(item)}
        ).items()
    )
    command = " ".join(shlex.quote(arg) for arg in argv)
    return (
        "#!/bin/sh\n"
        f"# {item.name}: written by `orch app install`, removed by `orch app uninstall`.\n"
        f"{why}"
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
    env = [f"{name}={value}" for name, value in written_env(item).items()]
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


def files(item: Launcher, own_python: bool = False) -> dict[Path, tuple[bytes, int]]:
    """Every file install writes from its content: path -> (content, mode). On macOS also
    the interpreter copy and its record (`make_interpreter`)."""
    if item.kind == "mac":
        contents = item.path / "Contents"
        return {
            contents / "Info.plist": (render_info_plist(item), 0o644),
            contents / "Resources" / f"{item.name}.icns": (ICNS.read_bytes(), 0o644),
            item.executable: (render_script(item, own_python).encode(), 0o755),
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


def bundle_identifier(bundle: Path) -> str:
    """The identifier in a bundle's Info.plist ("" when it cannot be read)."""
    try:
        info = plistlib.loads((bundle / "Contents" / "Info.plist").read_bytes())
    except OSError, ValueError, plistlib.InvalidFileException:
        return ""
    return str(info.get("CFBundleIdentifier", ""))


def installed(item: Launcher) -> bool:
    """This home's launcher is there (on macOS: with its executable script)."""
    if installed_id(item) != item.identifier:
        return False
    return item.kind != "mac" or os.access(item.executable, os.X_OK)


def install(item: Launcher, notes: list[str] | None = None) -> Path:
    """Write the launcher (again: the same files, replaced); returns where it is. On macOS
    with the interpreter copy when one works; `notes` gets a line on how that went."""
    if installed_id(item) == "":
        raise LauncherError(
            f"{item.path} exists and was not written by `orch app install`; it is left alone"
        )
    own_python = False
    if item.kind == "mac":
        own_python, note = make_interpreter(item)
        if notes is not None:
            notes.extend(note)
    for path, (content, mode) in files(item, own_python).items():
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        temp.write_bytes(content)
        temp.chmod(mode)
        temp.replace(path)
    return item.path


def interpreter_source() -> Path:
    """The interpreter binary to copy: a framework build's real one (inside its Python.app),
    else the one the environment's interpreter resolves to."""
    if sysconfig.get_config_var("PYTHONFRAMEWORK"):
        app = Path(sys.base_prefix) / "Resources" / "Python.app" / "Contents" / "MacOS" / "Python"
        if app.is_file():
            return Path(os.path.realpath(app))
    return Path(os.path.realpath(getattr(sys, "_base_executable", None) or sys.executable))


def codesign(path: Path, identity: str) -> str | None:
    """Sign `path` ad hoc as `identity`; None when that worked, else why not."""
    argv = [CODESIGN, "-s", "-", "--force", "--identifier", identity, str(path)]
    try:
        done = subprocess.run(argv, capture_output=True, text=True, timeout=60, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return f"codesign did not run: {exc}"
    if done.returncode:
        lines = (done.stderr or done.stdout).strip().splitlines()
        return f"codesign failed: {lines[-1] if lines else f'exit code {done.returncode}'}"
    return None


def check_copy(copy: Path, environment: str) -> str | None:
    """Run the copy as the bundle's script will; None when it finds this environment (its
    sys.prefix and this package), else what went wrong."""
    code = "import sys, orchestrator; print(sys.prefix)"
    env = {**os.environ, "__PYVENV_LAUNCHER__": environment}
    env.pop("PYTHONHOME", None)
    try:
        done = subprocess.run(
            [str(copy), "-I", "-c", code],
            capture_output=True,
            text=True,
            env=env,
            timeout=CHECK_TIMEOUT,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return f"the copy did not run: {exc}"
    if done.returncode:
        lines = done.stderr.strip().splitlines()
        return f"the copy did not run: {lines[-1] if lines else f'exit code {done.returncode}'}"
    if done.stdout.strip() != sys.prefix:
        return f"the copy found another environment ({done.stdout.strip()})"
    return None


def make_interpreter(item: Launcher) -> tuple[bool, list[str]]:
    """Copy, sign and check the bundle's interpreter, and record what it was made from;
    returns whether it works, and the lines to say about it. A copy that does not work is
    removed (the script then uses CFProcessPath)."""
    stale = item.record.exists() and desktop.interpreter_problem(item.path)
    source = interpreter_source()
    environment = item.argv[0]
    item.interpreter.parent.mkdir(parents=True, exist_ok=True)
    temp = item.interpreter.with_name(f".{COPY_NAME}.{os.getpid()}.tmp")
    try:
        shutil.copy2(source, temp)
        problem = codesign(temp, item.identifier) or check_copy(temp, environment)
    except OSError as exc:
        problem = f"cannot copy {source}: {exc.strerror or exc}"
    if problem:
        temp.unlink(missing_ok=True)
        item.interpreter.unlink(missing_ok=True)
        item.record.unlink(missing_ok=True)
        return False, [
            f"notifications from {item.name} itself are not available ({problem});"
            " notifications without an open dashboard page come from osascript"
        ]
    temp.replace(item.interpreter)
    stat = source.stat()
    record = {
        "copy": COPY_NAME,
        "source": str(source),
        "size": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
        "environment": environment,
        "resolved": os.path.realpath(environment),
        "version": platform.python_version(),
        "identifier": item.identifier,
    }
    item.record.parent.mkdir(parents=True, exist_ok=True)
    temp_record = item.record.with_name(f".{item.record.name}.{os.getpid()}.tmp")
    temp_record.write_text(json.dumps(record, indent=1) + "\n")
    temp_record.replace(item.record)
    lines = []
    if stale:
        lines.append(f"replaced the stale copy of the Python interpreter ({stale})")
    lines.append(
        f"{COPY_NAME}: a copy of {source} (Python {record['version']}), signed as"
        f" {item.identifier}, so {item.name} can post notifications itself;"
        " run `orch app install` again after upgrading Python"
    )
    return True, lines


def interpreter(item: Launcher) -> Path | None:
    """The bundle's interpreter copy when it is there and current."""
    if item.kind != "mac" or desktop.interpreter_problem(item.path):
        return None
    return item.interpreter


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
        desktop.forget_grant(item.path)
    written = [*files(item)]
    if item.kind == "mac":
        written += [item.interpreter, item.record]
        written += item.interpreter.parent.glob(f".{COPY_NAME}.*.tmp")
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
