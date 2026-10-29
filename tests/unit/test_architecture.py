"""The package's layering (docs/architecture.md §3), checked on the source's imports.

A module may import from its own package or from a lower layer, never from the same layer
or above. This keeps the transport layers (api, cli, evaluation) replaceable and stops the
core from depending on how it is served.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from tests.conftest import REPO_ROOT

PACKAGE = REPO_ROOT / "src" / "production_rag"

LAYERS: list[set[str]] = [
    {"__init__", "config", "text", "observability"},  # foundation
    {"db", "embeddings", "parsing"},  # storage, models and text extraction
    {"ingestion", "retrieval"},  # core services
    {"generation"},  # answers from retrieved chunks
    {"rag"},  # query orchestration and abstention
    {"container"},  # wiring
    {"api", "evaluation"},  # applications on top of the container
    {"cli"},  # entry point for all of them
]
_LAYER_OF = {name: rank for rank, names in enumerate(LAYERS) for name in names}


def _package_of(module: str) -> str:
    parts = module.split(".")
    return parts[1] if len(parts) > 1 else "__init__"


def _imports(path: Path) -> set[str]:
    """Packages of production_rag that ``path`` imports, at any nesting level."""
    found: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            modules = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module == "production_rag":
            # `from production_rag import cli` imports the cli package; `__version__` is
            # an attribute of the package root.
            modules = [f"production_rag.{alias.name}" for alias in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules = [node.module]
        else:
            continue
        for module in modules:
            if module.split(".")[0] == "production_rag":
                package = _package_of(module)
                found.add(package if package in _LAYER_OF else "__init__")
    return found


def _modules() -> list[Path]:
    return sorted(PACKAGE.rglob("*.py"))


def _own_package(path: Path) -> str:
    """``ingestion/queue.py`` -> ``ingestion``; ``config.py`` -> ``config``."""
    return path.relative_to(PACKAGE).parts[0].removesuffix(".py")


def test_every_package_has_a_layer() -> None:
    packages = {_own_package(p) for p in _modules()}
    assert packages - _LAYER_OF.keys() == set(), "add new packages to LAYERS"


@pytest.mark.parametrize("path", _modules(), ids=lambda p: p.relative_to(PACKAGE).as_posix())
def test_imports_only_point_down(path: Path) -> None:
    own = _own_package(path)
    violations = sorted(
        target for target in _imports(path) if target != own and _LAYER_OF[target] >= _LAYER_OF[own]
    )
    assert violations == [], f"{own} imports {violations} from its own layer or above"
