"""Text and heading sections from HTML (FR-2), with the standard library's tolerant parser.

What is kept: the text a reader sees in the body, with ``h1`` to ``h6`` as headings (sections
carry the heading path, as for Markdown), paragraphs and other blocks separated by blank
lines, list items on their own lines with a marker, table rows as lines with cells joined by
``" | "``, and ``pre`` text as written.

What is dropped: the head (except ``title``), scripts, styles, ``noscript``, templates,
embedded SVG and MathML, frames and objects, and elements with the ``hidden`` attribute. No
CSS is applied, so text hidden only by a style is kept, and nothing is executed, so text
that a script would add is missing.
"""

from __future__ import annotations

import codecs
import re
from html.parser import HTMLParser

from production_rag.parsing.model import ExtractedDocument, ExtractionError
from production_rag.parsing.structure import TextBuilder, first_top_level, heading_sections

CONTENT_TYPE = "text/html"

_SKIPPED = frozenset(
    {"script", "style", "noscript", "template", "svg", "math", "iframe", "object", "canvas"}
)
_VOID = frozenset(
    {
        "area",
        "base",
        "br",
        "col",
        "embed",
        "hr",
        "img",
        "input",
        "link",
        "meta",
        "source",
        "track",
        "wbr",
    }
)
_BLOCKS = frozenset(
    {
        "address", "article", "aside", "blockquote", "caption", "details", "dialog", "div", "dl",
        "fieldset", "figcaption", "figure", "footer", "form", "header", "hgroup", "legend",
        "main", "nav", "p", "section", "summary", "table",
    }
)  # fmt: skip
_LINES = frozenset({"dt", "dd", "tr"})
_HEADINGS = {f"h{level}": level for level in range(1, 7)}
_LISTS = frozenset({"ul", "ol", "menu"})
_CELLS = frozenset({"td", "th"})
_WHITESPACE = re.compile(r"[ \t\n\r\f]+")  # HTML's whitespace; a no-break space is text
_META_CHARSET = re.compile(rb"""<meta\b[^>]*?charset\s*=\s*["']?\s*([A-Za-z0-9._:-]+)""", re.I)


def decode_html(data: bytes) -> str:
    """HTML's text: a byte-order mark first, then UTF-8, then the encoding a ``<meta>`` declares.

    UTF-8 is tried before the declaration because a file that decodes as UTF-8 almost
    certainly is UTF-8, and many pages declare an encoding they no longer use.
    """
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return _decode(data, "utf-16")
    try:
        return _normalize(data.decode("utf-8-sig"))
    except UnicodeDecodeError:
        pass
    declared = _META_CHARSET.search(data[:1024])  # where the HTML standard looks for it
    if declared is None:
        raise ExtractionError(
            "HTML documents must be UTF-8 or declare their encoding in a <meta charset> tag"
        )
    return _decode(data, declared.group(1).decode("ascii"))


def _decode(data: bytes, encoding: str) -> str:
    try:
        codecs.lookup(encoding)
        # bytes.decode refuses codecs that aren't text encodings (base64, zlib) with LookupError.
        return _normalize(data.decode(encoding))
    except LookupError as exc:
        raise ExtractionError(f"unknown HTML encoding {encoding!r}") from exc
    except UnicodeDecodeError as exc:
        raise ExtractionError(f"HTML is not valid {encoding}") from exc


def _normalize(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n").replace("\x00", "")


def extract_html(data: bytes, stem: str) -> ExtractedDocument:
    parser = _Parser()
    parser.feed(decode_html(data))
    parser.close()
    out = parser.finish()
    text = out.text
    title = " ".join(parser.title.split()) or first_top_level(out.headings) or stem
    return ExtractedDocument(text, title, CONTENT_TYPE, heading_sections(text, out.headings))


class _Parser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.out = TextBuilder()
        self.title = ""
        self._skip: tuple[str, int] | None = None  # skipped element and its nesting depth
        self._in_head = False
        self._in_title = False
        self._pre: list[str] | None = None  # text of the open pre element
        self._pre_depth = 0
        self._lists: list[list[int]] = []  # per open list: [ordered (0 or 1), items so far]

    # --- Start and end tags ---

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._in_title = False  # a title holds only text; any tag ends an unclosed one
        if self._skip is not None:
            if tag == self._skip[0]:
                self._skip = (tag, self._skip[1] + 1)
            return
        if tag in _VOID:
            self._void(tag)
            return
        if tag in _SKIPPED or any(name == "hidden" for name, _ in attrs):
            self._skip = (tag, 1)
            return
        if tag == "head":
            self._in_head = True
            return
        if tag == "title":
            self._in_title = True
            return
        # Anything else belongs to the body, as browsers read it: </head> and <body> are optional.
        self._in_head = False
        if tag == "pre":
            self._pre_depth += 1
            if self._pre is None:
                self.out.paragraph_break()
                self._pre = []
        elif tag in _HEADINGS:
            self.out.start_heading(_HEADINGS[tag])
        elif tag in _LISTS:
            self._list_break()
            self._lists.append([int(tag == "ol"), 0])
        elif tag == "li":
            self._list_item()
        elif tag in _CELLS:
            self.out.separator(" | ")
        elif tag in _LINES:
            self.out.line_break()
        elif tag in _BLOCKS:
            self.out.paragraph_break()

    def handle_endtag(self, tag: str) -> None:
        if self._skip is not None:
            name, depth = self._skip
            if tag == name:
                self._skip = None if depth == 1 else (name, depth - 1)
            return
        if tag == "head":
            self._in_head = False
        elif tag == "title":
            self._in_title = False
        elif self._in_head:
            pass
        elif tag == "pre" and self._pre_depth:
            self._pre_depth -= 1
            if not self._pre_depth:
                self._flush_pre()
        elif tag in _HEADINGS:
            self.out.end_heading()
        elif tag in _LISTS:
            if self._lists:
                self._lists.pop()
            self._list_break()
        elif tag in _LINES or tag == "li":
            self.out.line_break()
        elif tag in _BLOCKS:
            self.out.paragraph_break()

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        # <br/> and friends. A self-closed element otherwise holds nothing: the parser doesn't
        # enter script or style content for <script/>, so it mustn't start a skip either.
        if self._skip is not None or self._in_head:
            return
        if tag in _VOID:
            self._void(tag)
        elif tag in _BLOCKS:
            self.out.paragraph_break()

    def _void(self, tag: str) -> None:
        if self._in_head or self._skip is not None:
            return
        if self._pre is not None and tag == "br":
            self._pre.append("\n")
        elif tag == "br":
            self.out.line_break()
        elif tag == "hr":
            self.out.paragraph_break()

    def _list_break(self) -> None:
        # A list starts a new paragraph; a list nested in an item starts a new line.
        if self._lists:
            self.out.line_break()
        else:
            self.out.paragraph_break()

    def _list_item(self) -> None:
        self.out.line_break()
        if self._lists and self._lists[-1][0]:
            self._lists[-1][1] += 1
            self.out.prefix(f"{self._lists[-1][1]}. ")
        else:
            self.out.prefix("- ")

    # --- Text ---

    def handle_data(self, data: str) -> None:
        if self._skip is not None:
            return
        if self._in_title:
            self.title += data
            return
        if self._in_head:
            if not data.strip():
                return
            self._in_head = False  # text can't be in the head: the body has begun
        if self._pre is not None:
            self._pre.append(data)
        else:
            collapsed = _WHITESPACE.sub(" ", data)
            core = collapsed.strip(" ")
            if collapsed.startswith(" "):
                self.out.space()
            self.out.write(core)
            if core and collapsed.endswith(" "):
                self.out.space()

    def _flush_pre(self) -> None:
        text = "".join(self._pre or []).strip("\n")
        self._pre = None
        if text.strip():
            self.out.write(text)
        self.out.paragraph_break()

    def finish(self) -> TextBuilder:
        if self._pre is not None:  # an unclosed pre runs to the end of the document
            self._flush_pre()
        self.out.end_heading()
        return self.out
