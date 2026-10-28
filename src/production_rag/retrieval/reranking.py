"""Cross-encoder reranking.

A cross-encoder reads the question and a passage together and scores how well the passage
answers the question. That is a better relevance signal than bi-encoder cosine similarity,
both for ordering candidates and for deciding that nothing retrieved is relevant enough to
answer from (docs/decisions/0005-reranking-and-abstention.md).
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from production_rag.config import Settings


class Reranker(Protocol):
    @property
    def model_id(self) -> str: ...

    def score(self, query: str, passages: Sequence[str]) -> list[float]:
        """One relevance score per passage; higher is more relevant."""
        ...


class FastEmbedReranker:  # pragma: no cover - exercised by live tests only (downloads a model)
    """Local ONNX cross-encoder via ``fastembed``."""

    def __init__(self, model_name: str, cache_dir: str | None = None) -> None:
        try:
            from fastembed.rerank.cross_encoder import TextCrossEncoder
        except ImportError as exc:
            raise RuntimeError(
                "fastembed is not installed; install the 'local-embeddings' extra"
            ) from exc
        self._model_name = model_name
        self._model = TextCrossEncoder(model_name=model_name, cache_dir=cache_dir)

    @property
    def model_id(self) -> str:
        return f"fastembed:{self._model_name}"

    def score(self, query: str, passages: Sequence[str]) -> list[float]:
        if not passages:
            return []
        return [float(s) for s in self._model.rerank(query, list(passages))]


def build_reranker(settings: Settings) -> Reranker | None:
    if settings.reranker == "fastembed":
        return FastEmbedReranker(settings.reranker_model, settings.fastembed_cache_dir)
    return None
