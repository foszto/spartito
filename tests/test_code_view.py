"""The code view of an agent's worktree (core/code_view.py, web/code_api.py, the page's part
in app.js): the changed files against the base branch with their patches, the file tree and
a file's content; read-only, confined to the worktree and to the files git lists, without
running anything the repository's configuration names."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from orchestrator import git
from orchestrator.core import code_view
from orchestrator.service import Orchestrator, OrchestratorError
from orchestrator.web.app import app

STATIC = Path(__file__).resolve().parent.parent / "src" / "orchestrator" / "web" / "static"
ID = {"name": "t", "email": "t@example.com"}


def run_git(where: Path, *args: str) -> str:
    command = ["git", "-c", f"user.name={ID['name']}", "-c", f"user.email={ID['email']}"]
    done = subprocess.run([*command, "-C", str(where), *args], capture_output=True, text=True)
    assert done.returncode == 0, done.stderr
    return done.stdout


def commit_all(where: Path, message: str = "work") -> None:
    run_git(where, "add", "-A")
    run_git(where, "commit", "-q", "-m", message)


@pytest.fixture
def agent_id(orch: Orchestrator, project: str, repo: Path) -> str:
    """A worker with its own worktree on its branch from `main` (no turn run)."""
    agent = orch.create_agent(project, name="fix-login")
    git.add_worktree(repo, agent.cwd, agent.branch, "main")
    return agent.id


@pytest.fixture
def tree(orch: Orchestrator, agent_id: str) -> Path:
    return Path(orch.agent(agent_id).cwd)


@pytest.fixture
def client() -> TestClient:
    return TestClient(app, base_url="http://localhost")


def by_path(answer: dict) -> dict[str, dict]:
    return {row["path"]: row for row in answer["files"]}


# -- the changed files and their patches --------------------------------------------------


def test_committed_uncommitted_and_untracked_changes_are_listed_with_counts(
    orch: Orchestrator, agent_id: str, tree: Path, repo: Path
) -> None:
    (repo / "kept.txt").write_text("one\ntwo\nthree\n")
    (repo / "gone.txt").write_text("a\nb\n")
    commit_all(repo, "base files")
    run_git(tree, "merge", "-q", "--ff-only", "main")
    (tree / "committed.py").write_text("print(1)\nprint(2)\n")
    commit_all(tree)
    (tree / "kept.txt").write_text("one\nTWO\nthree\nfour\n")  # unstaged
    (tree / "gone.txt").unlink()
    (tree / "staged.txt").write_text("staged\n")
    run_git(tree, "add", "staged.txt")
    (tree / "new.md").write_text("first\nsecond\nno newline")  # untracked

    answer = orch.code_changes(agent_id)
    rows = by_path(answer)

    assert answer["base"] == "main" and answer["truncated"] is False and answer["total"] == 5
    assert list(rows) == ["committed.py", "gone.txt", "kept.txt", "new.md", "staged.txt"]
    assert rows["committed.py"] == {
        "path": "committed.py",
        "status": "A",
        "added": 2,
        "removed": 0,
        "binary": False,
        "too_large": False,
        "untracked": False,
    }
    assert (rows["kept.txt"]["status"], rows["kept.txt"]["added"], rows["kept.txt"]["removed"]) == (
        "M",
        2,
        1,
    )
    assert (rows["gone.txt"]["status"], rows["gone.txt"]["removed"]) == ("D", 2)
    assert (rows["staged.txt"]["status"], rows["staged.txt"]["added"]) == ("A", 1)
    assert rows["new.md"]["untracked"] is True
    assert (rows["new.md"]["status"], rows["new.md"]["added"], rows["new.md"]["removed"]) == (
        "A",
        3,
        0,
    )


def test_changes_are_counted_from_where_the_branch_left_the_base(
    orch: Orchestrator, agent_id: str, tree: Path, repo: Path
) -> None:
    """A commit on the base after the branch left it is not the agent's change."""
    (repo / "later.txt").write_text("on main\n")
    commit_all(repo, "later on main")
    (tree / "mine.txt").write_text("mine\n")
    commit_all(tree)

    assert list(by_path(orch.code_changes(agent_id))) == ["mine.txt"]


def test_a_patch_is_given_per_file_for_tracked_and_untracked_files(
    orch: Orchestrator, agent_id: str, tree: Path
) -> None:
    (tree / "app.py").write_text("x = 1\n")
    commit_all(tree)
    (tree / "app.py").write_text("x = 2\n")
    (tree / "notes.txt").write_text("hello\nworld")

    tracked = orch.code_patch(agent_id, "app.py")
    untracked = orch.code_patch(agent_id, "notes.txt")

    assert tracked["truncated"] is False and tracked["binary"] is False
    assert "+x = 2\n" in tracked["patch"] and "diff --git a/app.py b/app.py" in tracked["patch"]
    assert "-x" not in tracked["patch"]  # against the base, the file is new
    assert untracked["patch"].splitlines() == [
        "diff --git a/notes.txt b/notes.txt",
        "new file mode 100644",
        "--- /dev/null",
        "+++ b/notes.txt",
        "@@ -0,0 +1,2 @@",
        "+hello",
        "+world",
        "\\ No newline at end of file",
    ]
    with pytest.raises(OrchestratorError, match="no changes"):
        orch.code_patch(agent_id, "README-that-is-not-there")


def test_binary_and_oversized_files_are_named_not_rendered(
    orch: Orchestrator, agent_id: str, tree: Path
) -> None:
    (tree / "logo.png").write_bytes(b"\x89PNG\r\n\x00\x00binary")
    commit_all(tree)
    (tree / "blob.bin").write_bytes(b"text first\x00then binary")  # untracked
    big = "line\n" * (code_view.MAX_FILE_BYTES // 5 + 10)
    (tree / "big.log").write_text(big)  # untracked, above the cap

    rows = by_path(orch.code_changes(agent_id))
    assert rows["logo.png"]["binary"] is True and rows["logo.png"]["added"] is None
    assert rows["blob.bin"]["binary"] is True and rows["blob.bin"]["added"] is None
    assert rows["big.log"]["too_large"] is True and rows["big.log"]["added"] is None

    for path in ("logo.png", "blob.bin"):
        patch = orch.code_patch(agent_id, path)
        assert patch["binary"] is True and patch["patch"] is None
        shown = orch.code_file(agent_id, path)
        assert shown["binary"] is True and shown["content"] is None
    assert orch.code_patch(agent_id, "big.log")["too_large"] is True
    assert orch.code_patch(agent_id, "big.log")["patch"] is None

    shown = orch.code_file(agent_id, "big.log")
    assert shown["truncated"] is True and shown["size"] == len(big)
    assert len(shown["content"].encode()) == code_view.MAX_FILE_BYTES


def test_long_patches_and_long_lists_are_cut_and_say_so(
    orch: Orchestrator, agent_id: str, tree: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tree / "long.txt").write_text("".join(f"{n}\n" for n in range(3000)))
    for n in range(4):
        (tree / f"f{n}.txt").write_text("x\n")
    commit_all(tree)

    patch = orch.code_patch(agent_id, "long.txt")
    assert patch["truncated"] is True and patch["max_lines"] == code_view.MAX_PATCH_LINES
    assert patch["lines"] > code_view.MAX_PATCH_LINES
    assert len(patch["patch"].splitlines()) == code_view.MAX_PATCH_LINES

    monkeypatch.setattr(code_view, "MAX_CHANGED", 2)
    monkeypatch.setattr(code_view, "MAX_TREE", 3)
    changes = orch.code_changes(agent_id)
    files = orch.code_tree(agent_id)
    assert (changes["truncated"], changes["total"], len(changes["files"])) == (True, 5, 2)
    assert (files["truncated"], files["total"], len(files["files"])) == (True, 5, 3)
    assert changes["max_files"] == 2 and files["max_files"] == 3


# -- the file tree and a file's content ---------------------------------------------------


def test_the_tree_lists_tracked_and_untracked_files_but_never_ignored_ones(
    orch: Orchestrator, agent_id: str, tree: Path
) -> None:
    (tree / ".gitignore").write_text(".env\nbuild/\n")
    (tree / "src").mkdir()
    (tree / "src" / "main.py").write_text("print('hi')\n")
    commit_all(tree)
    (tree / "src" / "draft.py").write_text("pass\n")
    (tree / ".env").write_text("TOKEN=not-for-the-page\n")
    (tree / "build").mkdir()
    (tree / "build" / "out.js").write_text("ignored\n")

    files = orch.code_tree(agent_id)["files"]
    assert files == [".gitignore", "src/draft.py", "src/main.py"]
    assert orch.code_file(agent_id, "src/main.py")["content"] == "print('hi')\n"
    assert ".env" not in by_path(orch.code_changes(agent_id))
    for path in (".env", "build/out.js"):
        with pytest.raises(OrchestratorError, match="not a file git lists"):
            orch.code_file(agent_id, path)
        with pytest.raises(OrchestratorError, match="no changes"):
            orch.code_patch(agent_id, path)


@pytest.mark.parametrize(
    "path",
    [
        "../repo/README",
        "src/../../x",
        "/etc/passwd",
        "a\0b",
        "a//b",
        "./a",
        "a/",
        "",
        ".git/config",
        ".GIT/config",
        "sub/.git/HEAD",
        None,
    ],
)
def test_paths_outside_the_worktree_or_into_git_are_refused(
    orch: Orchestrator, agent_id: str, tree: Path, path: str | None
) -> None:
    (tree / "a").mkdir()
    (tree / "a" / "b").write_text("x\n")
    for read in (orch.code_file, orch.code_patch):
        with pytest.raises(OrchestratorError):
            read(agent_id, path)


def test_git_config_is_never_served_even_when_named_without_dot_git(
    orch: Orchestrator, agent_id: str, tree: Path
) -> None:
    """The worktree's `.git` is a file pointing at the repository; neither it nor what git
    keeps is ever listed."""
    assert ".git" not in orch.code_tree(agent_id)["files"]
    with pytest.raises(OrchestratorError):
        orch.code_file(agent_id, ".git")


def test_a_link_is_shown_as_its_target_and_never_followed(
    orch: Orchestrator, agent_id: str, tree: Path, tmp_path: Path
) -> None:
    secret = tmp_path / "outside"
    secret.mkdir()
    (secret / "secret.txt").write_text("SECRET-CONTENT\n")
    (tree / "docs").mkdir()
    (tree / "docs" / "guide.md").write_text("# Guide\n")
    os.symlink(secret / "secret.txt", tree / "escape")
    os.symlink("docs/guide.md", tree / "inside")
    os.symlink(secret, tree / "outdir")

    outside = orch.code_file(agent_id, "escape")
    inside = orch.code_file(agent_id, "inside")

    assert outside["kind"] == "symlink" and outside["content"] is None
    assert outside["target"] == str(secret / "secret.txt")
    assert inside == {**inside, "kind": "symlink", "target": "docs/guide.md", "content": None}
    for answer in (outside, inside, orch.code_patch(agent_id, "escape")):
        assert "SECRET-CONTENT" not in json.dumps(answer)
    with pytest.raises(OrchestratorError):
        orch.code_file(agent_id, "outdir/secret.txt")


def test_reading_never_goes_through_a_linked_directory(tree: Path, tmp_path: Path) -> None:
    """The second line of defence, below git's listing: a directory that is a link is not
    entered, whether its target is outside the worktree or inside it."""
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    (outside / "x.txt").write_text("outside\n")
    (tree / "real").mkdir()
    (tree / "real" / "y.txt").write_text("inside\n")
    os.symlink(outside, tree / "out")
    os.symlink(tree / "real", tree / "in")
    root = tree.resolve()

    for path in ("out/x.txt", "in/y.txt"):
        with pytest.raises((OSError, OrchestratorError)):
            code_view.read_entry(root, path, 100)
    assert code_view.read_entry(root, "real/y.txt", 100).data == b"inside\n"


def test_unusual_file_names_come_back_intact(orch: Orchestrator, agent_id: str, tree: Path) -> None:
    # The last one is the file `script>.html` in the folder `<script>alert(1)<`.
    names = ["with space.txt", "quote\"d'.txt", "ékezet-ü.md", "<script>alert(1)</script>.html"]
    (tree / "<script>alert(1)<").mkdir()
    for name in names:
        (tree / name).write_text(f"{name}\n")
    run_git(tree, "add", names[0], names[1])
    run_git(tree, "commit", "-q", "-m", "two of them")

    assert set(names) <= set(orch.code_tree(agent_id)["files"])
    assert set(names) <= set(by_path(orch.code_changes(agent_id)))
    for name in names:
        assert orch.code_file(agent_id, name)["content"] == f"{name}\n"
        assert f"+{name}\n" in orch.code_patch(agent_id, name)["patch"]


# -- which agents, which worktrees ---------------------------------------------------------


def test_only_an_agent_with_its_own_existing_worktree_is_shown(
    orch: Orchestrator, project: str, agent_id: str, tree: Path
) -> None:
    plain = orch.create_agent(project, name="no-tree", worktree=False)
    unborn = orch.create_agent(project, name="not-yet")
    calls = (orch.code_changes, orch.code_tree)

    for agent, message in (
        ("nobody-1234", "unknown agent"),
        (plain.id, "no worktree of its own"),
        (unborn.id, "does not exist"),
    ):
        for read in calls:
            with pytest.raises(OrchestratorError, match=message):
                read(agent)

    shutil.rmtree(tree)
    with pytest.raises(OrchestratorError, match="does not exist"):
        orch.code_tree(agent_id)
    tree.mkdir()  # a plain directory where the worktree was
    with pytest.raises(OrchestratorError, match="not a git repository"):
        orch.code_tree(agent_id)
    run_git(tree, "init", "-q")  # another repository there: not a worktree of the project
    with pytest.raises(OrchestratorError, match="is not a worktree of"):
        orch.code_tree(agent_id)


def test_a_project_no_longer_registered_is_refused(
    orch: Orchestrator, agent_id: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(orch.store, "get_project", lambda name: None)
    with pytest.raises(OrchestratorError, match="unknown project"):
        orch.code_tree(agent_id)


# -- nothing runs, nothing is written -------------------------------------------------------


def test_the_repositorys_own_configuration_runs_nothing(
    orch: Orchestrator, agent_id: str, tree: Path, tmp_path: Path
) -> None:
    """An external diff, a textconv driver, a clean filter (command and process) and an
    fsmonitor hook that each leave a marker: plain git runs them, the code view does not."""
    marks = tmp_path / "marks"
    marks.mkdir()
    (tree / "app.txt").write_text("one\n")
    commit_all(tree)
    hook = lambda name: f"touch '{marks / name}'; cat"  # noqa: E731
    run_git(tree, "config", "diff.external", f"touch '{marks / 'external'}' #")
    run_git(tree, "config", "diff.evil.textconv", hook("textconv"))
    run_git(tree, "config", "filter.evil.clean", hook("clean"))
    run_git(tree, "config", "filter.other.process", f"touch '{marks / 'process'}'")
    run_git(tree, "config", "core.fsmonitor", f"touch '{marks / 'fsmonitor'}'; true #")
    (tree / ".gitattributes").write_text("*.txt diff=evil filter=evil\n*.md filter=other\n")
    (tree / "app.txt").write_text("one\ntwo\n")
    (tree / "readme.md").write_text("x\n")
    run_git(tree, "add", "readme.md")
    (tree / "readme.md").write_text("y\n")
    os.utime(tree / "app.txt", (1, 1))
    os.utime(tree / "readme.md", (1, 1))

    # The control: plain git runs them.
    subprocess.run(["git", "-C", str(tree), "status"], capture_output=True)
    subprocess.run(["git", "-C", str(tree), "diff", "HEAD"], capture_output=True)
    textconv = ("diff", "--no-ext-diff", "--textconv", "HEAD")
    subprocess.run(["git", "-C", str(tree), *textconv], capture_output=True)
    assert {p.name for p in marks.iterdir()} == {
        "external",
        "fsmonitor",
        "textconv",
        "clean",
        "process",
    }
    shutil.rmtree(marks)
    marks.mkdir()

    rows = by_path(orch.code_changes(agent_id))
    assert rows["app.txt"]["added"] == 2
    assert "+two" in orch.code_patch(agent_id, "app.txt")["patch"]
    assert "+y" in orch.code_patch(agent_id, "readme.md")["patch"]
    assert orch.code_tree(agent_id)["files"]
    assert orch.code_file(agent_id, "app.txt")["content"] == "one\ntwo\n"

    assert list(marks.iterdir()) == []


def test_reading_writes_nothing_to_the_worktree_or_its_index(
    orch: Orchestrator, agent_id: str, tree: Path
) -> None:
    (tree / "a.txt").write_text("a\n")
    commit_all(tree)
    (tree / "a.txt").write_text("b\n")
    (tree / "u.txt").write_text("u\n")
    os.utime(tree / "a.txt", (1, 1))
    index = Path(
        run_git(tree, "rev-parse", "--path-format=absolute", "--git-path", "index").strip()
    )

    def snapshot() -> dict[str, tuple[int, int]]:
        found = {str(p): (p.stat().st_mtime_ns, p.stat().st_size) for p in tree.rglob("*")}
        return {**found, "index": (index.stat().st_mtime_ns, index.stat().st_size)}

    before = snapshot()
    orch.code_changes(agent_id)
    orch.code_patch(agent_id, "a.txt")
    orch.code_patch(agent_id, "u.txt")
    orch.code_tree(agent_id)
    orch.code_file(agent_id, "a.txt")
    assert snapshot() == before


# -- the routes --------------------------------------------------------------------------------


def test_the_routes_answer_get_with_the_core_answers(
    client: TestClient, orch: Orchestrator, agent_id: str, tree: Path
) -> None:
    name = "<script>alert(1)</script> ü.txt"
    (tree / "<script>alert(1)<").mkdir()
    (tree / name).write_text("<b>bold</b>\n")
    base = f"/api/agents/{agent_id}/code"

    changes = client.get(f"{base}/changes")
    patch = client.get(f"{base}/patch", params={"path": name})
    files = client.get(f"{base}/tree")
    shown = client.get(f"{base}/file", params={"path": name})

    for response in (changes, patch, files, shown):
        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-store"
    assert [row["path"] for row in changes.json()["files"]] == [name]
    assert files.json()["files"] == [name]
    assert shown.json()["content"] == "<b>bold</b>\n"
    assert "+<b>bold</b>" in patch.json()["patch"]


@pytest.mark.parametrize(
    ("query", "status"),
    [
        ("/code/file?path=../x", 400),
        ("/code/file?path=%2Fetc%2Fpasswd", 400),
        ("/code/file?path=a%00b", 400),
        ("/code/file", 400),
        ("/code/patch?path=.git%2Fconfig", 400),
        ("/code/file?path=missing.txt", 400),
    ],
)
def test_refusals_are_short_client_errors(
    client: TestClient, agent_id: str, query: str, status: int
) -> None:
    response = client.get(f"/api/agents/{agent_id}{query}")
    assert response.status_code == status
    assert set(response.json()) == {"error"} and "Traceback" not in response.text


def test_unknown_agents_and_missing_worktrees_are_client_errors(
    client: TestClient, agent_id: str, tree: Path
) -> None:
    assert client.get("/api/agents/nobody-1234/code/tree").status_code == 400
    shutil.rmtree(tree)
    for what in ("changes", "tree", "file?path=a", "patch?path=a"):
        response = client.get(f"/api/agents/{agent_id}/code/{what}")
        assert response.status_code == 400 and "error" in response.json()


def test_a_file_vanishing_mid_request_is_a_client_error(
    client: TestClient, agent_id: str, tree: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tree / "brief.txt").write_text("x\n")

    def vanish(root: Path, path: str, limit: int) -> code_view.Entry:
        (root / path).unlink()
        raise FileNotFoundError(2, "No such file or directory", path)

    monkeypatch.setattr(code_view, "read_entry", vanish)
    response = client.get(f"/api/agents/{agent_id}/code/file", params={"path": "brief.txt"})
    assert response.status_code == 400 and "does not exist" in response.json()["error"]


@pytest.mark.parametrize("what", ["changes", "patch", "tree", "file"])
def test_the_routes_take_only_get(client: TestClient, agent_id: str, what: str) -> None:
    url = f"/api/agents/{agent_id}/code/{what}"
    headers = {"origin": "http://localhost"}
    for method in ("POST", "PUT", "DELETE", "PATCH"):
        response = client.request(method, url, json={"path": "a"}, headers=headers)
        assert response.status_code == 405, method


@pytest.mark.parametrize("what", ["changes", "tree", "file?path=a.txt"])
def test_other_sites_are_refused(client: TestClient, agent_id: str, tree: Path, what: str) -> None:
    """The dashboard's Host check (DNS rebinding), and for these routes also an Origin or a
    Sec-Fetch-Site of another site, even on GET."""
    (tree / "a.txt").write_text("a\n")
    url = f"/api/agents/{agent_id}/code/{what}"

    assert client.get(url, headers={"host": "evil.example"}).status_code == 403
    assert client.get(url, headers={"origin": "http://evil.example"}).status_code == 403
    assert client.get(url, headers={"origin": "http://localhost:3000"}).status_code == 403
    for site in ("cross-site", "same-site"):
        assert client.get(url, headers={"sec-fetch-site": site}).status_code == 403
    assert client.get(url, headers={"origin": "http://localhost"}).status_code == 200
    assert client.get(url, headers={"sec-fetch-site": "same-origin"}).status_code == 200
    assert client.get(url, headers={"sec-fetch-site": "none"}).status_code == 200


def test_the_existing_requests_compute_nothing_of_the_code_view(
    client: TestClient, agent_id: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refuse(*args: object) -> None:
        raise AssertionError("the code view was computed")

    for name in ("code_changes", "code_patch", "code_tree", "code_file"):
        monkeypatch.setattr(Orchestrator, name, refuse)
    assert client.get("/api/state").status_code == 200
    assert client.get(f"/api/agents/{agent_id}/events").status_code == 200


# -- the page -------------------------------------------------------------------------------------


def page_functions(*names: str) -> str:
    """The named top-level functions and one-line constants of app.js, as source."""
    script = (STATIC / "app.js").read_text()
    parts = []
    for name in names:
        function = re.search(rf"^(?:async )?function {name}\(.*?^\}}\n", script, re.M | re.S)
        constant = re.search(rf"^const {name} = .*?;\n(?=\S|\n)", script, re.M | re.S)
        found = function or constant
        assert found, name
        parts.append(found.group(0))
    return "\n".join(parts)


# A minimal document: elements keep their text and children; markup is refused.
FAKE_DOM = r"""
class Node {
  constructor(tag) { this.tag = tag; this.children = []; this.className = ''; this.text = ''; this.dataset = {}; }
  set textContent(value) { this.text = String(value); this.children = []; }
  get textContent() { return this.text + this.children.map(c => c.textContent).join(''); }
  set innerHTML(value) { throw new Error('markup set: ' + value); }
  append(...nodes) { for (const n of nodes) this.children.push(typeof n === 'string' ? Object.assign(new Node('#text'), { text: n }) : n); }
  replaceChildren(...nodes) { this.children = []; this.append(...nodes); }
}
const document = { createElement: tag => new Node(tag) };
const dump = n => ({ tag: n.tag, cls: n.className, text: n.text, children: n.children.map(dump) });
"""


def run_node(code: str) -> object:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    done = subprocess.run([node, "-e", code], capture_output=True, text=True, check=True)
    return json.loads(done.stdout)


def test_the_page_builds_its_urls_and_notes() -> None:
    source = page_functions("codeUrl", "sizeText", "codeNote", "countText")
    cases = {
        "url": "codeUrl('fix-login-3f2a', 'file', 'a b/<x>&?.txt')",
        "plain": "codeUrl('fix-login-3f2a', 'tree')",
        "list": "codeNote({max_files: 1000, files: [1, 2], total: 1500, truncated: true})",
        "whole": "codeNote({max_files: 1000, files: [1], total: 1, truncated: false})",
        "patch": "codeNote({max_lines: 2000, lines: 4000, truncated: true, patch: ''})",
        "file": "codeNote({max_bytes: 524288, size: 2097152, truncated: true, content: ''})",
        "binary": "codeNote({binary: true})",
        "large": "codeNote({too_large: true})",
        "link": "codeNote({kind: 'symlink', target: '../x'})",
        "counts": "[countText({added: 3, removed: 1}), countText({binary: true}),"
        " countText({too_large: true}), countText({added: null})]",
    }
    code = source + "\nprocess.stdout.write(JSON.stringify({"
    code += ", ".join(f"{key}: {expr}" for key, expr in cases.items()) + "}));"

    assert run_node(code) == {
        "url": "/api/agents/fix-login-3f2a/code/file?path=a%20b%2F%3Cx%3E%26%3F.txt",
        "plain": "/api/agents/fix-login-3f2a/code/tree",
        "list": "Showing 2 of 1500 files (the first 1000).",
        "whole": "",
        "patch": "Patch cut: showing at most 2000 of 4000 lines.",
        "file": "File cut: showing the first 512 KB of 2.0 MB.",
        "binary": "Binary file: not shown.",
        "large": "Too large to show.",
        "link": "Symbolic link to ../x (not followed).",
        "counts": ["+3 −1", "binary", "too large", ""],
    }


def test_the_page_nests_paths_into_folders() -> None:
    source = page_functions("fileTree")
    code = source + (
        "\nconst plain = n => ({dirs: Object.fromEntries([...n.dirs].map(([k, v]) => [k, plain(v)])),"
        " files: n.files});"
        "\nprocess.stdout.write(JSON.stringify(plain(fileTree(['a.txt', 'src/x.py', 'src/lib/y.py']))));"
    )
    assert run_node(code) == {
        "dirs": {
            "src": {
                "dirs": {"lib": {"dirs": {}, "files": [{"name": "y.py", "path": "src/lib/y.py"}]}},
                "files": [{"name": "x.py", "path": "src/x.py"}],
            }
        },
        "files": [{"name": "a.txt", "path": "a.txt"}],
    }


def test_the_page_renders_patches_as_text_never_as_markup() -> None:
    source = page_functions("el", "patchClass", "renderPatch")
    patch = (
        "diff --git a/<script>.html b/<script>.html\n--- /dev/null\n+++ b/<script>.html\n"
        "@@ -0,0 +1,2 @@\n+<script>alert(1)</script>\n-<img src=x onerror=alert(1)>\n context\n"
    )
    code = (
        FAKE_DOM
        + source
        + f"\nprocess.stdout.write(JSON.stringify(dump(renderPatch({json.dumps(patch)}))));"
    )

    pre = run_node(code)

    assert (pre["tag"], pre["cls"]) == ("pre", "code-text patch")
    lines = [(child["cls"], child["text"]) for child in pre["children"]]
    assert lines == [
        ("meta", "diff --git a/<script>.html b/<script>.html\n"),
        ("meta", "--- /dev/null\n"),
        ("meta", "+++ b/<script>.html\n"),
        ("hunk", "@@ -0,0 +1,2 @@\n"),
        ("add", "+<script>alert(1)</script>\n"),
        ("del", "-<img src=x onerror=alert(1)>\n"),
        ("", " context\n"),
    ]


def test_the_page_shows_names_and_contents_only_as_text() -> None:
    """Every code-view function that puts a name, a line or a message into the page does it
    through el() (textContent); none of them sets markup."""
    script = (STATIC / "app.js").read_text()
    start = script.index("// -- the code view of a worktree")
    section = script[start : script.index("// -- model and effort", start)]
    assert "innerHTML" not in section and "insertAdjacentHTML" not in section
    assert "outerHTML" not in section and "document.write" not in section


def test_the_page_declares_each_top_level_name_once() -> None:
    """A classic script's later function of the same name silently replaces the earlier one
    (the folder picker's renderFolder would replace the code view's)."""
    script = (STATIC / "app.js").read_text()
    pattern = r"^(?:async )?function ([\w$]+)|^(?:const|let|var) ([\w$]+)"
    names = [a or b for a, b in re.findall(pattern, script, re.M)]
    assert len(names) > 100
    assert sorted({n for n in names if names.count(n) > 1}) == []
    assert "renderCodeFolder" in names
