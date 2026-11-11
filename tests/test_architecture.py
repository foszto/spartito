"""Guard rails for the package layout: import layers and the facade.

The modules are parsed, not imported. An import counts wherever it appears: at module level
or inside a function ("lazy"); only `if TYPE_CHECKING:` blocks are skipped, as they never run.

Both exception lists below may only shrink: a test fails when an entry is no longer needed.
"""

from __future__ import annotations

import ast
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

PACKAGE = Path(__file__).resolve().parent.parent / "src" / "orchestrator"

# Module (first dotted component below `orchestrator`) -> layer. A module may import from its own
# layer or any layer of a lower rank. Domain and infra share a rank: infra builds domain types
# (store -> models, providers -> loadout) and some domain modules read through infra
# (settings -> store), so between those two only import cycles are checked.
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
RANK = {"interfaces": 3, "core": 2, "runner": 1, "domain": 0, "infra": 0}

# (importer, imported) pairs that go against the layering or form a cycle today.
IMPORT_EXCEPTIONS = {
    ("service", "runner"): "kilo; cycle with runner -> service (task 1: textfmt)",
    ("runner", "service"): "lazy, runner launches follow-up turns (task 4: run-end pipeline)",
    ("roles", "loadout"): "cycle with the lazy loadout -> roles (no plan task yet)",
    ("loadout", "roles"): "lazy, roles imports KEYS from loadout (no plan task yet)",
}

# Interface file -> allowed number of `.store` attribute accesses (task 5 brings all to zero).
STORE_ACCESS = {
    "cli.py": 9,
    "mcp_server.py": 23,
    "web/app.py": 7,
}


@dataclass(frozen=True)
class Import:
    importer: str
    imported: str
    line: int
    lazy: bool

    def __str__(self) -> str:
        where = "lazy" if self.lazy else "module level"
        return f"{self.importer} -> {self.imported} (line {self.line}, {where})"


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


def all_imports() -> list[Import]:
    files = sorted(PACKAGE.rglob("*.py"))
    return [imp for path in files if module_name(path) for imp in imports_of(path)]


def layer(module: str) -> str:
    return LAYERS[module.split(".")[0]]


def violations() -> dict[tuple[str, str], list[Import]]:
    """Imports of a higher layer, and imports that are part of a cycle, by (importer, imported)."""
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
        if upward or reaches(imp.imported, imp.importer):
            found[imp.importer, imp.imported].append(imp)
    return found


def test_every_module_has_a_layer():
    modules = {module_name(path) for path in PACKAGE.rglob("*.py")} - {""}
    unplaced = sorted(m for m in modules if m.split(".")[0] not in LAYERS)
    assert not unplaced, f"add these modules to LAYERS: {unplaced}"
    assert set(LAYERS.values()) == set(RANK)


def test_imports_follow_the_layers():
    found = violations()
    new = [str(imp) for pair in sorted(set(found) - set(IMPORT_EXCEPTIONS)) for imp in found[pair]]
    assert not new, "imports against the layering or in a cycle:\n" + "\n".join(new)


def test_import_exceptions_are_still_needed():
    stale = sorted(set(IMPORT_EXCEPTIONS) - set(violations()))
    assert not stale, f"remove these entries from IMPORT_EXCEPTIONS: {stale}"


def store_accesses(path: Path) -> list[int]:
    tree = ast.parse(path.read_text(), filename=str(path))
    return [n.lineno for n in ast.walk(tree) if isinstance(n, ast.Attribute) and n.attr == "store"]


def test_interfaces_use_the_store_no_more_than_allowed():
    for file, allowed in STORE_ACCESS.items():
        lines = store_accesses(PACKAGE / file)
        assert len(lines) <= allowed, f"{file}: {len(lines)} `.store` uses, allowed {allowed}"
        assert len(lines) >= allowed, (
            f"{file}: only {len(lines)} `.store` uses left; lower STORE_ACCESS to {len(lines)}"
        )
