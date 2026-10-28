import pytest

from production_rag.ingestion.extract import (
    ExtractionError,
    UnsupportedDocumentError,
    extract,
    safe_filename,
)


def _pdf(pages: list[str]) -> bytes:
    """Build a minimal, valid PDF with one line of Helvetica text per page."""
    objects: list[bytes] = []
    kids = " ".join(f"{3 + 2 * i} 0 R" for i in range(len(pages)))
    objects.append(b"<< /Type /Catalog /Pages 2 0 R >>")
    objects.append(f"<< /Type /Pages /Kids [{kids}] /Count {len(pages)} >>".encode())
    font_id = 3 + 2 * len(pages)
    for i, text in enumerate(pages):
        content_id = 4 + 2 * i
        objects.append(
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents {content_id} 0 R "
            f"/Resources << /Font << /F1 {font_id} 0 R >> >> >>".encode()
        )
        stream = f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode()
        objects.append(b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream")
    objects.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")

    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{number} 0 obj\n".encode() + body + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode()
    out += b"".join(f"{o:010d} 00000 n \n".encode() for o in offsets)
    out += (
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    )
    return bytes(out)


def test_markdown_sections_carry_heading_paths() -> None:
    md = (
        "Preamble text.\n\n"
        "# Guide\n\nIntro.\n\n"
        "## Install\n\nRun the installer.\n\n"
        "### Windows\n\nUse the MSI.\n\n"
        "## Empty\n"
        "## Usage\n\nCall it.\n"
    )
    doc = extract("guide.md", md.encode())
    assert doc.title == "Guide"
    assert doc.content_type == "text/markdown"
    headings = [s.heading for s in doc.sections]
    assert headings == [
        None,
        "Guide",
        "Guide > Install",
        "Guide > Install > Windows",
        "Guide > Usage",
    ]
    usage = doc.sections[-1]
    assert doc.text[usage.start : usage.end].startswith("## Usage")


def test_headings_inside_code_fences_are_ignored() -> None:
    md = "# Real\n\n```bash\n# not a heading\necho hi\n```\n"
    doc = extract("x.md", md.encode())
    assert [s.heading for s in doc.sections] == ["Real"]


def test_markdown_without_h1_uses_file_stem_as_title() -> None:
    assert extract("notes.md", b"## Sub\n\nBody").title == "notes"


def test_text_decoding_normalises_newlines_and_bom() -> None:
    doc = extract("a.txt", b"\xef\xbb\xbfline one\r\nline two\rline three\x00")
    assert doc.text == "line one\nline two\nline three"
    assert doc.title == "a"


def test_non_utf8_text_is_rejected() -> None:
    with pytest.raises(ExtractionError, match="UTF-8"):
        extract("a.txt", b"\xff\xfe\xfa")


def test_unsupported_type_is_rejected() -> None:
    with pytest.raises(UnsupportedDocumentError, match=r"\.exe"):
        extract("run.exe", b"MZ")


def test_pdf_pages_become_sections() -> None:
    doc = extract("report.pdf", _pdf(["Hello from page one", "Page two text"]))
    assert doc.content_type == "application/pdf"
    assert [s.page for s in doc.sections] == [1, 2]
    assert "Hello from page one" in doc.text
    second = doc.sections[1]
    assert "Page two text" in doc.text[second.start : second.end]
    assert doc.metadata == {"pages": 2}


def test_pdf_page_limit() -> None:
    with pytest.raises(ExtractionError, match="limit"):
        extract("big.pdf", _pdf(["a", "b", "c"]), max_pdf_pages=2)


def test_corrupt_pdf_is_an_extraction_error() -> None:
    with pytest.raises(ExtractionError):
        extract("bad.pdf", b"%PDF-1.4 this is not really a pdf")


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("../../etc/passwd.md", "passwd.md"),
        ("C:\\Users\\me\\notes.txt", "notes.txt"),
        ("bad\x00name\n.md", "badname.md"),
        ("", "untitled"),
    ],
)
def test_safe_filename(raw: str, expected: str) -> None:
    assert safe_filename(raw) == expected
