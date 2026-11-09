"""HTML extraction (FR-2): headings, titles, layout, dropped content, bad markup, encodings."""

from __future__ import annotations

import pytest

from production_rag.parsing.extract import ExtractionError, extract, validate_upload

GUIDE = b"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>  Guide &amp; Handbook </title>
  <style>body { color: red }</style>
  <script>var note = "never indexed";</script>
</head>
<body>
  <p>Preamble   text,
     wrapped.</p>
  <h1>Guide</h1>
  <p>Intro with <b>bold</b>, <a href="#">a link</a> and caf&eacute;.</p>
  <h2>Install</h2>
  <p>Run the installer.</p>
  <h2>Empty</h2>
  <h2>Usage</h2>
  <p>Call it.</p>
</body>
</html>
"""


def test_headings_become_sections_with_heading_paths() -> None:
    doc = extract("guide.html", GUIDE)
    assert doc.content_type == "text/html"
    assert doc.title == "Guide & Handbook"
    assert doc.text == (
        "Preamble text, wrapped.\n\nGuide\n\nIntro with bold, a link and café.\n\n"
        "Install\n\nRun the installer.\n\nEmpty\n\nUsage\n\nCall it."
    )
    assert [s.heading for s in doc.sections] == [None, "Guide", "Guide > Install", "Guide > Usage"]
    usage = doc.sections[-1]
    assert doc.text[usage.start : usage.end] == "Usage\n\nCall it."


def test_the_title_falls_back_to_the_first_h1_then_the_file_name() -> None:
    assert extract("a.htm", b"<h2>Sub</h2><h1>Main</h1><p>x</p>").title == "Main"
    assert extract("page.html", b"<p>No headings.</p>").title == "page"


def test_lists_tables_and_preformatted_text_keep_their_layout() -> None:
    html = b"""
    <p>Steps:</p>
    <ol><li>Build</li><li>Test<ul><li>unit</li><li>integration</li></ul></li><li>Ship</li></ol>
    <table><tr><th>Name</th><th>Value</th></tr><tr><td>retries</td><td></td><td>3</td></tr></table>
    <pre>
def f():
    return 1
</pre>
    <p>Line one<br>line two</p><hr><p>After</p>
    """
    assert extract("a.html", html).text == (
        "Steps:\n\n1. Build\n2. Test\n- unit\n- integration\n3. Ship\n\n"
        "Name | Value\nretries | 3\n\n"
        "def f():\n    return 1\n\n"
        "Line one\nline two\n\nAfter"
    )


def test_non_content_and_hidden_elements_are_dropped() -> None:
    html = b"""<body>
    <p>Visible</p>
    <noscript>Enable JavaScript</noscript>
    <template><p>Template</p></template>
    <svg><text>Chart label</text></svg>
    <div hidden><div>Hidden <div>nested</div></div> still hidden</div>
    <iframe>Frame fallback</iframe>
    <!-- a comment -->
    <p>Also visible</p>
    </body>"""
    assert extract("a.html", html).text == "Visible\n\nAlso visible"


def test_inline_whitespace_is_collapsed_without_gluing_words() -> None:
    html = b"<p> Hello <b>bold</b>world, <i>it</i><b>alic</b>.\n</p><p>\n\n</p><p>Next</p>"
    assert extract("a.html", html).text == "Hello boldworld, italic.\n\nNext"


@pytest.mark.parametrize(
    ("html", "text"),
    [
        (b"<html><head><title>T</title><p>Body without a body tag", "Body without a body tag"),
        (b"<head><title>T</title>Bare text ends the head", "Bare text ends the head"),
        (
            b'<script src="app.js"/><p>Visible after a self-closed script</p>',
            "Visible after a self-closed script",
        ),
        (b"<p>Unclosed <b>tags <i>everywhere", "Unclosed tags everywhere"),
        (b"<h2>One<h3>Two</h3><p>Body</p>", "One\n\nTwo\n\nBody"),
    ],
)
def test_malformed_markup_is_read_as_browsers_would(html: bytes, text: str) -> None:
    assert extract("a.html", html).text == text


def test_a_heading_left_open_is_closed_by_the_next_one() -> None:
    doc = extract("a.html", b"<h2>One<h3>Two</h3><p>Body</p>")
    assert [s.heading for s in doc.sections] == ["One > Two"]


@pytest.mark.parametrize(
    ("data", "expected"),
    [
        ("<p>café</p>".encode(), "café"),
        (b"\xef\xbb\xbf<p>bom</p>", "bom"),
        ("<p>utf-16 ✓</p>".encode("utf-16"), "utf-16 ✓"),
        (
            '<meta charset="windows-1252"><p>caf\xe9 \x93quoted\x94</p>'.encode("latin-1"),
            "café “quoted”",
        ),
        (
            b'<meta http-equiv="Content-Type" content="text/html; charset=ISO-8859-1">'
            b"<p>na\xefve</p>",
            "naïve",
        ),
    ],
)
def test_encodings(data: bytes, expected: str) -> None:
    assert extract("a.html", data).text == expected


@pytest.mark.parametrize(
    ("data", "message"),
    [
        (b"<p>caf\xe9</p>", "must be UTF-8 or declare"),
        (b'<meta charset="no-such-codec"><p>caf\xe9</p>', "unknown HTML encoding"),
        (b'<meta charset="base64"><p>caf\xe9</p>', "unknown HTML encoding"),
        (b'<meta charset="utf-8"><p>caf\xe9</p>', "not valid utf-8"),
    ],
)
def test_undecodable_html_is_rejected(data: bytes, message: str) -> None:
    with pytest.raises(ExtractionError, match=message):
        extract("a.html", data)


def test_the_upload_check_decodes_without_parsing() -> None:
    assert validate_upload("a.html", GUIDE) == "text/html"
    with pytest.raises(ExtractionError, match="no extractable text"):
        validate_upload("blank.html", b"  \n\t ")


def test_markup_without_text_has_no_sections() -> None:
    doc = extract("a.html", b"<html><head><title>Only a title</title></head><body></body></html>")
    assert (doc.text, doc.sections, doc.title) == ("", [], "Only a title")
