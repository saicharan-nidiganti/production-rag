"""Text extraction for supported document types.

Each document becomes full text plus *sections*: spans that chunks must not cross, carrying
the metadata a citation needs (Markdown heading path, PDF page number).
"""

from __future__ import annotations

import io
import re
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

SUPPORTED_TYPES: dict[str, str] = {
    ".txt": "text/plain",
    ".md": "text/markdown",
    ".markdown": "text/markdown",
    ".pdf": "application/pdf",
}

PDF_SIGNATURE = b"%PDF-"


class UnsupportedDocumentError(ValueError):
    pass


class ExtractionError(ValueError):
    pass


@dataclass(frozen=True)
class Section:
    start: int  # offset of the section in the document's full text
    end: int
    heading: str | None = None
    page: int | None = None


@dataclass(frozen=True)
class ExtractedDocument:
    text: str
    title: str
    content_type: str
    sections: list[Section]
    metadata: dict[str, Any] = field(default_factory=dict)


def safe_filename(filename: str) -> str:
    """Strip any directory components and control characters from a client-supplied name."""
    name = filename.replace("\\", "/").rsplit("/", 1)[-1]
    name = "".join(ch for ch in name if ch.isprintable()).strip()
    return name[:255] or "untitled"


def content_type_for(filename: str) -> str:
    suffix = PurePosixPath(filename).suffix.lower()
    if suffix not in SUPPORTED_TYPES:
        supported = ", ".join(sorted(SUPPORTED_TYPES))
        raise UnsupportedDocumentError(f"unsupported file type {suffix!r}; supported: {supported}")
    return SUPPORTED_TYPES[suffix]


def validate_upload(filename: str, data: bytes) -> str:
    """Checks that need no parsing, run before a document is accepted. Returns the content type.

    Anything that needs parsing (a damaged PDF, Markdown with headings but no text) fails
    later, while the document is processed, and is reported in its status.
    """
    content_type = content_type_for(filename)
    if content_type == "application/pdf":
        _check_pdf_signature(data)
    elif not _decode(data).strip():
        raise ExtractionError("document contains no extractable text")
    return content_type


def discover_documents(path: Path) -> list[Path]:
    """Supported files under a directory (recursively, sorted), or ``[path]`` for a file.

    A single file is returned as given, even with an unsupported extension, so the caller
    gets a clear error for it instead of having it silently skipped.
    """
    if not path.is_dir():
        return [path]
    return sorted(p for p in path.rglob("*") if p.is_file() and p.suffix.lower() in SUPPORTED_TYPES)


def extract(filename: str, data: bytes, *, max_pdf_pages: int = 500) -> ExtractedDocument:
    content_type = content_type_for(filename)
    stem = PurePosixPath(filename).stem
    if content_type == "application/pdf":
        return _extract_pdf(data, stem, max_pdf_pages)
    text = _decode(data)
    if content_type == "text/markdown":
        return _extract_markdown(text, stem)
    return ExtractedDocument(text, stem, content_type, [Section(0, len(text))] if text else [])


def _decode(data: bytes) -> str:
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ExtractionError("text documents must be UTF-8 encoded") from exc
    return _normalize(text)


def _check_pdf_signature(data: bytes) -> None:
    # Validate by content, not extension (NFR-2): the header must appear in the first 1024 bytes.
    if PDF_SIGNATURE not in data[:1024]:
        raise UnsupportedDocumentError("file has a .pdf extension but is not a PDF")


def _normalize(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n").replace("\x00", "")


_HEADING = re.compile(r"^(#{1,6})[ \t]+(.+?)[ \t]*#*[ \t]*$")
_FENCE = re.compile(r"^[ \t]*(```|~~~)")


def _extract_markdown(text: str, stem: str) -> ExtractedDocument:
    headings: list[tuple[int, int, str, int]] = []  # (line start, line end, path, level)
    stack: list[tuple[int, str]] = []
    in_fence = False
    offset = 0
    for line in text.splitlines(keepends=True):
        if _FENCE.match(line):
            in_fence = not in_fence
        elif not in_fence and (match := _HEADING.match(line.rstrip("\n"))):
            level, title = len(match.group(1)), match.group(2).strip()
            stack = [(lvl, t) for lvl, t in stack if lvl < level] + [(level, title)]
            headings.append((offset, offset + len(line), " > ".join(t for _, t in stack), level))
        offset += len(line)

    sections: list[Section] = []
    first_heading_start = headings[0][0] if headings else len(text)
    if text[:first_heading_start].strip():
        sections.append(Section(0, first_heading_start))
    for i, (start, line_end, path, _level) in enumerate(headings):
        end = headings[i + 1][0] if i + 1 < len(headings) else len(text)
        if text[line_end:end].strip():  # skip headings with no body of their own
            sections.append(Section(start, end, heading=path))

    h1 = next((path for _, _, path, level in headings if level == 1), None)
    return ExtractedDocument(text, h1 or stem, "text/markdown", sections)


def _extract_pdf(data: bytes, stem: str, max_pages: int) -> ExtractedDocument:
    from pypdf import PdfReader
    from pypdf.errors import PdfReadError

    _check_pdf_signature(data)
    try:
        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted:
            raise ExtractionError("encrypted PDFs are not supported")
        if len(reader.pages) > max_pages:
            raise ExtractionError(f"PDF has {len(reader.pages)} pages; the limit is {max_pages}")
        page_texts = [_normalize(page.extract_text() or "") for page in reader.pages]
        title = (reader.metadata.title if reader.metadata else None) or stem
    except PdfReadError as exc:
        raise ExtractionError(f"could not read PDF: {exc}") from exc

    parts: list[str] = []
    sections: list[Section] = []
    offset = 0
    for number, page_text in enumerate(page_texts, start=1):
        if parts:
            parts.append("\n\n")
            offset += 2
        parts.append(page_text)
        if page_text.strip():
            sections.append(Section(offset, offset + len(page_text), page=number))
        offset += len(page_text)

    return ExtractedDocument(
        "".join(parts),
        str(title).strip() or stem,
        "application/pdf",
        sections,
        metadata={"pages": len(page_texts)},
    )
