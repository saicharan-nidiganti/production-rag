"""Text and heading sections from Word documents (``.docx``, FR-2), with the standard library.

A ``.docx`` file is a ZIP archive of XML parts. The main document part is streamed with
``iterparse`` and each element is dropped once read, so memory stays proportional to one
paragraph or table rather than the whole document.

What is kept: the body's paragraphs, with paragraphs in a heading style (or with an outline
level) as headings, so sections carry the heading path as for Markdown; list items on their
own lines with a marker; table rows as lines with cells joined by ``" | "``; text boxes;
inserted text of tracked changes.

What is dropped: deleted text of tracked changes, field codes, headers and footers,
footnotes, endnotes, comments, images and embedded objects. The alternative copy of a text
box that Word writes for older readers is skipped, so its text appears once.

Hostile files are refused before their XML is read: only parts within a size limit are read,
the archive may not hold more than ``MAX_ENTRIES`` entries, and a part with a document type
declaration is rejected (Word never writes one; entity tricks need one). Macro-enabled
documents are refused; their macros would never run here, but they aren't ``.docx`` files.
"""

from __future__ import annotations

import io
import re
import zipfile
import zlib
from collections.abc import Iterator
from dataclasses import dataclass, field
from xml.etree import ElementTree

from production_rag.parsing.model import (
    ExtractedDocument,
    ExtractionError,
    UnsupportedDocumentError,
)
from production_rag.parsing.structure import TextBuilder, first_top_level, heading_sections

CONTENT_TYPE = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"

ZIP_SIGNATURE = b"PK\x03\x04"
# Legacy .doc files, and password-protected .docx files, are OLE compound files.
OLE_SIGNATURE = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"

MAX_ENTRIES = 10_000
MAX_DOCUMENT_PART_BYTES = 64 * 1024 * 1024  # uncompressed main document XML
MAX_SMALL_PART_BYTES = 8 * 1024 * 1024  # content types, styles, document properties

_MAIN_TYPES = {
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.template.main+xml",
}
_MACRO_TYPES = {
    "application/vnd.ms-word.document.macroEnabled.main+xml",
    "application/vnd.ms-word.template.macroEnabledTemplate.main+xml",
}

_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
_CT = "{http://schemas.openxmlformats.org/package/2006/content-types}"
_DC_TITLE = "{http://purl.org/dc/elements/1.1/}title"
_FALLBACK = "{http://schemas.openxmlformats.org/markup-compatibility/2006}Fallback"

_P, _TBL, _TR, _TC = f"{_W}p", f"{_W}tbl", f"{_W}tr", f"{_W}tc"
_T, _TAB, _PTAB, _BR, _CR = f"{_W}t", f"{_W}tab", f"{_W}ptab", f"{_W}br", f"{_W}cr"
_NO_BREAK_HYPHEN = f"{_W}noBreakHyphen"
_PPR, _PSTYLE, _OUTLINE, _NUMPR = f"{_W}pPr", f"{_W}pStyle", f"{_W}outlineLvl", f"{_W}numPr"
_VAL = f"{_W}val"

_HEADING_NAME = re.compile(r"^heading ([1-9])$", re.I)
_HEADING_ID = re.compile(r"^Heading([1-9])$")
_SPACES = re.compile(r"[ \t]+")


def check_signature(data: bytes) -> None:
    """Validate by content, not extension (NFR-2); needs no parsing, so it runs at upload."""
    if data.startswith(OLE_SIGNATURE):
        raise UnsupportedDocumentError(
            "legacy .doc files and password-protected Word documents are not supported; "
            "save it as a .docx without a password"
        )
    if not data.startswith(ZIP_SIGNATURE):
        raise UnsupportedDocumentError("file has a .docx extension but is not a Word document")


def extract_docx(data: bytes, stem: str) -> ExtractedDocument:
    check_signature(data)
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            if len(archive.infolist()) > MAX_ENTRIES:
                raise ExtractionError(f"DOCX has over {MAX_ENTRIES} parts")
            main = _main_part(archive)
            folder = main.rpartition("/")[0]
            styles = _Styles.parse(
                _read_part(archive, f"{folder}/styles.xml" if folder else "styles.xml")
            )
            title = _core_title(_read_part(archive, "docProps/core.xml"))
            document = _read_part(archive, main, MAX_DOCUMENT_PART_BYTES)
    except (zipfile.BadZipFile, zlib.error, EOFError, NotImplementedError) as exc:
        raise ExtractionError("could not read DOCX: the file is damaged") from exc
    if document is None:
        raise ExtractionError("DOCX has no document part")

    out, title_paragraph = _Body(styles).read(document)
    text = out.text
    title = title or title_paragraph or first_top_level(out.headings) or stem
    return ExtractedDocument(text, title, CONTENT_TYPE, heading_sections(text, out.headings))


# --- Package parts ---


def _read_part(
    archive: zipfile.ZipFile, name: str, limit: int = MAX_SMALL_PART_BYTES
) -> bytes | None:
    try:
        info = archive.getinfo(name)
    except KeyError:
        return None
    if info.flag_bits & 0x1:
        raise ExtractionError("encrypted DOCX parts are not supported")
    if info.file_size > limit:
        raise ExtractionError(
            f"DOCX part {name} is {info.file_size} bytes uncompressed; the limit is {limit}"
        )
    with archive.open(info) as part:
        data = part.read(limit + 1)  # the archive's size field could lie; never read past it
    if len(data) > limit:
        raise ExtractionError(f"DOCX part {name} is over the limit of {limit} bytes")
    if b"<!DOCTYPE" in data or b"<!ENTITY" in data:
        raise ExtractionError("DOCX contains a document type declaration, which Word never writes")
    return data


# Safe to parse with ElementTree: _read_part refuses any part with a document type or entity
# declaration, so no entity can be expanded, and ElementTree never fetches external resources.


def _xml(data: bytes) -> ElementTree.Element:
    try:
        return ElementTree.fromstring(data)  # noqa: S314 (see above)
    except ElementTree.ParseError as exc:
        raise ExtractionError("could not read DOCX: malformed XML") from exc


def _main_part(archive: zipfile.ZipFile) -> str:
    """The main document part, as ``[Content_Types].xml`` names it (usually word/document.xml)."""
    types = _read_part(archive, "[Content_Types].xml")
    if types is None:
        raise UnsupportedDocumentError("file has a .docx extension but is not a Word document")
    overrides = {
        o.get("ContentType", ""): o.get("PartName", "") for o in _xml(types).iter(f"{_CT}Override")
    }
    if _MACRO_TYPES & overrides.keys():
        raise UnsupportedDocumentError("macro-enabled Word documents (.docm) are not supported")
    for content_type in _MAIN_TYPES:
        if part := overrides.get(content_type, "").lstrip("/"):
            return part
    raise UnsupportedDocumentError("file has a .docx extension but is not a Word document")


def _core_title(data: bytes | None) -> str:
    if data is None:
        return ""
    element = _xml(data).find(_DC_TITLE)
    return " ".join((element.text or "").split()) if element is not None else ""


@dataclass
class _Styles:
    """Heading levels and title styles by style id, following each style's ``basedOn`` chain."""

    levels: dict[str, int] = field(default_factory=dict)
    titles: set[str] = field(default_factory=set)

    @classmethod
    def parse(cls, data: bytes | None) -> _Styles:
        styles = cls()
        if data is None:
            return styles
        raw: dict[str, tuple[str, str | None, int | None]] = {}  # id: name, based on, outline
        for style in _xml(data).iter(f"{_W}style"):
            if style.get(f"{_W}type", "paragraph") != "paragraph":
                continue
            style_id = style.get(f"{_W}styleId")
            if not style_id:
                continue
            name = _attribute(style.find(f"{_W}name")) or ""
            based_on = _attribute(style.find(f"{_W}basedOn"))
            outline = _outline_level(style.find(_PPR))
            raw[style_id] = (name, based_on, outline)
        for style_id in raw:
            level, is_title = cls._resolve(raw, style_id)
            if level is not None:
                styles.levels[style_id] = level
            if is_title:
                styles.titles.add(style_id)
        return styles

    @staticmethod
    def _resolve(
        raw: dict[str, tuple[str, str | None, int | None]], style_id: str
    ) -> tuple[int | None, bool]:
        seen: set[str] = set()
        current: str | None = style_id
        while current is not None and current in raw and current not in seen:
            seen.add(current)
            name, based_on, outline = raw[current]
            if name.lower() == "title":
                return None, True
            if outline is not None:
                return (outline + 1 if outline < 9 else None), False
            if match := _HEADING_NAME.match(name):
                return int(match.group(1)), False
            current = based_on
        return None, False

    def heading_level(self, style_id: str | None) -> int | None:
        if style_id is None:
            return None
        if style_id in self.levels:
            return self.levels[style_id]
        match = _HEADING_ID.match(style_id)  # documents without a styles part
        return int(match.group(1)) if match else None

    def is_title(self, style_id: str | None) -> bool:
        return style_id is not None and (style_id in self.titles or style_id == "Title")


def _attribute(element: ElementTree.Element | None) -> str | None:
    return element.get(_VAL) if element is not None else None


def _outline_level(ppr: ElementTree.Element | None) -> int | None:
    value = _attribute(ppr.find(_OUTLINE)) if ppr is not None else None
    return int(value) if value is not None and value.isdigit() else None


# --- The body ---


@dataclass
class _Paragraph:
    text: str
    level: int | None
    is_title: bool
    is_list_item: bool


class _Body:
    def __init__(self, styles: _Styles) -> None:
        self._styles = styles
        self._out = TextBuilder()
        self._title = ""
        self._in_list = False
        self._cells: list[list[str]] = []  # paragraphs of each open table cell
        self._rows: list[list[str]] = []  # cells of each open table row

    def read(self, document: bytes) -> tuple[TextBuilder, str]:
        fallback = 0  # depth inside a compatibility fallback, which repeats its choice
        for event, element in self._events(document):
            tag = element.tag
            if tag == _FALLBACK:
                fallback += 1 if event == "start" else -1
                if event == "end":
                    element.clear()
                continue
            if fallback:
                continue
            if event == "start":
                if tag == _TR:
                    self._rows.append([])
                elif tag == _TC:
                    self._cells.append([])
                elif tag == _TBL and not self._cells:  # a table starts a new paragraph
                    self._out.paragraph_break()
                    self._in_list = False
                continue
            if tag == _P:
                self._paragraph(self._read_paragraph(element))
            elif tag == _TC and self._cells:
                cell = " ".join(self._cells.pop())
                if self._rows:
                    self._rows[-1].append(cell)
            elif tag == _TR and self._rows:
                self._row(" | ".join(c for c in self._rows.pop() if c))
            elif tag == _TBL and not self._cells:
                self._out.paragraph_break()
                self._in_list = False
            else:
                continue
            element.clear()
        return self._out, self._title

    @staticmethod
    def _events(document: bytes) -> Iterator[tuple[str, ElementTree.Element]]:
        try:
            # Safe for the reason given at _xml: _read_part refused DTDs and entities.
            yield from ElementTree.iterparse(  # noqa: S314
                io.BytesIO(document), events=("start", "end")
            )
        except ElementTree.ParseError as exc:
            raise ExtractionError("could not read DOCX: malformed XML") from exc

    def _read_paragraph(self, element: ElementTree.Element) -> _Paragraph:
        pieces: list[str] = []
        for node in element.iter():  # paragraphs nested in text boxes were already read
            tag = node.tag
            if tag == _T:
                pieces.append(node.text or "")
            elif tag in (_TAB, _PTAB):
                pieces.append(" ")
            elif tag in (_BR, _CR):
                pieces.append("\n")
            elif tag == _NO_BREAK_HYPHEN:
                pieces.append("-")
        lines = (_SPACES.sub(" ", line).strip() for line in "".join(pieces).split("\n"))
        text = "\n".join(line for line in lines if line).replace("\x00", "")
        ppr = element.find(_PPR)
        style_id = _attribute(ppr.find(_PSTYLE)) if ppr is not None else None
        outline = _outline_level(ppr)
        if outline is not None:  # set on the paragraph itself; 9 means body text
            level = outline + 1 if outline < 9 else None
        else:
            level = self._styles.heading_level(style_id)
        return _Paragraph(
            text=text,
            level=level,
            is_title=self._styles.is_title(style_id),
            is_list_item=ppr is not None and ppr.find(_NUMPR) is not None,
        )

    def _paragraph(self, paragraph: _Paragraph) -> None:
        if not paragraph.text:
            return
        if self._cells:  # inside a table cell: joined into the cell's text
            self._cells[-1].append(" ".join(paragraph.text.split()))
            return
        if paragraph.level is not None:
            self._out.start_heading(paragraph.level)
            self._out.write(" ".join(paragraph.text.split()))
            self._out.end_heading()
            self._in_list = False
            return
        if paragraph.is_title and not self._title:
            self._title = " ".join(paragraph.text.split())
        if paragraph.is_list_item:
            if self._in_list:
                self._out.line_break()
            else:
                self._out.paragraph_break()
            self._out.prefix("- ")
            self._in_list = True
        else:
            self._out.paragraph_break()
            self._in_list = False
        self._out.write(paragraph.text)

    def _row(self, text: str) -> None:
        if not text:
            return
        if self._cells:  # a table nested in a cell: its rows join the outer cell
            self._cells[-1].append(text)
            return
        self._out.line_break()
        self._out.write(text)
