# ADR-0009: Extract DOCX and HTML with the standard library

- **Status:** Accepted, 2026-11-10. An engineering decision, taken to deliver FR-2 within
  the constraints of [ADR-0007](0007-isolated-document-parsing.md).
- **Date:** 2026-11-10
- **Deciders:** Engineering
- **Supersedes:** the DOCX and HTML part of [ADR-0001](0001-technology-stack.md) §4
  ("python-docx for DOCX, an HTML parser with sanitization for HTML")

## Context

FR-2 requires DOCX and HTML uploads. ADR-0001 named python-docx for DOCX. Since then,
ADR-0007 moved parsing into a separate process that imports only the standard library and
pypdf: it starts in about 40 ms, runs under an address-space limit on Linux and macOS, and
every library it imports is attack surface for hostile files. python-docx depends on lxml,
a large C extension; the common HTML choice, BeautifulSoup, is usually paired with lxml
too.

What the service needs from both formats is modest: the text a reader sees, the headings
(so sections carry a heading path, as for Markdown and citations can name the section), and
a readable shape for lists and tables. It doesn't need styling, layout or round-tripping.

## Decision drivers

1. Hostile files fail their own document and never the service (ADR-0007).
2. The parser process stays small and fast, and works under rlimits.
3. No new runtime dependency unless it earns its place (supply chain, licences, lockfile).
4. Sections and citations behave the same as for Markdown.
5. Existing Markdown, TXT and PDF results don't move (evaluation baselines, chunk keys).

## Options considered

| Option | Pros | Cons |
| --- | --- | --- |
| python-docx (+ lxml) for DOCX, BeautifulSoup (+ lxml) for HTML | Mature; richer object models | Two dependencies plus a large C extension in the parser process; slower start-up; more attack surface; python-docx reads the whole document into memory |
| **Standard library: `zipfile` + `xml.etree.ElementTree` (streamed with `iterparse`) for DOCX, `html.parser` for HTML** ✅ | No new dependency; fast start-up; streaming keeps memory to one paragraph or table; we choose exactly what is kept | We maintain the extraction rules ourselves; no CSS or layout model |
| defusedxml for the DOCX XML | Ready-made protection against XML attacks | A dependency for one check we can make directly: OOXML never contains a DTD |
| Convert with an external tool (LibreOffice, pandoc) | Broad format support | A large external binary and a subprocess per file; out of proportion for v0 |

## Decision

1. **DOCX** ([docx_text.py](../../src/production_rag/parsing/docx_text.py)):
   - At upload, only the signature is checked: a ZIP file. OLE files, which are legacy
     `.doc` and password-protected `.docx`, are refused with a message saying so.
   - In the parser process, `[Content_Types].xml` must declare a Word document;
     macro-enabled documents (`.docm`) are refused.
   - The main part is streamed with `iterparse`, and each element is dropped once read.
   - Headings come from paragraph styles (following each style's `basedOn` chain, by
     outline level or by the built-in name, so localized style ids work) or from an
     outline level set on the paragraph.
   - List items get a `-` marker on their own line; table rows become lines with cells
     joined by ` | `; text boxes are read once, skipping the fallback copy Word writes
     for old readers; deleted text of tracked changes and field codes are dropped.
2. **HTML** ([html_text.py](../../src/production_rag/parsing/html_text.py)):
   - Decoded from a byte-order mark, then as UTF-8, then with the encoding a `<meta>`
     declares in the first 1024 bytes; anything else is refused.
   - `h1` to `h6` are headings. The head (except the title), scripts, styles, `noscript`,
     templates, SVG, MathML, frames, objects and `hidden` elements are dropped. Lists,
     tables and `pre` keep their shape. Markup is read the way browsers read it: optional
     tags may be missing, and a heading left open is closed by the next one.
3. **Both** share the heading logic with Markdown
   ([structure.py](../../src/production_rag/parsing/structure.py)): one section per
   heading with a body, carrying the heading path. The title is the declared one
   (document properties or `<title>`, or the Title style in DOCX), else the first
   level-1 heading, else the file name.
4. **XML safety without defusedxml:** a DOCX part containing a document type or entity
   declaration is refused before it is parsed (Word never writes one, and every entity
   attack needs one); ElementTree never fetches external resources. Each part has an
   uncompressed size limit (64 MiB for the document, 8 MiB for the others, checked against
   the archive and while reading), and an archive may hold at most 10,000 entries.

Verified by tests:

- Markdown extraction is unchanged: identical output for all 32 Markdown files in the
  repository (275 sections) before and after the shared heading code, and the retrieval
  gate's numbers are unchanged.
- Per format: headings, titles, layout, dropped content, malformed markup and encodings.
- Hostile DOCX files are refused: legacy, encrypted, not a Word document, macro-enabled,
  damaged, malformed XML, a "billion laughs" DTD, oversized parts and too many entries.
- The isolated parser gives the same result as inline extraction for both formats.
- Ingestion and the HTTP API, end to end, cite DOCX and HTML chunks by heading.
- Each protection was checked by removing it: the tests fail without the part checks,
  without skipping the text box fallback, and without skipping scripts and styles.
- The extraction tests also pass on Python 3.12, whose HTML parser differs from 3.14's.

## Consequences

### Positive

- DOCX and HTML uploads work, with citations by heading, and no new dependency.
- Both run in the isolated parser process with its timeout, memory limit and stripped
  environment (ADR-0007).
- Memory for DOCX stays proportional to one paragraph or table, not to the document.

### Negative and risks

- Not extracted from DOCX: headers and footers, footnotes and endnotes, comments, images,
  equations and embedded objects. List numbering isn't reproduced (every item gets `-`).
- HTML text hidden only by CSS is kept, and text that a script would add is missing.
- The extraction rules are ours to maintain, and real-world documents will find cases the
  tests don't cover.
- Python's HTML parser has changed between versions (3.14 reads `title` as raw text, as
  browsers do), so unusual markup can extract slightly differently on 3.12 and 3.14.

## Revisit when

- Users need footnotes, headers or comments from DOCX, or tables with merged cells kept
  faithfully: consider python-docx, measured against this extractor on real documents.
- HTML from web pages (navigation, cookie banners) dilutes retrieval: add main-content
  detection, measured by the evaluation suite.
- Old `.doc`, `.odt` or RTF files are needed: consider a conversion step in a sandbox.
