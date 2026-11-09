"""DOCX extraction (FR-2): headings, titles, lists, tables, text boxes, and hostile files."""

from __future__ import annotations

import io
import zipfile
from pathlib import Path
from xml.sax.saxutils import escape

import pytest

from production_rag.parsing import docx_text
from production_rag.parsing.extract import (
    ExtractionError,
    UnsupportedDocumentError,
    discover_documents,
    extract,
    validate_upload,
)

W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
MC = "http://schemas.openxmlformats.org/markup-compatibility/2006"
DOCUMENT = "application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"
MACRO_DOCUMENT = "application/vnd.ms-word.document.macroEnabled.main+xml"

STYLES = f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:styles xmlns:w="{W}">
  <w:style w:type="paragraph" w:styleId="Normal"><w:name w:val="Normal"/></w:style>
  <w:style w:type="paragraph" w:styleId="Heading1"><w:name w:val="heading 1"/>
    <w:basedOn w:val="Normal"/><w:pPr><w:outlineLvl w:val="0"/></w:pPr></w:style>
  <w:style w:type="paragraph" w:styleId="Heading2"><w:name w:val="heading 2"/>
    <w:basedOn w:val="Normal"/><w:pPr><w:outlineLvl w:val="1"/></w:pPr></w:style>
  <w:style w:type="paragraph" w:styleId="Title"><w:name w:val="Title"/></w:style>
  <w:style w:type="paragraph" w:styleId="Step"><w:name w:val="Step"/>
    <w:basedOn w:val="Heading2"/></w:style>
  <w:style w:type="paragraph" w:styleId="Kop1"><w:name w:val="heading 1"/></w:style>
  <w:style w:type="character" w:styleId="Heading1Char"><w:name w:val="Heading 1 Char"/></w:style>
</w:styles>"""


def p(
    text: str, style: str | None = None, *, listed: bool = False, outline: int | None = None
) -> str:
    """A paragraph of one run."""
    properties = []
    if style:
        properties.append(f'<w:pStyle w:val="{style}"/>')
    if listed:
        properties.append('<w:numPr><w:ilvl w:val="0"/><w:numId w:val="1"/></w:numPr>')
    if outline is not None:
        properties.append(f'<w:outlineLvl w:val="{outline}"/>')
    ppr = f"<w:pPr>{''.join(properties)}</w:pPr>" if properties else ""
    return f'<w:p>{ppr}<w:r><w:t xml:space="preserve">{escape(text)}</w:t></w:r></w:p>'


def table(*rows: list[str]) -> str:
    cells = ("".join(f"<w:tc>{cell}</w:tc>" for cell in row) for row in rows)
    return "<w:tbl>" + "".join(f"<w:tr>{row}</w:tr>" for row in cells) + "</w:tbl>"


def docx(
    body: str,
    *,
    styles: str | None = STYLES,
    title: str | None = None,
    main_type: str = DOCUMENT,
    document_xml: str | None = None,
    extra: dict[str, str] | None = None,
) -> bytes:
    """A minimal Word document, as Word lays out its package."""
    types = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="xml" ContentType="application/xml"/>'
        f'<Override PartName="/word/document.xml" ContentType="{main_type}"/>'
        "</Types>"
    )
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", types)
        archive.writestr(
            "word/document.xml",
            document_xml
            or f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            f'<w:document xmlns:w="{W}" xmlns:mc="{MC}"><w:body>{body}</w:body></w:document>',
        )
        if styles is not None:
            archive.writestr("word/styles.xml", styles)
        if title is not None:
            archive.writestr(
                "docProps/core.xml",
                '<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/'
                'metadata/core-properties" xmlns:dc="http://purl.org/dc/elements/1.1/">'
                f"<dc:title>{escape(title)}</dc:title></cp:coreProperties>",
            )
        for name, data in (extra or {}).items():
            archive.writestr(name, data)
    return buffer.getvalue()


GUIDE = docx(
    p("Preamble text.")
    + p("Guide", "Heading1")
    + p("Intro.")
    + p("Install", "Heading2")
    + p("Run the installer.")
    + p("Empty", "Heading2")
    + p("Usage", "Heading2")
    + p("Call it.")
)


def test_heading_styles_become_sections_with_heading_paths() -> None:
    doc = extract("guide.docx", GUIDE)
    assert doc.content_type == docx_text.CONTENT_TYPE
    assert doc.title == "Guide"
    assert doc.text == (
        "Preamble text.\n\nGuide\n\nIntro.\n\nInstall\n\nRun the installer.\n\n"
        "Empty\n\nUsage\n\nCall it."
    )
    assert [s.heading for s in doc.sections] == [None, "Guide", "Guide > Install", "Guide > Usage"]
    install = doc.sections[2]  # up to the next heading, which has no body and no section
    assert doc.text[install.start : install.end] == "Install\n\nRun the installer.\n\n"


def test_the_title_comes_from_properties_then_the_title_style_then_the_first_heading() -> None:
    body = p("Handbook", "Title") + p("Chapter", "Heading1") + p("Text.")
    assert extract("a.docx", docx(body, title="  Official   Title ")).title == "Official Title"
    assert extract("a.docx", docx(body)).title == "Handbook"
    assert extract("a.docx", docx(p("Chapter", "Heading1") + p("Text."))).title == "Chapter"
    assert extract("notes.docx", docx(p("Just text."))).title == "notes"


def test_heading_levels_come_from_styles_their_bases_names_and_outline_levels() -> None:
    body = (
        p("Top", "Kop1")  # localized style id, built-in name "heading 1"
        + p("Body.")
        + p("Derived", "Step")  # based on Heading2
        + p("Body.")
        + p("Direct", outline=1)  # outline level set on the paragraph itself
        + p("Body.")
        + p("Demoted", "Heading1", outline=9)  # 9 is body text, overriding the style
        + p("Body.")
    )
    doc = extract("a.docx", docx(body))
    assert [s.heading for s in doc.sections] == ["Top", "Top > Derived", "Top > Direct"]
    assert "Demoted" in doc.text


def test_heading_style_ids_work_without_a_styles_part() -> None:
    doc = extract("a.docx", docx(p("Guide", "Heading1") + p("Body."), styles=None))
    assert [s.heading for s in doc.sections] == ["Guide"]


def test_runs_tabs_line_breaks_and_hyphens() -> None:
    body = (
        "<w:p><w:r><w:t>Hello</w:t></w:r><w:r><w:tab/><w:t>world</w:t><w:br/>"
        "<w:t>e</w:t><w:noBreakHyphen/><w:t>mail</w:t></w:r>"
        '<w:hyperlink><w:r><w:t xml:space="preserve">  linked   text </w:t></w:r>'
        "</w:hyperlink></w:p>"
    )
    assert extract("a.docx", docx(body)).text == "Hello world\ne-mail linked text"


def test_list_items_get_markers_on_their_own_lines() -> None:
    body = p("Steps:") + "".join(p(s, listed=True) for s in ("one", "two", "three")) + p("Done.")
    assert extract("a.docx", docx(body)).text == "Steps:\n\n- one\n- two\n- three\n\nDone."


def test_tables_become_rows_of_cells() -> None:
    nested = table([p("inner a"), p("inner b")])
    body = (
        p("Before")
        + table(
            [p("Name"), p("Value")],
            [p("retries"), p("3") + p("(max)")],
            [p("empty"), ""],
            [p("nested"), nested],
        )
        + p("After")
    )
    assert extract("a.docx", docx(body)).text == (
        "Before\n\nName | Value\nretries | 3 (max)\nempty\nnested | inner a | inner b\n\nAfter"
    )


def test_tracked_deletions_and_field_codes_are_dropped() -> None:
    body = (
        "<w:p><w:r><w:t>Kept </w:t></w:r>"
        "<w:del><w:r><w:delText>deleted </w:delText></w:r></w:del>"
        "<w:ins><w:r><w:t>inserted </w:t></w:r></w:ins>"
        '<w:r><w:fldChar w:fldCharType="begin"/></w:r>'
        "<w:r><w:instrText> PAGE </w:instrText></w:r>"
        '<w:r><w:fldChar w:fldCharType="separate"/></w:r><w:r><w:t>7</w:t></w:r>'
        '<w:r><w:fldChar w:fldCharType="end"/></w:r></w:p>'
    )
    assert extract("a.docx", docx(body)).text == "Kept inserted 7"


def test_text_box_text_appears_once() -> None:
    """Word writes a text box twice: for current readers, and as a fallback for old ones."""
    box = p("Boxed note.")
    body = (
        "<w:p><w:r><w:t>Outer.</w:t></w:r><w:r><mc:AlternateContent>"
        f"<mc:Choice><w:drawing><w:txbxContent>{box}</w:txbxContent></w:drawing></mc:Choice>"
        f"<mc:Fallback><w:pict><w:txbxContent>{box}</w:txbxContent></w:pict></mc:Fallback>"
        "</mc:AlternateContent></w:r></w:p>"
    )
    text = extract("a.docx", docx(body)).text
    assert text.count("Boxed note.") == 1 and "Outer." in text


def test_an_empty_document_has_no_sections() -> None:
    doc = extract("empty.docx", docx(p("") + p("   ")))
    assert (doc.text, doc.sections) == ("", [])


def test_a_valid_document_passes_the_upload_check() -> None:
    assert validate_upload("guide.docx", GUIDE) == docx_text.CONTENT_TYPE


@pytest.mark.parametrize(
    ("data", "message"),
    [
        (b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 64, "legacy .doc"),
        (b"plain text renamed", "not a Word document"),
        (b"", "not a Word document"),
    ],
)
def test_files_that_are_not_docx_are_refused_at_upload(data: bytes, message: str) -> None:
    with pytest.raises(UnsupportedDocumentError, match=message):
        validate_upload("renamed.docx", data)
    with pytest.raises(UnsupportedDocumentError, match=message):
        extract("renamed.docx", data)


def test_a_zip_that_is_not_a_word_document_is_refused() -> None:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("notes.txt", "hello")
    with pytest.raises(UnsupportedDocumentError, match="not a Word document"):
        extract("archive.docx", buffer.getvalue())


def test_macro_enabled_documents_are_refused() -> None:
    with pytest.raises(UnsupportedDocumentError, match="macro-enabled"):
        extract("renamed.docx", docx(p("Text."), main_type=MACRO_DOCUMENT))


@pytest.mark.parametrize("cut", [0.5, 0.9])
def test_a_damaged_archive_is_an_extraction_error(cut: float) -> None:
    with pytest.raises(ExtractionError, match="damaged"):
        extract("broken.docx", GUIDE[: int(len(GUIDE) * cut)])


def test_malformed_xml_is_an_extraction_error() -> None:
    broken = f'<w:document xmlns:w="{W}"><w:body><w:p><w:r><w:t>unclosed'
    with pytest.raises(ExtractionError, match="malformed XML"):
        extract("broken.docx", docx("", document_xml=broken))


def test_document_type_declarations_are_refused() -> None:
    """Entity expansion ("billion laughs") needs a DTD; Word never writes one."""
    laughs = (
        '<?xml version="1.0"?><!DOCTYPE w:document [<!ENTITY a "ha"><!ENTITY b "&a;&a;&a;">]>'
        f'<w:document xmlns:w="{W}"><w:body><w:p><w:r><w:t>&b;</w:t></w:r></w:p></w:body>'
        "</w:document>"
    )
    with pytest.raises(ExtractionError, match="document type declaration"):
        extract("bomb.docx", docx("", document_xml=laughs))


def test_parts_over_the_size_limit_are_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(docx_text, "MAX_DOCUMENT_PART_BYTES", 2048)
    with pytest.raises(ExtractionError, match="limit"):
        extract("big.docx", docx(p("x" * 4096)))  # compresses well: small file, large part


def test_archives_with_too_many_parts_are_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(docx_text, "MAX_ENTRIES", 5)
    padding = {f"word/media/pad{i}.xml": "<x/>" for i in range(10)}
    with pytest.raises(ExtractionError, match="parts"):
        extract("many.docx", docx(p("Text."), extra=padding))


def test_discover_documents_finds_docx_and_html(tmp_path: Path) -> None:
    for name in ("a.docx", "b.html", "c.htm", "d.doc"):
        (tmp_path / name).write_bytes(b"x")
    assert [p.name for p in discover_documents(tmp_path)] == ["a.docx", "b.html", "c.htm"]
