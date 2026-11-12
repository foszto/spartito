"""`spartito app install` / `spartito app uninstall`: the launcher's files, written into temporary
directories only (conftest points the default directory there and fakes lsregister)."""

from __future__ import annotations

import json
import os
import platform
import plistlib
import shlex
import stat
import subprocess
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

from orchestrator import __version__, autostart, cli
from orchestrator.gui import launcher

HOME = '/tmp/it\'s a "home"'  # a space and both quotes
ENV = {"ORCH_HOME": HOME, "PATH": "/usr/bin:/bin", "LANG": "en_US.UTF-8"}


def item(kind: str, root: Path, name: str = "Spartito", argv=None) -> launcher.Launcher:
    return launcher.Launcher(
        kind=kind,
        name=name,
        root=root,
        identifier=f"local.{launcher.slug(name)}-0123abcd.app",
        env=ENV,
        argv=tuple(argv or ("/opt/py 3/bin/python", "-m", "orchestrator", "app")),
    )


def invoke(*args: str):
    return CliRunner().invoke(cli.app, ["app", *args])


# -- names -----------------------------------------------------------------------------------


def test_identifier_follows_the_label_rule_of_the_service():
    default = Path(autostart.DEFAULT_HOME).expanduser().resolve()
    other = Path("/tmp/elsewhere")
    assert launcher.identifier("Spartito", default) == "local.spartito.app"
    assert autostart.default_label(default) == "local.spartito.web"
    digest = autostart.home_hash(other)[:8]
    assert launcher.identifier("Spartito", other) == f"local.spartito-{digest}.app"
    assert autostart.default_label(other) == f"local.spartito-{digest}.web"
    assert launcher.identifier("Spartito Test", other) == f"local.spartito-test-{digest}.app"


def test_launcher_of_this_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    monkeypatch.setenv("ORCH_AGENT_ID", "worker-1")  # a marker of the caller: not carried
    monkeypatch.setenv("ORCH_TURN", "7")
    monkeypatch.setenv("ORCH_NOTIFY_DELAY", "0")
    monkeypatch.setenv("SECRET_TOKEN", "x")
    found = launcher.launcher(platform="darwin")
    home = autostart.home()
    assert found.name == "Spartito"
    assert found.root == launcher.default_dir("mac")
    assert found.identifier == f"local.spartito-{autostart.home_hash(home)[:8]}.app"
    assert found.env == autostart.captured_env(home)
    assert found.env["ORCH_HOME"] == str(home)
    assert {"ORCH_AGENT_ID", "ORCH_TURN", "SECRET_TOKEN"}.isdisjoint(found.env)
    assert found.argv == (sys.executable, "-m", "orchestrator", "app")
    assert found.path == launcher.default_dir("mac") / "Spartito.app"
    linux = launcher.launcher("Spartito Test", tmp_path, platform="linux")
    assert linux.path == tmp_path / "applications" / "spartito-test.desktop"


def test_default_directories(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    monkeypatch.undo()  # the real default_dir, not conftest's; nothing is written here
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    assert launcher.default_dir("mac") == tmp_path / "Applications"
    assert launcher.default_dir("linux") == tmp_path / ".local" / "share"
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    assert launcher.default_dir("linux") == tmp_path / "data"


@pytest.mark.parametrize("name", ["", ".hidden", "-x", "a/b", "a\nb", "x" * 65])
def test_bad_names_are_refused(name: str):
    with pytest.raises(launcher.LauncherError, match="invalid name"):
        launcher.launcher(name, platform="darwin")


def test_other_platforms_are_refused():
    with pytest.raises(launcher.LauncherError) as exc:
        launcher.launcher(platform="win32")
    assert str(exc.value) == "the launcher is supported on macOS and Linux, not win32"


# -- what it writes ---------------------------------------------------------------------------


def test_info_plist(tmp_path: Path):
    info = plistlib.loads(launcher.render_info_plist(item("mac", tmp_path)))
    assert info == {
        "CFBundleDevelopmentRegion": "en",
        "CFBundleDisplayName": "Spartito",
        "CFBundleExecutable": "Spartito",
        "CFBundleIconFile": "Spartito.icns",
        "CFBundleIdentifier": "local.spartito-0123abcd.app",
        "CFBundleInfoDictionaryVersion": "6.0",
        "CFBundleName": "Spartito",
        "CFBundlePackageType": "APPL",
        "CFBundleShortVersionString": __version__,
        "CFBundleVersion": __version__,
        "LSApplicationCategoryType": "public.app-category.developer-tools",
        "LSUIElement": True,
        "NSHighResolutionCapable": True,
    }


def test_script_golden():
    root = Path('/Users/some one/it\'s "apps"')
    script = launcher.render_script(item("mac", root, "Spartito Test"))
    assert script == (
        "#!/bin/sh\n"
        "# Spartito Test: written by `spartito app install`, removed by `spartito app uninstall`.\n"
        "# CFProcessPath makes the Python process count as this app (name, icon, identifier).\n"
        "export CFProcessPath='/Users/some one/it'\"'\"'s \"apps\"/Spartito Test.app/Contents"
        "/MacOS/Spartito Test'\n"
        "export ORCH_HOME='/tmp/it'\"'\"'s a \"home\"'\n"
        "export PATH=/usr/bin:/bin\n"
        "export LANG=en_US.UTF-8\n"
        "exec '/opt/py 3/bin/python' -m orchestrator app \"$@\"\n"
    )


def test_script_runs_with_its_environment_and_arguments(tmp_path: Path):
    """The script, run by sh: the environment it sets and the arguments it passes on reach
    the command (a Python that prints them instead of `spartito app`)."""
    show = "import os, sys; print(os.environ['ORCH_HOME']); print(os.environ['CFProcessPath']); print(sys.argv[1:])"
    root = tmp_path / 'it\'s "apps"'
    written = item("mac", root, argv=(sys.executable, "-c", show))
    launcher.install(written)
    done = subprocess.run(
        [str(written.executable), "--decision", "5"],
        capture_output=True,
        text=True,
        env={"PATH": os.environ["PATH"]},
        check=True,
    )
    assert done.stdout.splitlines() == [HOME, str(written.executable), "['--decision', '5']"]


BAD_NAMES = {"ORCH_FOO-BAR": "x", "ORCH_$(touch pwned)": "y", "ORCH_A B": "z", "1ORCH": "w"}


def test_only_valid_variable_names_are_written(tmp_path: Path):
    """A captured name that is no shell or `env` name is left out of the script and of the
    desktop entry: it could break the script or run as code at every start."""
    show = "import os; print(sorted(k for k in os.environ if k.startswith(('ORCH', '1'))))"
    written = launcher.Launcher(
        **{
            **item("mac", tmp_path, argv=(sys.executable, "-c", show)).__dict__,
            "env": {**ENV, **BAD_NAMES, "ORCH_OK_1": "v"},
        }
    )
    script = launcher.render_script(written)
    assert "export ORCH_OK_1=v\n" in script
    assert not any(name in script for name in BAD_NAMES)
    launcher.install(written)
    done = subprocess.run(
        [str(written.executable)],
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env={"PATH": os.environ["PATH"]},
        check=True,
    )
    assert done.stdout.strip() == "['ORCH_HOME', 'ORCH_OK_1']"
    assert not (tmp_path / "pwned").exists()
    entry = launcher.render_desktop(launcher.Launcher(**{**written.__dict__, "kind": "linux"}))
    (line,) = [x for x in entry.splitlines() if x.startswith("Exec=")]
    assert "ORCH_OK_1=v" in unquote_exec(line.removeprefix("Exec="))
    assert not any(name in line for name in BAD_NAMES)


def test_desktop_entry_golden(tmp_path: Path):
    entry = launcher.render_desktop(item("linux", tmp_path))
    assert entry == (
        "[Desktop Entry]\n"
        "Type=Application\n"
        "Version=1.5\n"
        "Name=Spartito\n"
        "Comment=The dashboard of your coding agents\n"
        'Exec=env "ORCH_HOME=/tmp/it\'s a \\\\"home\\\\"" PATH=/usr/bin:/bin LANG=en_US.UTF-8'
        ' "/opt/py 3/bin/python" -m orchestrator app\n'
        "Icon=spartito\n"
        "Terminal=false\n"
        "Categories=Development;\n"
        "StartupWMClass=Spartito\n"
        "X-Orchestrator-Id=local.spartito-0123abcd.app\n"
    )


@pytest.mark.parametrize(
    ("arg", "written"),
    [
        ("plain", "plain"),
        ("", '""'),
        ("a b", '"a b"'),
        ('say "hi"', '"say \\\\"hi\\\\""'),  # \" in the quoted form, its \ doubled as a string
        ("$HOME", '"\\\\$HOME"'),
        ("back\\slash", '"back\\\\\\\\slash"'),
        ("100%", "100%%"),
        ("tick`", '"tick\\\\`"'),
        ("x=1", "x=1"),
    ],
)
def test_exec_arguments_are_quoted_as_the_spec_says(arg: str, written: str):
    assert launcher.exec_arg(arg) == written


def unquote_exec(value: str) -> list[str]:
    """Read an Exec= value back: string escapes first, then the quoting rules, as a desktop
    does (`%%` back to `%`)."""
    value = value.replace("%%", "%")
    text, i = "", 0
    while i < len(value):  # string-value escapes
        if value[i] == "\\" and i + 1 < len(value):
            text += {"s": " ", "n": "\n", "t": "\t", "r": "\r", "\\": "\\"}.get(
                value[i + 1], value[i + 1]
            )
            i += 2
        else:
            text += value[i]
            i += 1
    return shlex.split(text)  # double quotes with \" \` \$ \\ inside: as sh reads them


def test_desktop_entry_reads_back_to_the_command(tmp_path: Path):
    entry = launcher.render_desktop(item("linux", tmp_path))
    (line,) = [x for x in entry.splitlines() if x.startswith("Exec=")]
    assert unquote_exec(line.removeprefix("Exec=")) == [
        "env",
        f"ORCH_HOME={HOME}",
        "PATH=/usr/bin:/bin",
        "LANG=en_US.UTF-8",
        "/opt/py 3/bin/python",
        "-m",
        "orchestrator",
        "app",
    ]


# -- install / uninstall ----------------------------------------------------------------------


def tree(root: Path) -> dict[str, tuple[bytes, int]]:
    return {
        str(path.relative_to(root)): (path.read_bytes(), stat.S_IMODE(path.stat().st_mode))
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def test_mac_install_is_idempotent_and_uninstall_removes_it(tmp_path: Path):
    root = tmp_path / "Applications"
    root.mkdir()
    written = item("mac", root)
    assert launcher.install(written) == root / "Spartito.app"
    first = tree(root)
    assert sorted(first) == [
        "Spartito.app/Contents/Info.plist",
        "Spartito.app/Contents/MacOS/Spartito",
        "Spartito.app/Contents/Resources/Spartito.icns",
    ]
    assert first["Spartito.app/Contents/MacOS/Spartito"][1] == 0o755
    assert first["Spartito.app/Contents/Resources/Spartito.icns"][0] == launcher.ICNS.read_bytes()
    assert launcher.installed(written)
    assert launcher.install(written) == root / "Spartito.app"
    assert tree(root) == first
    assert launcher.uninstall(written) == root / "Spartito.app"
    assert list(root.iterdir()) == []  # the directory itself stays
    assert launcher.uninstall(written) is None
    assert not launcher.installed(written)


def test_linux_install_is_idempotent_and_uninstall_removes_it(tmp_path: Path):
    written = item("linux", tmp_path, "Spartito Test")
    assert launcher.install(written) == tmp_path / "applications" / "spartito-test.desktop"
    first = tree(tmp_path)
    assert sorted(first) == [
        "applications/spartito-test.desktop",
        "icons/hicolor/192x192/apps/spartito-test.png",
        "icons/hicolor/512x512/apps/spartito-test.png",
    ]
    assert launcher.install(written) and tree(tmp_path) == first
    assert launcher.uninstall(written) == written.path
    assert tree(tmp_path) == {}
    assert launcher.uninstall(written) is None


def test_uninstall_keeps_what_install_did_not_write(tmp_path: Path):
    written = item("mac", tmp_path)
    launcher.install(written)
    extra = written.path / "Contents" / "Resources" / "notes.txt"
    extra.write_text("mine")
    assert launcher.uninstall(written) == written.path
    assert tree(tmp_path) == {"Spartito.app/Contents/Resources/notes.txt": (b"mine", 0o644)}


def test_uninstall_unregisters_the_bundle(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    seen = []
    monkeypatch.setattr(launcher, "unregister", lambda bundle: seen.append(bundle))
    written = item("mac", tmp_path)
    launcher.install(written)
    launcher.uninstall(written)
    assert seen == [written.path]


@pytest.mark.parametrize("kind", ["mac", "linux"])
def test_something_else_at_the_place_is_left_alone(tmp_path: Path, kind: str):
    written = item(kind, tmp_path)
    if kind == "mac":
        (written.path / "Contents").mkdir(parents=True)
        (written.path / "Contents" / "Info.plist").write_bytes(
            plistlib.dumps({"CFBundleIdentifier": "com.example.spartito"})
        )
    else:
        written.path.parent.mkdir(parents=True)
        written.path.write_text("[Desktop Entry]\nName=Spartito\n")
    before = tree(tmp_path)
    with pytest.raises(launcher.LauncherError, match="was not written by `spartito app install`"):
        launcher.install(written)
    assert launcher.uninstall(written) is None
    assert tree(tmp_path) == before


def test_another_homes_launcher_is_replaced_but_not_removed(tmp_path: Path):
    other = item("mac", tmp_path)
    mine = launcher.Launcher(**{**other.__dict__, "identifier": "local.spartito-feedbeef.app"})
    launcher.install(other)
    assert launcher.uninstall(mine) is None  # not this home's
    assert not launcher.installed(mine)
    launcher.install(mine)
    assert launcher.installed(mine) and not launcher.installed(other)


# -- the commands ------------------------------------------------------------------------------


@pytest.mark.parametrize("platform", ["darwin", "linux"])
def test_install_and_uninstall_commands(tmp_path: Path, monkeypatch, platform: str):
    monkeypatch.setattr(launcher, "this_platform", lambda: platform)
    target = tmp_path / "apps"
    options = ["--name", "Spartito Test", "--dir", str(target)]
    found = launcher.launcher("Spartito Test", target)
    expected = (
        target / "Spartito Test.app"
        if platform == "darwin"
        else target / "applications" / "spartito-test.desktop"
    )
    assert found.path == expected
    for _ in range(2):
        result = invoke("install", *options)
        assert result.exit_code == 0, result.output
        note = (
            "notifications from Spartito Test itself are not available (not checked in tests);"
            " without a dashboard page allowed to notify, an open window shows them inside itself, else osascript\n"
            if platform == "darwin"
            else ""
        )
        installed = f"installed Spartito Test ({found.identifier}): {expected}\n"
        assert result.stdout == installed + note
    assert found.identifier.startswith("local.spartito-test-")
    result = invoke("uninstall", *options)
    assert result.exit_code == 0, result.output
    assert result.stdout == f"removed Spartito Test: {expected}\n"
    result = invoke("uninstall", *options)
    assert result.stdout == f"Spartito Test is not installed ({expected})\n"
    assert not any(p.is_file() for p in target.rglob("*"))


def test_default_install_goes_to_the_default_directory(no_real_launcher: Path, monkeypatch):
    monkeypatch.setattr(launcher, "this_platform", lambda: "darwin")
    result = invoke("install")
    assert result.exit_code == 0, result.output
    assert (no_real_launcher / "mac" / "Spartito.app" / "Contents" / "MacOS" / "Spartito").is_file()
    assert invoke("uninstall").exit_code == 0
    assert not (no_real_launcher / "mac" / "Spartito.app").exists()


def test_install_refusal_is_an_error(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(launcher, "this_platform", lambda: "darwin")
    (tmp_path / "Spartito.app").mkdir()
    result = invoke("install", "--dir", str(tmp_path))
    assert result.exit_code == 1
    assert result.stderr.startswith(f"error: {tmp_path / 'Spartito.app'} exists and was not")


def test_unsupported_platform(monkeypatch):
    monkeypatch.setattr(launcher, "this_platform", lambda: "win32")
    result = invoke("install")
    assert result.exit_code == 1
    assert result.stderr == "error: the launcher is supported on macOS and Linux, not win32\n"


def test_app_help_lists_the_commands():
    result = invoke("--help")
    assert result.exit_code == 0
    for word in ("install", "uninstall", "--agent", "--decision", "--port"):
        assert word in result.stdout
    for hidden in ("--close-after", "--echo-location", "--name", "--dir"):
        assert hidden not in result.stdout


def test_the_icon_is_package_data():
    """In the source tree next to the module, so the wheel carries it (uv_build packs every
    file of the package)."""
    assert Path(launcher.__file__).parent / "icon.icns" == launcher.ICNS
    assert launcher.ICNS.read_bytes()[:4] == b"icns"
    assert all(path.is_file() for path in launcher.LINUX_ICONS.values())


def test_no_test_can_reach_a_real_window_or_browser():
    """conftest's guard (this module adds no fixture of its own): no pywebview, no browser."""
    from orchestrator.gui import window

    with pytest.raises(ImportError):
        import webview  # noqa: F401
    assert window.find_chromium() is None
    assert window.webbrowser.open("http://127.0.0.1:1/") is True  # opens nothing


# -- the interpreter copy (macOS) ------------------------------------------------------------

REAL_CHECK_COPY, REAL_CODESIGN = launcher.check_copy, launcher.codesign  # before conftest's fakes
REAL_SOURCE = launcher.interpreter_source


@pytest.fixture
def working_copy(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict:
    """A fake interpreter to copy (`source`), the environment's interpreter linking to it
    (`env`), and a codesign that notes its calls and works."""
    source = tmp_path / "python" / "bin" / "python3.14"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"interpreter 3.14.6")
    env = tmp_path / "venv" / "bin" / "python"
    env.parent.mkdir(parents=True)
    env.symlink_to(source)
    signed: list[tuple[Path, str]] = []
    monkeypatch.setattr(launcher, "interpreter_source", lambda: source)
    monkeypatch.setattr(
        launcher, "codesign", lambda path, identity: signed.append((path, identity))
    )
    monkeypatch.setattr(launcher, "check_copy", lambda copy, environment: None)
    return {"source": source, "env": env, "signed": signed}


def test_mac_install_makes_a_signed_copy_and_runs_it_as_the_bundle(
    tmp_path: Path, working_copy: dict
):
    root = tmp_path / "Applications"
    written = item("mac", root, "Spartito Test", argv=(str(working_copy["env"]), "-m",
                                                       "orchestrator", "app"))  # fmt: skip
    notes: list[str] = []
    assert launcher.install(written, notes) == written.path
    files = tree(root)
    assert sorted(files) == [
        "Spartito Test.app/Contents/Info.plist",
        "Spartito Test.app/Contents/MacOS/Spartito Test",
        "Spartito Test.app/Contents/MacOS/spartito-python",
        "Spartito Test.app/Contents/Resources/Spartito Test.icns",
        "Spartito Test.app/Contents/Resources/interpreter.json",
    ]
    assert files["Spartito Test.app/Contents/MacOS/spartito-python"][0] == b"interpreter 3.14.6"
    ((signed_path, identity),) = working_copy["signed"]
    assert identity == written.identifier and signed_path.parent == written.interpreter.parent
    record = json.loads(files["Spartito Test.app/Contents/Resources/interpreter.json"][0])
    assert record["source"] == str(working_copy["source"])
    assert record["environment"] == str(working_copy["env"])
    assert record["resolved"] == os.path.realpath(working_copy["source"])
    assert record["identifier"] == written.identifier
    assert notes == [
        f"spartito-python: a copy of {working_copy['source']} (Python {record['version']}),"
        f" signed as {written.identifier}, so Spartito Test can post notifications itself;"
        " run `spartito app install` again after upgrading Python"
    ]
    script = files["Spartito Test.app/Contents/MacOS/Spartito Test"][0].decode()
    copy, env = shlex.quote(str(written.interpreter)), shlex.quote(str(working_copy["env"]))
    assert script == (
        "#!/bin/sh\n"
        "# Spartito Test: written by `spartito app install`, removed by `spartito app uninstall`.\n"
        "# The interpreter copy in this bundle makes the process this app (name, icon,\n"
        "# identifier, notifications); __PYVENV_LAUNCHER__ gives it the environment.\n"
        "export ORCH_HOME='/tmp/it'\"'\"'s a \"home\"'\n"
        "export PATH=/usr/bin:/bin\n"
        "export LANG=en_US.UTF-8\n"
        f"if __PYVENV_LAUNCHER__={env} {copy} -I -c '' >/dev/null 2>&1; then\n"
        f"    export ORCH_APP_BUNDLE={shlex.quote(str(written.path))}\n"
        f"    export __PYVENV_LAUNCHER__={env}\n"
        f'    exec {copy} -m orchestrator app "$@"\n'
        "fi\n"
        "# The copy does not start (a Python upgrade removed what it loads): the earlier way,\n"
        "# without notifications of its own until `spartito app install` runs again.\n"
        "export ORCH_APP_COPY_FAILED=1\n"
        f"export CFProcessPath={shlex.quote(str(written.executable))}\n"
        f'exec {env} -m orchestrator app "$@"\n'
    )
    assert launcher.interpreter(written) == written.interpreter
    assert launcher.uninstall(written) == written.path
    assert tree(root) == {}


def test_the_bundle_script_runs_the_copy_with_the_environment(tmp_path: Path, working_copy):
    """The written script, run by sh: the copy (here a script that prints what it got) gets
    the environment's interpreter and the bundle in its environment, and the arguments."""
    working_copy["source"].write_text(
        '#!/bin/sh\necho "$__PYVENV_LAUNCHER__|$ORCH_APP_BUNDLE|$ORCH_HOME|$*"\n'
    )
    working_copy["source"].chmod(0o755)
    written = item("mac", tmp_path / "apps", "Spartito Test",
                   argv=(str(working_copy["env"]), "-m", "orchestrator", "app"))  # fmt: skip
    launcher.install(written)
    done = subprocess.run(
        ["sh", str(written.executable), "--notify"], capture_output=True, text=True, timeout=30
    )
    assert done.stdout == (
        f"{working_copy['env']}|{written.path}|{HOME}|-m orchestrator app --notify\n"
    )


def test_a_copy_that_no_longer_starts_falls_back_to_the_environments_python(
    tmp_path: Path, working_copy
):
    """After a Python upgrade the copy cannot load its library: the script notices it by a
    trial start and runs the environment's interpreter with CFProcessPath instead, so the
    window still opens (without notifications of its own)."""
    env = working_copy["env"]
    env.unlink()
    env.write_text(
        '#!/bin/sh\necho "python|$CFProcessPath|${ORCH_APP_BUNDLE-unset}|'
        '${__PYVENV_LAUNCHER__-unset}|$ORCH_APP_COPY_FAILED|$*"\n'
    )
    env.chmod(0o755)
    working_copy["source"].write_text("#!/bin/sh\necho copy\n")
    working_copy["source"].chmod(0o755)
    written = item("mac", tmp_path / "apps", "Spartito Test",
                   argv=(str(env), "-m", "orchestrator", "app"))  # fmt: skip
    launcher.install(written)
    run = ["sh", str(written.executable), "--decision", "3"]
    assert subprocess.run(run, capture_output=True, text=True, timeout=30).stdout == "copy\n"
    written.interpreter.write_text("#!/bin/sh\necho 'dyld: Library not loaded' >&2\nexit 134\n")
    done = subprocess.run(run, capture_output=True, text=True, timeout=30)
    assert done.stdout == (
        f"python|{written.executable}|unset|unset|1|-m orchestrator app --decision 3\n"
    )
    assert done.stderr == ""


def test_a_copy_that_does_not_work_is_removed_and_the_script_uses_cfprocesspath(
    tmp_path: Path, working_copy: dict, monkeypatch: pytest.MonkeyPatch
):
    written = item("mac", tmp_path, argv=(str(working_copy["env"]), "-m", "orchestrator", "app"))
    launcher.install(written)
    assert written.interpreter.exists()
    monkeypatch.setattr(launcher, "check_copy", lambda copy, environment: "the copy did not run")
    notes: list[str] = []
    launcher.install(written, notes)
    assert notes == [
        "notifications from Spartito itself are not available (the copy did not run);"
        " without a dashboard page allowed to notify, an open window shows them inside itself, else osascript"
    ]
    assert not written.interpreter.exists() and not written.record.exists()
    assert not list(written.interpreter.parent.glob(".spartito-python.*"))
    assert launcher.render_script(written) == written.executable.read_text()
    assert launcher.interpreter(written) is None


def test_a_python_upgrade_makes_the_copy_stale_and_install_repairs_it(
    tmp_path: Path, working_copy: dict, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(launcher, "this_platform", lambda: "darwin")
    monkeypatch.setattr(sys, "executable", str(working_copy["env"]))
    options = ["--name", "Spartito Test", "--dir", str(tmp_path / "apps")]
    assert invoke("install", *options).exit_code == 0
    written = launcher.launcher("Spartito Test", tmp_path / "apps")
    assert launcher.interpreter(written) == written.interpreter
    working_copy["source"].write_bytes(b"interpreter 3.14.7, a little longer")  # an upgrade
    assert launcher.interpreter(written) is None
    result = invoke("install", *options)
    assert result.exit_code == 0, result.output
    lines = result.stdout.splitlines()
    assert lines[1] == (
        "replaced the stale copy of the Python interpreter (Python changed since"
        f" `spartito app install` (it copied {platform.python_version()}))"
    )
    assert lines[2].startswith("spartito-python: a copy of ")
    assert launcher.interpreter(written) == written.interpreter
    assert written.interpreter.read_bytes() == b"interpreter 3.14.7, a little longer"
    assert len(invoke("install", *options).stdout.splitlines()) == 2  # current: nothing to say


def test_uninstall_forgets_the_grant_of_its_own_bundle_only(tmp_path: Path, working_copy):
    from orchestrator import desktop

    written = item("mac", tmp_path, argv=(str(working_copy["env"]), "-m", "orchestrator", "app"))
    launcher.install(written)
    desktop.record_grant(written.identifier, tmp_path / "Other.app")
    launcher.uninstall(written)
    assert desktop.granted_bundle() == tmp_path / "Other.app"
    launcher.install(written)
    desktop.record_grant(written.identifier, written.path)
    launcher.uninstall(written)
    assert desktop.granted_bundle() is None


def test_codesign_signs_ad_hoc_with_the_identifier(tmp_path: Path, monkeypatch):
    fake = tmp_path / "codesign"
    fake.write_text(
        '#!/bin/sh\nprintf "%s\\n" "$@" > "$0.args"\n[ -z "$FAIL" ] || { echo "$FAIL" >&2; exit 1; }\n'
    )
    fake.chmod(0o755)
    monkeypatch.setattr(launcher, "CODESIGN", str(fake))
    assert REAL_CODESIGN(tmp_path / "copy", "local.x.app") is None
    assert (tmp_path / "codesign.args").read_text().splitlines() == [
        "-s", "-", "--force", "--identifier", "local.x.app", str(tmp_path / "copy")
    ]  # fmt: skip
    monkeypatch.setenv("FAIL", "copy: invalid format")
    assert (
        REAL_CODESIGN(tmp_path / "copy", "local.x.app") == "codesign failed: copy: invalid format"
    )
    monkeypatch.setattr(launcher, "CODESIGN", str(tmp_path / "missing"))
    assert REAL_CODESIGN(tmp_path / "copy", "local.x.app").startswith("codesign did not run: ")


@pytest.mark.parametrize(
    "body, problem",
    [
        ('echo "$PREFIX"', None),
        ("echo /somewhere/else", "the copy found another environment (/somewhere/else)"),
        (
            "echo 'dyld: Library not loaded' >&2; exit 6",
            "the copy did not run: dyld: Library not loaded",
        ),
        ("exit 3", "the copy did not run: exit code 3"),
    ],
)
def test_check_copy_runs_it_as_the_bundle_will(tmp_path: Path, monkeypatch, body, problem):
    copy = tmp_path / "copy"
    copy.write_text(
        '#!/bin/sh\n[ "$__PYVENV_LAUNCHER__" = /env/bin/python ] || exit 9\n'
        f'[ "$1 $2" = "-I -c" ] || exit 8\n{body}\n'
    )
    copy.chmod(0o755)
    monkeypatch.setenv("PREFIX", sys.prefix)
    assert REAL_CHECK_COPY(copy, "/env/bin/python") == problem


def test_the_copy_source_is_the_real_interpreter():
    """Not a symlink or a launcher stub: the binary that runs (a framework build's lives in
    its Python.app)."""
    source = REAL_SOURCE()
    assert source.is_file() and os.access(source, os.X_OK) and not source.is_symlink()
    assert source == Path(os.path.realpath(source))
