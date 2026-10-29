import random
from itertools import pairwise

import pytest

from production_rag.ingestion.chunking import chunk_document
from production_rag.parsing.extract import Section


def _chunks(text: str, size: int = 200, overlap: int = 40, sections: list[Section] | None = None):
    return chunk_document(
        text, sections or [Section(0, len(text))], chunk_size=size, overlap=overlap
    )


def test_offsets_point_at_the_chunk_text() -> None:
    text = " ".join(f"Sentence number {i} talks about topic {i % 7}." for i in range(80))
    chunks = _chunks(text)
    assert len(chunks) > 3
    for chunk in chunks:
        assert text[chunk.start : chunk.end] == chunk.text
        assert len(chunk.text) <= 200
        assert chunk.text == chunk.text.strip()


def test_consecutive_chunks_overlap_and_start_on_word_boundaries() -> None:
    text = " ".join(f"word{i}" for i in range(400))
    chunks = _chunks(text, size=200, overlap=40)
    for prev, nxt in pairwise(chunks):
        assert nxt.start < prev.end, "expected overlap between consecutive chunks"
        assert text[nxt.start - 1].isspace()


def test_prefers_paragraph_then_sentence_breaks() -> None:
    para = "Alpha beta gamma delta. " * 5  # ~120 chars
    text = para.strip() + "\n\n" + para.strip()
    chunks = _chunks(text, size=200, overlap=0)
    assert chunks[0].text.endswith("delta.")
    assert "\n\n" not in chunks[0].text


def test_chunks_never_cross_sections() -> None:
    text = "First section text here.\nSecond section text here."
    split = text.index("Second")
    sections = [Section(0, split, heading="A"), Section(split, len(text), heading="B")]
    chunks = _chunks(text, size=500, overlap=0, sections=sections)
    assert [c.heading for c in chunks] == ["A", "B"]
    assert chunks[0].end <= split <= chunks[1].start


def test_unbreakable_text_is_hard_cut_and_fully_covered() -> None:
    text = "x" * 1050
    chunks = _chunks(text, size=200, overlap=50)
    assert chunks[0].start == 0
    assert chunks[-1].end == len(text)
    assert all(len(c.text) <= 200 for c in chunks)


def test_whitespace_only_section_yields_nothing() -> None:
    assert _chunks("   \n\n  \t ") == []


def test_overlap_must_be_smaller_than_size() -> None:
    with pytest.raises(ValueError, match="overlap"):
        chunk_document("abc", [Section(0, 3)], chunk_size=100, overlap=100)


@pytest.mark.parametrize("seed", range(20))
def test_every_non_space_character_is_covered(seed: int) -> None:
    rng = random.Random(seed)
    words = ["lorem", "ipsum", "dolor.", "sit", "amet\n\n", "consectetur!", "a", "elit?"]
    text = " ".join(rng.choice(words) for _ in range(rng.randint(1, 600)))
    size = rng.randint(100, 400)
    overlap = rng.randint(0, size // 2)
    chunks = _chunks(text, size=size, overlap=overlap)
    covered = set()
    for chunk in chunks:
        assert len(chunk.text) <= size
        covered.update(range(chunk.start, chunk.end))
    missing = [i for i, ch in enumerate(text) if not ch.isspace() and i not in covered]
    assert missing == []


def test_chunking_is_deterministic() -> None:
    text = "\n\n".join(f"Paragraph {i}. " + "Some words follow here. " * 12 for i in range(30))
    sections = [Section(0, len(text))]
    first = chunk_document(text, sections, chunk_size=300, overlap=60)
    second = chunk_document(text, sections, chunk_size=300, overlap=60)
    assert len(first) > 5 and first == second
