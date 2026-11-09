"""Text extraction for supported document types.

Each document becomes full text plus *sections*: spans that chunks must not cross, carrying
the metadata a citation needs (heading path for Markdown, HTML and DOCX; page number for PDF).
"""

from __future__ import annotations

import io
import re
from pathlib import Path, PurePosixPath

from production_rag.parsing import docx_text, html_text
from production_rag.parsing.model import (
    ExtractedDocument,
    ExtractionError,
    Section,
    UnsupportedDocumentError,
)
from production_rag.parsing.structure import (
    Heading,
    HeadingPath,
    first_top_level,
    heading_sections,
)

__all__ = [
    "SUPPORTED_TYPES",
    "ExtractedDocument",
    "ExtractionError",
    "Section",
    "UnsupportedDocumentError",
    "content_type_for",
    "discover_documents",
    "extract",
    "safe_filename",
    "validate_upload",
]

SUPPORTED_TYPES: dict[str, str] = {
    ".txt": "text/plain",
    ".md": "text/markdown",
    ".markdown": "text/markdown",
    ".pdf": "application/pdf",
    ".docx": docx_text.CONTENT_TYPE,
    ".html": html_text.CONTENT_TYPE,
    ".htm": html_text.CONTENT_TYPE,
}

PDF_SIGNATURE = b"%PDF-"


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
    elif content_type == docx_text.CONTENT_TYPE:
        docx_text.check_signature(data)
    elif content_type == html_text.CONTENT_TYPE:
        if not html_text.decode_html(data).strip():
            raise ExtractionError("document contains no extractable text")
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
    if content_type == docx_text.CONTENT_TYPE:
        return docx_text.extract_docx(data, stem)
    if content_type == html_text.CONTENT_TYPE:
        return html_text.extract_html(data, stem)
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
    headings: list[Heading] = []  # (line start, line end, path, level)
    paths = HeadingPath()
    in_fence = False
    offset = 0
    for line in text.splitlines(keepends=True):
        if _FENCE.match(line):
            in_fence = not in_fence
        elif not in_fence and (match := _HEADING.match(line.rstrip("\n"))):
            level, title = len(match.group(1)), match.group(2).strip()
            headings.append((offset, offset + len(line), paths.push(level, title), level))
        offset += len(line)
    title = first_top_level(headings) or stem
    return ExtractedDocument(text, title, "text/markdown", heading_sections(text, headings))


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
