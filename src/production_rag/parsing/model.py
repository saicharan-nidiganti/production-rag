"""What extraction produces and the errors it raises, shared by every format's extractor."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


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
