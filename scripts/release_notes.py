"""A release's notes, for the GitHub release: its section of docs/release.md, followed by
the release checklist.

The section is the block quote under ``## vX.Y.Z release notes``; its first bold line is the
release's title.

    python scripts/release_notes.py 1.0.0 --checklist reports/checklist.md --title-file title
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

RELEASE_DOC = Path(__file__).resolve().parent.parent / "docs" / "release.md"


class NotesError(ValueError):
    pass


def notes_for(version: str, document: str) -> tuple[str, str]:
    """(title, body) of the version's notes in ``document``."""
    heading = f"## v{version} release notes"
    lines = document.splitlines()
    try:
        start = lines.index(heading) + 1
    except ValueError:
        raise NotesError(f"docs/release.md has no '{heading}' section") from None
    section: list[str] = []
    for line in lines[start:]:
        if line.startswith("## "):
            break
        section.append(line)
    quoted = [line for line in section if line.startswith(">")]
    if not quoted:
        raise NotesError(f"the '{heading}' section has no block quote of notes")
    body = "\n".join(re.sub(r"^> ?", "", line) for line in quoted).strip()
    match = re.search(r"\*\*(.+?)\*\*", body)
    if match is None:
        raise NotesError("the notes start with no bold title line")
    title = match.group(1).rstrip(".")
    body = body[match.end() :].lstrip(" .\n")
    return title, body


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("version")
    parser.add_argument("--checklist", help="the release checklist (checklist.md) to append")
    parser.add_argument("--title-file", help="also write the title to this file")
    parser.add_argument("--document", default=str(RELEASE_DOC))
    args = parser.parse_args(argv)
    try:
        title, body = notes_for(args.version, Path(args.document).read_text(encoding="utf-8"))
    except (OSError, NotesError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    if args.checklist:
        checklist = Path(args.checklist).read_text(encoding="utf-8")
        body += "\n\n" + re.sub(r"^# Release checklist", "## Release checklist", checklist)
    if args.title_file:
        Path(args.title_file).write_text(title, encoding="utf-8")
    print(body)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
