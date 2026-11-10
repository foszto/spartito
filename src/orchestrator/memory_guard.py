"""PreToolUse hook: Claude Code's file-writing tools may change only the session's memory directory.

The loadout key `memory` (see loadout.py) gives a role Write and Edit together with this hook
(providers/claude.py). The CLI runs it before each of those calls as
`<python> -I memory_guard.py || exit 2` and passes the call as JSON on stdin. Exit 0 hands the
call on to the CLI's own permission checks; exit 2 refuses it and shows stderr to the model.
Fails closed: input that is not such a call, a tool it does not know, a path it cannot place,
or a crash refuses the write; the `|| exit 2` covers an interpreter that does not even start.

The memory directory is derived the way Claude Code derives its default (read from and
verified with 2.1.322): <config dir>/projects/<key>/memory/, where the config dir is
$CLAUDE_CONFIG_DIR or ~/.claude and <key> is the sanitized main checkout of the git
repository holding $CLAUDE_PROJECT_DIR (the CLI sets it for hooks to the session's launch
directory; a later `cd` does not move it), or of that directory itself outside git. Where the
CLI could place it differently, the guard refuses every write instead of guessing: a symlinked
`.git`, a worktree whose bare common directory holds a `.git`, and the environment overrides
in OVERRIDES. A directory moved with `autoMemoryDirectory` is not followed: writes there are
refused, and only the default directory stays writable.

Paths are taken only as the CLI would certainly take them: absolute, without surrounding
whitespace; `~`, relative paths and anything else are refused.

Standard library only, and no imports from the package: it runs as a script in isolated mode.
"""

from __future__ import annotations

import json
import os
import sys
import unicodedata
from collections.abc import Mapping
from typing import Any

# Tool name -> the input field naming the file it writes.
PATH_FIELDS = {
    "Write": "file_path",
    "Edit": "file_path",
    "MultiEdit": "file_path",
    "NotebookEdit": "notebook_path",
}
MATCHER = "|".join(PATH_FIELDS)
KEY_LIMIT = 200  # longer keys are cut and get a hash suffix, as in the CLI
# Move the memory directory or rename the project's key in ways the guard does not follow.
OVERRIDES = (
    "CLAUDE_CODE_PROJECT_DIR_NAME",
    "CLAUDE_CODE_REMOTE_MEMORY_DIR",
    "CLAUDE_COWORK_MEMORY_PATH_OVERRIDE",
)


def _utf16_units(text: str) -> list[int]:
    data = text.encode("utf-16-le", "surrogatepass")
    return [int.from_bytes(data[i : i + 2], "little") for i in range(0, len(data), 2)]


def _base36(number: int) -> str:
    digits = "0123456789abcdefghijklmnopqrstuvwxyz"
    out = ""
    while True:
        number, rest = divmod(number, 36)
        out = digits[rest] + out
        if not number:
            return out


def project_key(path: str) -> str:
    """The CLI's directory name for a path: every UTF-16 unit that is not [A-Za-z0-9]
    becomes `-`; past 200 characters it is cut and a hash of the path appended."""
    units = _utf16_units(path)
    key = "".join(
        chr(unit) if chr(unit).isascii() and chr(unit).isalnum() else "-" for unit in units
    )
    if len(key) <= KEY_LIMIT:
        return key
    value = 0
    for unit in units:  # JavaScript `(h << 5) - h + c | 0`
        value = ((value << 5) - value + unit) & 0xFFFFFFFF
    if value >= 0x80000000:
        value -= 0x100000000
    return f"{key[:KEY_LIMIT]}-{_base36(abs(value))}"


def _read(path: str) -> str | None:
    try:
        with open(path, encoding="utf-8") as handle:
            return handle.read().strip()
    except OSError, UnicodeDecodeError:
        return None


def _main_checkout(root: str) -> str:
    """The main checkout of a linked worktree at `root`; `root` itself otherwise."""
    if not os.path.isfile(os.path.join(root, ".git")):
        return root
    text = _read(os.path.join(root, ".git"))
    if not text or not text.startswith("gitdir:"):
        return root
    gitdir = os.path.normpath(os.path.join(root, text[len("gitdir:") :].strip()))
    common = _read(os.path.join(gitdir, "commondir"))
    if not common:
        return root  # a submodule, not a worktree
    common = os.path.normpath(os.path.join(gitdir, common))
    if os.path.dirname(gitdir) != os.path.join(common, "worktrees"):
        return root
    back = _read(os.path.join(gitdir, "gitdir"))
    if not back or os.path.realpath(os.path.join(gitdir, back)) != os.path.realpath(
        os.path.join(root, ".git")
    ):
        return root
    if os.path.basename(common) == ".git":
        return os.path.dirname(common)
    inner = os.path.join(common, ".git")  # a bare common directory
    if os.path.lexists(inner):
        raise ValueError(f"{inner} exists: the CLI may key the memory by another directory")
    return common


def canonical_root(project_dir: str) -> str:
    """The directory the CLI keys a project's memory by."""
    current = project_dir
    while True:
        marker = os.path.join(current, ".git")
        if os.path.islink(marker):
            raise ValueError(f"{marker} is a symbolic link: not followed")
        if os.path.isdir(marker) or os.path.isfile(marker):
            return _main_checkout(current)
        parent = os.path.dirname(current)
        if parent == current:
            return project_dir
        current = parent


def memory_dir(env: Mapping[str, str]) -> str:
    """The session's default memory directory. Raises ValueError when it cannot be placed."""
    moved = [name for name in OVERRIDES if env.get(name)]
    if moved:
        raise ValueError(f"{', '.join(moved)} set: the memory directory may lie elsewhere")
    project = env.get("CLAUDE_PROJECT_DIR", "")
    if not os.path.isabs(project):
        raise ValueError("CLAUDE_PROJECT_DIR is not set to an absolute path")
    project = unicodedata.normalize("NFC", os.path.normpath(project))
    config = env.get("CLAUDE_CONFIG_DIR") or os.path.join(os.path.expanduser("~"), ".claude")
    if not os.path.isabs(config):
        raise ValueError("cannot place the Claude config directory")
    path = os.path.join(config, "projects", project_key(canonical_root(project)), "memory")
    return unicodedata.normalize("NFC", path)


def refusal(call: Any, env: Mapping[str, str]) -> str | None:
    """Why this tool call must not run, or None when it writes inside the memory directory."""
    if not isinstance(call, dict) or call.get("hook_event_name") != "PreToolUse":
        return "not a PreToolUse call"
    tool = call.get("tool_name")
    if tool not in PATH_FIELDS:
        return f"unknown tool {tool!r}"
    tool_input = call.get("tool_input")
    target = tool_input.get(PATH_FIELDS[tool]) if isinstance(tool_input, dict) else None
    if not isinstance(target, str) or "\0" in target:
        return f"{tool} names no usable file path"
    # The CLI trims whitespace and expands `~`; only plain absolute paths map with certainty.
    if target != target.strip() or not os.path.isabs(target):
        return f"{tool} needs a plain absolute path, not {target!r}"
    allowed = memory_dir(env)
    root = os.path.realpath(allowed)
    resolved = os.path.realpath(target)  # `..`, and symlinks along the way, resolved
    where = f"{tool} may only change files in your memory directory {allowed}"
    if resolved == root or os.path.commonpath([root, resolved]) != root:
        return f"{where}; {target} is outside it"
    if os.path.isfile(resolved) and os.stat(resolved).st_nlink > 1:
        return f"{where}; {target} is a hard link to a file that may lie outside it"
    return None


def main() -> int:
    try:
        reason = refusal(json.loads(sys.stdin.read()), os.environ)
    except Exception as exc:  # fail closed  # noqa: BLE001
        reason = f"the memory guard failed: {type(exc).__name__}: {exc}"
    if reason is None:
        return 0
    print(f"Refused (orchestrator memory guard): {reason}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
