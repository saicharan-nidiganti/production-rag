"""Boundary-aware character chunking.

Chunks never cross a section boundary. Within a section, each chunk ends at the best
natural break in the second half of the size window: paragraph, then sentence, then
word. Offsets always point into the document's full text, so a citation can be mapped
back to its exact source span.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Sequence
from dataclasses import dataclass

from production_rag.parsing.extract import Section

_SENTENCE_END = re.compile(r"[.!?][\"')\]]?\s")


@dataclass(frozen=True)
class ChunkSpan:
    start: int
    end: int
    text: str
    heading: str | None
    page: int | None


def chunk_key(content_hash: str, start: int, end: int) -> str:
    """A chunk's stable identifier: a hash of the document's content and the chunk's span.

    Re-ingesting identical bytes with the same chunk settings yields the same keys, even
    after the document was deleted and its database ids changed. Different content always
    yields different keys; a different chunk size or overlap changes the keys of the
    chunks whose spans move. Two chunks share a key only if they cover the same text of the
    same content.
    """
    return hashlib.sha256(f"{content_hash}:{start}:{end}".encode()).hexdigest()[:32]


def chunk_document(
    text: str, sections: Sequence[Section], *, chunk_size: int, overlap: int
) -> list[ChunkSpan]:
    if overlap >= chunk_size:
        raise ValueError("overlap must be smaller than chunk_size")
    chunks: list[ChunkSpan] = []
    for section in sections:
        for start, end in _split(text, section.start, section.end, chunk_size, overlap):
            chunks.append(ChunkSpan(start, end, text[start:end], section.heading, section.page))
    return chunks


def _split(text: str, lo: int, hi: int, size: int, overlap: int) -> list[tuple[int, int]]:
    spans: list[tuple[int, int]] = []
    start = lo
    while True:
        while start < hi and text[start].isspace():
            start += 1
        if start >= hi:
            break
        window_end = min(start + size, hi)
        cut = window_end if window_end == hi else _best_break(text, start, window_end, size)
        end = cut
        while end > start and text[end - 1].isspace():
            end -= 1
        spans.append((start, end))
        if cut >= hi:
            break
        start = _next_start(text, max(cut - overlap, start + 1), cut)
    return spans


def _best_break(text: str, start: int, window_end: int, size: int) -> int:
    """Position just after the best separator in the second half of the window."""
    floor = start + size // 2
    window = text[floor:window_end]
    if (i := window.rfind("\n\n")) != -1:
        return floor + i + 2
    sentence_ends = list(_SENTENCE_END.finditer(window))
    if sentence_ends:
        return floor + sentence_ends[-1].end()
    if (i := max(window.rfind(" "), window.rfind("\n"), window.rfind("\t"))) != -1:
        return floor + i + 1
    return window_end


def _next_start(text: str, candidate: int, limit: int) -> int:
    """Move an overlap start forward to the beginning of a word, so chunks don't begin mid-word."""
    pos = candidate
    while pos < limit and not text[pos - 1].isspace():
        pos += 1
    return pos if pos < limit else candidate
