"""The layering checker must actually catch upward imports.

A checker that reports zero violations on deliberately broken code is worse than
no checker: CI would advertise a guarantee it does not provide.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))

import check_layers


def _write(root: Path, rel: str, body: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")


def _package(tmp_path: Path) -> Path:
    root = tmp_path / "demo"
    root.mkdir()
    for layer in ("core", "transport", "clients", "services"):
        (root / layer).mkdir()
    _write(root, "__init__.py", "")
    for layer in ("core", "transport", "clients", "services"):
        _write(root, f"{layer}/__init__.py", "")
    return root


@pytest.mark.parametrize(
    ("rel", "body", "expected"),
    [
        # absolute import, full package path
        ("core/a.py", "from demo.services import b\n", "core -> services"),
        ("core/a.py", "import demo.clients.b\n", "core -> clients"),
        # relative import one level up
        ("core/a.py", "from ..services import b\n", "core -> services"),
        (
            "transport/a.py",
            "from ...demo import clients\n",
            None,
        ),  # too many dots: invalid, ignored
        # relative import of a module in an upper layer
        ("transport/a.py", "from ..clients.b import thing\n", "transport -> clients"),
        # `from .. import x` form
        ("transport/a.py", "from ..services import thing\n", "transport -> services"),
    ],
)
def test_upward_imports_are_detected(
    tmp_path: Path, rel: str, body: str, expected: str | None
) -> None:
    root = _package(tmp_path)
    (root / "tools").mkdir(exist_ok=True)
    _write(root, "tools/helper.py", "thing = 1\n")
    _write(root, "services/b.py", "thing = 1\n")
    _write(root, "clients/b.py", "thing = 1\n")
    _write(root, rel, body)

    violations = check_layers.check_path(root, "demo")

    if expected is None:
        assert violations == []
    else:
        assert violations, f"checker missed {body!r}"
        assert any(expected in v for v in violations), violations


def test_downward_and_sibling_imports_are_fine(tmp_path: Path) -> None:
    root = _package(tmp_path)
    _write(root, "services/a.py", "from demo.core import b\nfrom . import b\n")
    _write(root, "core/b.py", "")
    _write(root, "services/b.py", "")

    assert check_layers.check_path(root, "demo") == []


def test_cycles_are_reported(tmp_path: Path) -> None:
    root = _package(tmp_path)
    _write(root, "clients/a.py", "from .b import x\n")
    _write(root, "clients/b.py", "from .a import y\n")

    cycles = check_layers.find_cycles(root, "demo")
    assert cycles, "cycle between clients/a and clients/b was not detected"


def test_real_repository_is_clean() -> None:
    root = Path(__file__).resolve().parents[1] / "src" / "facebook_mcp"
    assert check_layers.check_path(root, "facebook_mcp") == []
    assert check_layers.find_cycles(root, "facebook_mcp") == []


def test_repository_check_catches_an_injected_violation(tmp_path: Path) -> None:
    """Copy the real package, add one upward import, expect a failure."""
    source = Path(__file__).resolve().parents[1] / "src" / "facebook_mcp"
    root = tmp_path / "facebook_mcp"
    for path in source.rglob("*.py"):
        target = root / path.relative_to(source)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")

    victim = root / "core" / "config.py"
    victim.write_text(
        victim.read_text(encoding="utf-8") + "\nfrom facebook_mcp.clients import base\n",
        encoding="utf-8",
    )

    violations = check_layers.check_path(root, "facebook_mcp")
    assert violations, "injected upward import was not detected"
    assert any("core -> clients" in v for v in violations)


def test_package_init_relative_imports_are_resolved(tmp_path: Path) -> None:
    """``__init__.py`` *is* its package, so ``from ..x`` starts one level higher."""
    root = _package(tmp_path)
    _write(root, "core/__init__.py", "from ..clients import b\n")
    _write(root, "clients/b.py", "thing = 1\n")

    violations = check_layers.check_path(root, "demo")
    assert violations, "an upward import in core/__init__.py was not detected"
    assert any("core -> clients" in v for v in violations)


def test_package_init_sibling_import_is_fine(tmp_path: Path) -> None:
    root = _package(tmp_path)
    _write(root, "core/__init__.py", "from .b import thing\n")
    _write(root, "core/b.py", "thing = 1\n")

    assert check_layers.check_path(root, "demo") == []
