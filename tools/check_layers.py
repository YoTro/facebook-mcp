#!/usr/bin/env python
"""Verify the architecture's dependency rule: imports may only point downward.

Layer ranks (lower = deeper):

    core=0  transport=1  session=2  routes=2  clients=3  services=4  server=5

A module may import from the same or a lower layer. Same-layer imports are
allowed (composition roots, sibling clients), but cycles are not.

Usage::

    .venv/bin/python tools/check_layers.py      # exit 1 on violation
"""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "src" / "facebook_mcp"

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
    if len(rel.parts) == 1:
        return "server" if rel.name in TOP_LEVEL_MODULES else None
    return rel.parts[0] if rel.parts[0] in RANKS else None


def resolve_relative(rel: Path, node: ast.ImportFrom) -> list[str]:
    """Return the top-level package names a relative import touches."""
    if not node.level:
        return [node.module.split(".")[0]] if node.module else []
    base = list(rel.parts[:-1])
    for _ in range(node.level - 1):
        base = base[:-1]
    return [base[0]] if base else []


def check() -> list[str]:
    violations: list[str] = []

    for path in sorted(ROOT.rglob("*.py")):
        rel = path.relative_to(ROOT)
        layer = layer_of(rel)
        if layer is None:
            continue

        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                targets = resolve_relative(rel, node)
            elif isinstance(node, ast.Import):
                targets = [alias.name.split(".")[0] for alias in node.names]
            else:
                continue

            for target in targets:
                if target not in RANKS:
                    continue
                if RANKS[target] > RANKS[layer]:
                    violations.append(f"{rel}:{node.lineno}  {layer} -> {target} (upward)")

    return violations


def check_cycles() -> list[str]:
    """Detect same-layer import cycles inside a package."""
    problems: list[str] = []
    graph: dict[str, set[str]] = {}

    for path in sorted(ROOT.rglob("*.py")):
        rel = path.relative_to(ROOT)
        module = ".".join(rel.with_suffix("").parts)
        tree = ast.parse(path.read_text(encoding="utf-8"))
        edges: set[str] = set()

        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.level:
                base = list(rel.parts[:-1])
                for _ in range(node.level - 1):
                    base = base[:-1]
                if node.module:
                    base = base + node.module.split(".")
                if base:
                    edges.add(".".join(base))
        graph[module] = edges

    def walk(node: str, stack: list[str]) -> None:
        if node in stack:
            problems.append(" -> ".join([*stack[stack.index(node) :], node]))
            return
        for child in graph.get(node, ()):
            if child in graph:
                walk(child, [*stack, node])

    for module in graph:
        walk(module, [])

    return sorted(set(problems))


def main() -> int:
    violations = check()
    cycles = check_cycles()

    print(f"upward deps: {len(violations)}")
    for violation in violations:
        print("  ", violation)

    print(f"import cycles: {len(cycles)}")
    for cycle in cycles:
        print("  ", cycle)

    return 0 if not violations and not cycles else 1


if __name__ == "__main__":
    raise SystemExit(main())
