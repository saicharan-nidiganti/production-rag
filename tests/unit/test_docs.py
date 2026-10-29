"""Keep the documentation consistent with the code and with itself."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from production_rag.config import Settings
from tests.conftest import REPO_ROOT

_SKIP_DIRS = {".venv", "node_modules", ".pytest_cache", ".mypy_cache", ".ruff_cache", ".git"}
_LINK = re.compile(r"\[[^\]]*\]\(([^)\s]+)\)")


def _markdown_files() -> list[Path]:
    return sorted(
        p
        for p in REPO_ROOT.rglob("*.md")
        if not _SKIP_DIRS.intersection(p.relative_to(REPO_ROOT).parts)
        and "corpus" not in p.relative_to(REPO_ROOT).parts
    )


def _anchors(path: Path) -> set[str]:
    """GitHub-style heading anchors: lowercase, punctuation dropped, spaces to hyphens."""
    anchors = set()
    in_fence = False
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
        elif not in_fence and line.startswith("#"):
            title = line.lstrip("#").strip().lower().replace("`", "")
            anchors.add(re.sub(r"[^\w\- ]", "", title).replace(" ", "-"))
    return anchors


def _links(path: Path) -> list[tuple[int, str]]:
    found = []
    in_fence = False
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
        elif not in_fence:
            found.extend((number, target) for target in _LINK.findall(line))
    return found


@pytest.mark.parametrize(
    "doc", _markdown_files(), ids=lambda p: p.relative_to(REPO_ROOT).as_posix()
)
def test_relative_links_resolve(doc: Path) -> None:
    broken = []
    for number, target in _links(doc):
        if re.match(r"^[a-z][a-z0-9+.-]*:", target):  # http:, https:, mailto:
            continue
        file_part, _, anchor = target.partition("#")
        destination = (doc.parent / file_part).resolve() if file_part else doc
        if not destination.exists():
            broken.append(f"line {number}: {target} (no such file)")
        elif anchor and destination.suffix == ".md" and anchor not in _anchors(destination):
            broken.append(f"line {number}: {target} (no such heading)")
    assert broken == []


def _environment_names() -> list[str]:
    names = []
    for name, field in Settings.model_fields.items():
        alias = field.validation_alias
        names.append(alias.choices[-1] if alias is not None else f"RAG_{name.upper()}")
    return names


@pytest.mark.parametrize("variable", _environment_names())
def test_every_setting_is_documented(variable: str) -> None:
    assert f"`{variable}`" in (REPO_ROOT / "docs" / "configuration.md").read_text(encoding="utf-8")
    assert re.search(
        rf"^#? ?{variable}=", (REPO_ROOT / ".env.example").read_text(encoding="utf-8"), re.M
    ), f"{variable} missing from .env.example"


_MERMAID = re.compile(r"```mermaid\n(.*?)```", re.S)


def _mid_line_semicolons(markdown: str) -> list[str]:
    """Mermaid lines with a semicolon followed by more text, outside quotes.

    Mermaid reads `;` as a statement separator, so `A-->>B: ready; then go` ends the
    message at the semicolon and fails to parse the rest; GitHub then shows an error box
    instead of the diagram.
    """
    found = []
    for block in _MERMAID.findall(markdown):
        for line in block.splitlines():
            unquoted = re.sub(r'"[^"]*"', '""', line)
            if re.search(r";\s*\S", unquoted):
                found.append(line.strip())
    return found


@pytest.mark.parametrize(
    "doc", _markdown_files(), ids=lambda p: p.relative_to(REPO_ROOT).as_posix()
)
def test_mermaid_diagrams_have_no_statement_breaking_semicolons(doc: Path) -> None:
    assert _mid_line_semicolons(doc.read_text(encoding="utf-8")) == []


def test_the_semicolon_check_catches_a_broken_message() -> None:
    broken = "```mermaid\nsequenceDiagram\n    A-->>C: 200 if ready, else 202; created=false\n```\n"
    fine = '```mermaid\nflowchart LR\n    a["x; y"] --> b;\n```\n'
    assert _mid_line_semicolons(broken) == ["A-->>C: 200 if ready, else 202; created=false"]
    assert _mid_line_semicolons(fine) == []


def test_package_version_matches_pyproject() -> None:
    import tomllib

    from production_rag import __version__

    pyproject = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert __version__ == pyproject["project"]["version"]
