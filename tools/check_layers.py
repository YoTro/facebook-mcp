#!/usr/bin/env python
"""Verify the architecture's dependency rule: imports may only point downward.

Layer ranks (lower = deeper):

    core=0  transport=1  session=2  routes=2  clients=3  services=4  server=5

A module may import from the same or a lower layer. Same-layer imports are
allowed (composition roots, sibling clients), but cycles are not.

Resolution notes
----------------
Both relative and absolute imports are normalised to a full dotted module path
before the layer is taken from it. Two earlier bugs made the check useless:

* relative imports were resolved to the *package* only (``from .http import x``
  inside ``transport`` became ``transport``), so ``from . import services`` and
  ``from .clients import x`` were never judged; and
* absolute ``facebook_mcp.*`` imports were dropped entirely, because the first
  component (``facebook_mcp``) is not a layer.

Usage::

    .venv/bin/python tools/check_layers.py                    # repo default
    .venv/bin/python tools/check_layers.py --root <package>   # any package
"""

from __future__ import annotations

import argparse
import ast
import sys
from collections import defaultdict
from pathlib import Path

DEFAULT_ROOT = Path(__file__).resolve().parents[1] / "src" / "facebook_mcp"

RANKS = {
    "core": 0,
    "transport": 1,
    "session": 2,
    "routes": 2,
    "clients": 3,
    "services": 4,
    "server": 5,
}

TOP_LEVEL_MODULES = {"server.py", "__main__.py"}


def layer_of(rel: Path) -> str | None:
    """Layer of a file, relative to the package root."""
    if len(rel.parts) == 1:
        return "server" if rel.name in TOP_LEVEL_MODULES else None
    return rel.parts[0] if rel.parts[0] in RANKS else None


def module_name(rel: Path, package: str) -> str:
    """Dotted module path of a file, e.g. ``facebook_mcp.clients.base``."""
    parts = list(rel.with_suffix("").parts)
    if parts and parts[-1] == "__init__":
        parts.pop()
    return ".".join([package, *parts]) if parts else package


def resolve(node: ast.AST, current: str) -> list[str]:
    """Absolute module paths touched by an import statement.

    ``current`` is the dotted name of the module doing the importing.
    """
    if isinstance(node, ast.Import):
        return [alias.name for alias in node.names]

    if not isinstance(node, ast.ImportFrom):
        return []

    if not node.level:
        base = node.module or ""
        targets = [base] if base else []
        # ``from pkg import mod`` may import a module, not only a name.
        targets.extend(f"{base}.{alias.name}" for alias in node.names if base)
        return targets

    # Relative import: start at the *package* of the current module (drop the
    # module name itself), then walk up ``level - 1`` packages.
    package_parts = current.split(".")[:-1]
    extra = node.level - 1
    if extra:
        package_parts = package_parts[:-extra] if extra < len(package_parts) else []
    base = ".".join(package_parts)
    if node.module:
        base = f"{base}.{node.module}"
        targets = [base]
        targets.extend(f"{base}.{alias.name}" for alias in node.names)
        return targets

    # ``from . import x`` — x may be a submodule or an attribute.
    return [f"{base}.{alias.name}" for alias in node.names]


def layer_for_module(module: str, package: str) -> str | None:
    if not module.startswith(package):
        return None
    rest = module[len(package) :].lstrip(".")
    if not rest:
        return None  # the package itself
    return rest.split(".")[0] if rest.split(".")[0] in RANKS else None


def check_path(root: Path = DEFAULT_ROOT, package: str | None = None) -> list[str]:
    """Return human-readable violations for the package at ``root``."""
    package = package or root.name
    violations: list[str] = []
    seen: set[tuple[str, int, str]] = set()

    for path in sorted(root.rglob("*.py")):
        rel = path.relative_to(root)
        layer = layer_of(rel)
        if layer is None:
            continue

        current = module_name(rel, package)
        tree = ast.parse(path.read_text(encoding="utf-8"))

        for node in ast.walk(tree):
            if not isinstance(node, (ast.Import, ast.ImportFrom)):
                continue
            for module in resolve(node, current):
                target = layer_for_module(module, package)
                if target is None:
                    continue
                if RANKS[target] > RANKS[layer]:
                    key = (str(rel), node.lineno, target)
                    if key in seen:
                        continue
                    seen.add(key)
                    violations.append(
                        f"{rel}:{node.lineno}  {layer} -> {target} (upward: {module})"
                    )

    return violations


def find_cycles(root: Path = DEFAULT_ROOT, package: str | None = None) -> list[str]:
    """Detect import cycles between modules of the package."""
    package = package or root.name
    graph: dict[str, set[str]] = defaultdict(set)

    for path in sorted(root.rglob("*.py")):
        rel = path.relative_to(root)
        current = module_name(rel, package)
        graph[current]  # ensure the node exists
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, (ast.Import, ast.ImportFrom)):
                continue
            for module in resolve(node, current):
                if module.startswith(package) and module != current:
                    graph[current].add(module)

    cycles: set[str] = set()

    def walk(node: str, stack: list[str], seen: set[str]) -> None:
        for child in sorted(graph.get(node, ())):
            if child in stack:
                start = stack.index(child)
                cycle = [*stack[start:], child]
                # Normalise rotation so one cycle is reported once.
                body = cycle[:-1]
                pivot = body.index(min(body)) if body else 0
                rotated = body[pivot:] + body[:pivot]
                cycles.add(" -> ".join([*rotated, rotated[0]]))
                continue
            if child in seen:
                continue
            walk(child, [*stack, child], seen | {child})

    for module in sorted(graph):
        walk(module, [module], {module})

    return sorted(cycles)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Verify layered imports")
    parser.add_argument("--root", default=str(DEFAULT_ROOT), help="package root to scan")
    parser.add_argument("--package", default=None, help="dotted package name (default: dir name)")
    args = parser.parse_args(argv)

    root = Path(args.root).resolve()
    if not root.is_dir():
        print(f"error: {root} is not a directory", file=sys.stderr)
        return 2

    violations = check_path(root, args.package)
    cycles = find_cycles(root, args.package)

    print(f"upward deps: {len(violations)}")
    for violation in violations:
        print("  ", violation)

    print(f"import cycles: {len(cycles)}")
    for cycle in cycles:
        print("  ", cycle)

    return 0 if not violations and not cycles else 1


if __name__ == "__main__":
    raise SystemExit(main())
