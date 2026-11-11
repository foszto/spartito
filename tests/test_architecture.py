"""Guard rails for the package layout: import layers and the facade.

The modules are parsed, not imported. An import counts wherever it appears: at module level
or inside a function ("lazy"); only `if TYPE_CHECKING:` blocks are skipped, as they never run.

Both exception lists below may only shrink: a test fails when a count is higher than needed.
"""

from __future__ import annotations

import ast
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

PACKAGE = Path(__file__).resolve().parent.parent / "src" / "orchestrator"

# Module (first dotted component below `orchestrator`) -> layer. A module may import from its own
# layer or any layer of a lower rank; interfaces must not import the store module at all.
LAYERS = {
    "cli": "interfaces",
    "mcp_server": "interfaces",
    "web": "interfaces",
    "service": "core",
    "core": "core",
    "runner": "runner",
    "models": "domain",
    "settings": "domain",
    "roles": "domain",
    "loadout": "domain",
    "replies": "domain",
    "timeline": "domain",
    "textfmt": "domain",
    "errors": "domain",
    "notify": "domain",  # notification rules; its run SQL moves to the store in task 6
    "board": "domain",  # self-contained feature owning its table
    "decisions": "domain",  # self-contained feature owning its table
    "store": "infra",
    "git": "infra",
    "providers": "infra",
    "keep_awake": "infra",
    "desktop": "infra",
    "config": "infra",  # paths and environment
    "memory_guard": "infra",  # hook script used by the Claude provider
}
RANK = {"interfaces": 4, "core": 3, "runner": 2, "domain": 1, "infra": 0}

NO_TASK = "no plan task; revisit with tasks 6/12"
# (importer, imported) -> (number of imports, where they are, why) for the imports that go
# against the layering, form a cycle or reach the store from an interface today. More imports
# than pinned, or a lazy one turned module level, fails; so does a count above the real one.
IMPORT_EXCEPTIONS = {
    ("runner", "service"): (2, "lazy", "follow-up turns (task 4: run-end pipeline)"),
    ("roles", "loadout"): (1, "module level", f"cycle with loadout -> roles; {NO_TASK}"),
    ("loadout", "roles"): (1, "lazy", f"cycle with roles -> loadout; {NO_TASK}"),
    ("mcp_server", "store"): (1, "module level", "ANYONE (task 5: close the facade)"),
    ("store", "models"): (1, "module level", f"rows to model objects; {NO_TASK}"),
    ("providers.base", "models"): (1, "module level", f"Agent, Event; {NO_TASK}"),
    ("providers.base", "loadout"): (1, "module level", f"Loadout; {NO_TASK}"),
    ("providers.claude", "models"): (1, "module level", f"Agent, Event, Usage; {NO_TASK}"),
    ("providers.claude", "loadout"): (1, "module level", f"Loadout; {NO_TASK}"),
    ("providers.codex", "models"): (1, "module level", f"Agent, Event, Usage; {NO_TASK}"),
    ("keep_awake", "settings"): (1, "module level", f"keep_awake setting; {NO_TASK}"),
    ("desktop", "settings"): (1, "module level", f"desktop_notifications setting; {NO_TASK}"),
    ("desktop", "notify"): (1, "module level", f"notify.settled; {NO_TASK}"),
    ("desktop", "models"): (1, "module level", f"RunStatus; {NO_TASK}"),
}

# Interface module -> allowed number of `.store` attribute accesses; unlisted interface modules
# allow none (task 5 brings all to zero).
STORE_ACCESS = {
    "cli": 9,
    "mcp_server": 23,
    "web.app": 7,
}


@dataclass(frozen=True)
class Import:
    importer: str
    imported: str
    line: int
    lazy: bool

    @property
    def where(self) -> str:
        return "lazy" if self.lazy else "module level"

    def __str__(self) -> str:
        return f"{self.importer} -> {self.imported} (line {self.line}, {self.where})"


def module_name(path: Path) -> str:
    parts = list(path.relative_to(PACKAGE).with_suffix("").parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def is_module(dotted: str) -> bool:
    base = PACKAGE.joinpath(*dotted.split(".")) if dotted else PACKAGE
    return base.with_suffix(".py").is_file() or (base / "__init__.py").is_file()


def resolve(node: ast.Import | ast.ImportFrom, name: str, is_package: bool) -> list[str]:
    """The package modules (dotted, below `orchestrator`) an import statement loads."""
    if isinstance(node, ast.Import):
        targets = [alias.name for alias in node.names]
        return [t.removeprefix("orchestrator.") for t in targets if t.startswith("orchestrator.")]
    if node.level:
        package = name.split(".") if is_package else name.split(".")[:-1]
        package = package[: len(package) - (node.level - 1)]
        base = ".".join(package + ([node.module] if node.module else []))
    elif node.module == "orchestrator" or (node.module or "").startswith("orchestrator."):
        base = node.module.removeprefix("orchestrator").lstrip(".")
    else:
        return []
    found = []
    for alias in node.names:
        sub = f"{base}.{alias.name}" if base else alias.name
        found.append(sub if is_module(sub) else base)
    return [m for m in found if m]


def is_type_checking(test: ast.expr) -> bool:
    return (isinstance(test, ast.Name) and test.id == "TYPE_CHECKING") or (
        isinstance(test, ast.Attribute) and test.attr == "TYPE_CHECKING"
    )


def imports_of(path: Path) -> list[Import]:
    name = module_name(path)
    is_package = path.name == "__init__.py"
    found: list[Import] = []

    def visit(node: ast.AST, lazy: bool) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.If) and is_type_checking(child.test):
                for stmt in child.orelse:
                    visit(stmt, lazy)
            elif isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda):
                visit(child, True)
            elif isinstance(child, ast.Import | ast.ImportFrom):
                for target in resolve(child, name, is_package):
                    if target != name:
                        found.append(Import(name, target, child.lineno, lazy))
            else:
                visit(child, lazy)

    visit(ast.parse(path.read_text(), filename=str(path)), False)
    return list(dict.fromkeys(found))  # one entry per statement, not per imported name


def modules() -> dict[str, Path]:
    return {module_name(path): path for path in sorted(PACKAGE.rglob("*.py")) if module_name(path)}


def all_imports() -> list[Import]:
    return [imp for path in modules().values() for imp in imports_of(path)]


def layer(module: str) -> str:
    return LAYERS[module.split(".")[0]]


def violations() -> dict[tuple[str, str], list[Import]]:
    """Imports of a higher layer, imports in a cycle and imports of the store from an interface,
    by (importer, imported)."""
    imports = all_imports()
    graph: dict[str, set[str]] = defaultdict(set)
    for imp in imports:
        graph[imp.importer].add(imp.imported)

    def reaches(start: str, goal: str) -> bool:
        seen, todo = set(), [start]
        while todo:
            node = todo.pop()
            if node == goal:
                return True
            if node not in seen:
                seen.add(node)
                todo.extend(graph[node])
        return False

    found: dict[tuple[str, str], list[Import]] = defaultdict(list)
    for imp in imports:
        upward = RANK[layer(imp.imported)] > RANK[layer(imp.importer)]
        store = layer(imp.importer) == "interfaces" and imp.imported.split(".")[0] == "store"
        if upward or store or reaches(imp.imported, imp.importer):
            found[imp.importer, imp.imported].append(imp)
    return found


def test_every_module_has_a_layer():
    unplaced = sorted(m for m in modules() if m.split(".")[0] not in LAYERS)
    assert not unplaced, f"add these modules to LAYERS: {unplaced}"
    assert set(LAYERS.values()) == set(RANK)


def test_imports_follow_the_layers():
    problems = []
    for pair, imps in sorted(violations().items()):
        allowed, where, _ = IMPORT_EXCEPTIONS.get(pair, (0, "", ""))
        if len(imps) > allowed or any(imp.where != where for imp in imps):
            problems += [str(imp) for imp in imps]
    assert not problems, "imports against the layering, in a cycle or of the store:\n" + "\n".join(
        problems
    )


def test_import_exceptions_are_still_needed():
    found = violations()
    stale = {
        pair: len(found.get(pair, []))
        for pair, (allowed, _, _) in IMPORT_EXCEPTIONS.items()
        if len(found.get(pair, [])) < allowed
    }
    assert not stale, f"lower these IMPORT_EXCEPTIONS counts (remove at 0): {stale}"


def store_accesses(path: Path) -> list[int]:
    tree = ast.parse(path.read_text(), filename=str(path))
    return [n.lineno for n in ast.walk(tree) if isinstance(n, ast.Attribute) and n.attr == "store"]


def test_interfaces_use_the_store_no_more_than_allowed():
    interfaces = {name: path for name, path in modules().items() if layer(name) == "interfaces"}
    for name in sorted(set(interfaces) | set(STORE_ACCESS)):
        used = len(store_accesses(interfaces[name])) if name in interfaces else 0
        allowed = STORE_ACCESS.get(name, 0)
        assert used <= allowed, f"{name}: {used} `.store` uses, allowed {allowed}"
        assert used >= allowed, f"{name}: only {used} `.store` uses left; lower STORE_ACCESS"
