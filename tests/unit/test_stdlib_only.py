"""The root daemons must import the standard library only (SPEC 5.4).

No pip package runs as root. This walks the abstract syntax tree of every module
that ends up in a root process and fails on any import that is neither part of
the standard library nor part of Anchor itself.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest

# Packages that run as root and are therefore restricted (SPEC 5.1, 5.4).
ROOT_PACKAGES = ("protocol", "engine", "blocker")

SRC = Path(__file__).resolve().parents[2] / "src" / "anchor"


def _root_modules() -> list[Path]:
    modules: list[Path] = []
    for package in ROOT_PACKAGES:
        modules.extend(sorted((SRC / package).rglob("*.py")))
    return modules


def _imported_roots(tree: ast.AST) -> set[str]:
    """Return the top-level name of every module imported by ``tree``."""
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                roots.add(alias.name.split(".")[0])
        # A relative import (level > 0) stays inside Anchor, so it is allowed.
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            roots.add(node.module.split(".")[0])
    return roots


@pytest.mark.parametrize("module", _root_modules(), ids=lambda p: str(p.relative_to(SRC)))
def test_root_daemon_imports_stdlib_only(module: Path) -> None:
    tree = ast.parse(module.read_text(encoding="utf-8"), filename=str(module))
    allowed = set(sys.stdlib_module_names) | {"anchor"}
    offenders = sorted(_imported_roots(tree) - allowed)
    assert not offenders, (
        f"{module.relative_to(SRC)} imports non-stdlib modules {offenders}. "
        "Root daemons are restricted to the standard library (SPEC 5.4). "
        "Adding a dependency here needs an ADR in docs/adr/."
    )


def test_root_packages_are_present() -> None:
    """Guard against the check silently passing because nothing was found."""
    assert len(_root_modules()) >= len(ROOT_PACKAGES)
