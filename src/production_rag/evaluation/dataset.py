"""Golden dataset format.

One JSON object per line::

    {"id": "q001",
     "split": "test",
     "tags": ["lookup"],
     "question": "How long does a canary run before full rollout?",
     "relevant": [{"source": "deployment-runbook.md", "heading": "Canary releases"}],
     "must_include": ["30 minutes"],
     "must_not_include": ["ACCESS GRANTED"]}

Relevance is labelled at the level of document + section heading rather than chunk ids,
so labels stay valid when chunking parameters change. A case with an empty ``relevant``
list is *unanswerable* from the corpus: the system should abstain.

``split`` separates cases used to calibrate settings (``dev``) from held-out cases used
only to report results (``test``). ``tags`` group cases for per-kind results.
``must_not_include`` lists strings an answer must never contain, such as the payload of a
prompt injection planted in the corpus. All three are optional.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from production_rag.retrieval import RetrievedChunk

SPLITS = ("dev", "test")


@dataclass(frozen=True)
class RelevantSource:
    source: str
    heading: str | None = None

    def matches(self, chunk: RetrievedChunk) -> bool:
        if chunk.source_name != self.source:
            return False
        if self.heading is None:
            return True
        return chunk.heading is not None and self.heading in chunk.heading.split(" > ")


@dataclass(frozen=True)
class EvalCase:
    id: str
    question: str
    relevant: list[RelevantSource]
    must_include: list[str] = field(default_factory=list)
    must_not_include: list[str] = field(default_factory=list)
    split: str | None = None
    tags: list[str] = field(default_factory=list)

    @property
    def answerable(self) -> bool:
        return bool(self.relevant)


class DatasetError(ValueError):
    pass


def load_dataset(path: Path) -> list[EvalCase]:
    cases: list[EvalCase] = []
    seen: set[str] = set()
    for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            raw = json.loads(line)
            case = EvalCase(
                id=str(raw["id"]),
                question=str(raw["question"]),
                relevant=[RelevantSource(**r) for r in raw.get("relevant", [])],
                must_include=[str(s) for s in raw.get("must_include", [])],
                must_not_include=[str(s) for s in raw.get("must_not_include", [])],
                split=raw.get("split"),
                tags=[str(t) for t in raw.get("tags", [])],
            )
        except (json.JSONDecodeError, KeyError, TypeError) as exc:
            raise DatasetError(f"{path}:{lineno}: invalid case: {exc}") from exc
        if case.split is not None and case.split not in SPLITS:
            raise DatasetError(f"{path}:{lineno}: split must be one of {SPLITS}")
        if case.id in seen:
            raise DatasetError(f"{path}:{lineno}: duplicate case id {case.id!r}")
        seen.add(case.id)
        cases.append(case)
    if not cases:
        raise DatasetError(f"{path}: dataset is empty")
    return cases
