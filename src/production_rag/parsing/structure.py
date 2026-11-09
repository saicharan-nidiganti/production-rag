"""Heading sections, shared by Markdown, HTML and DOCX, and a text builder for formats that
are read element by element (HTML, DOCX).

A document with headings gets one section per heading, from the heading to the next one,
carrying the heading path ("Guide > Install > Windows"). Text before the first heading is a
section without a heading, and a heading with no body of its own gets no section, so a chunk
never consists of a heading alone.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field

from production_rag.parsing.model import Section

# (start of the heading, end of the heading text, heading path, level)
Heading = tuple[int, int, str, int]


class HeadingPath:
    """The path of the current heading, such as "Guide > Install > Windows"."""

    def __init__(self) -> None:
        self._stack: list[tuple[int, str]] = []

    def push(self, level: int, title: str) -> str:
        self._stack = [(lvl, t) for lvl, t in self._stack if lvl < level] + [(level, title)]
        return " > ".join(t for _, t in self._stack)


def heading_sections(text: str, headings: Sequence[Heading]) -> list[Section]:
    sections: list[Section] = []
    first_heading_start = headings[0][0] if headings else len(text)
    if text[:first_heading_start].strip():
        sections.append(Section(0, first_heading_start))
    for i, (start, heading_end, path, _level) in enumerate(headings):
        end = headings[i + 1][0] if i + 1 < len(headings) else len(text)
        if text[heading_end:end].strip():  # skip headings with no body of their own
            sections.append(Section(start, end, heading=path))
    return sections


def first_top_level(headings: Sequence[Heading]) -> str | None:
    """The path of the first level-1 heading: a document's title when it declares none."""
    return next((path for _, _, path, level in headings if level == 1), None)


@dataclass
class _OpenHeading:
    level: int
    start: int | None = None
    pieces: list[str] = field(default_factory=list)


class TextBuilder:
    """Assembles a document's text from runs of inline text and requested breaks.

    Breaks and spaces are requested, not written: consecutive requests collapse to the
    largest, a break wins over a pending space, and nothing is written before the first text
    or after the last. So the text never has leading, trailing or doubled blank lines, and
    offsets recorded while building (headings) never need adjusting afterwards.
    """

    def __init__(self) -> None:
        self._parts: list[str] = []
        self._length = 0
        self._break = 0  # newlines to write before the next text: 1 (line) or 2 (paragraph)
        self._inline = ""  # a space or a cell separator to write before the next text
        self._prefix = ""  # a list marker to write before the next text
        self._paths = HeadingPath()
        self._heading: _OpenHeading | None = None
        self.headings: list[Heading] = []

    @property
    def text(self) -> str:
        return "".join(self._parts)

    def line_break(self) -> None:
        self._break = max(self._break, 1)

    def paragraph_break(self) -> None:
        self._break = 2

    def space(self) -> None:
        if not self._inline:
            self._inline = " "

    def separator(self, text: str) -> None:
        self._inline = text

    def prefix(self, text: str) -> None:
        self._prefix = text

    def write(self, text: str) -> None:
        if not text:
            return
        if self._length:
            if self._break:
                self._append("\n" * self._break)
            elif self._inline:
                self._append(self._inline)
        self._break, self._inline = 0, ""
        if self._heading is not None and self._heading.start is None:
            self._heading.start = self._length
        if self._prefix:
            self._append(self._prefix)
            self._prefix = ""
        self._append(text)

    def start_heading(self, level: int) -> None:
        if self._heading is not None:  # a heading can't contain another: close the first
            self.end_heading()
        self.paragraph_break()
        self._prefix = ""
        self._heading = _OpenHeading(level)

    def end_heading(self) -> None:
        heading, self._heading = self._heading, None
        if heading is None or heading.start is None:
            return  # nothing was written inside it
        title = " ".join("".join(heading.pieces).split())
        if title:
            path = self._paths.push(heading.level, title)
            self.headings.append((heading.start, self._length, path, heading.level))
        self.paragraph_break()

    def _append(self, text: str) -> None:
        self._parts.append(text)
        self._length += len(text)
        if self._heading is not None and self._heading.start is not None:
            self._heading.pieces.append(text)
