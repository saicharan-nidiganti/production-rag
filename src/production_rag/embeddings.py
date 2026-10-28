"""Embedding providers.

Every provider returns L2-normalised float32 vectors, so cosine similarity is a dot product.
``model_id`` is stored with each chunk; vectors from different models are never compared.
"""

from __future__ import annotations

import hashlib
import math
from collections import Counter
from collections.abc import Sequence
from itertools import pairwise
from typing import TYPE_CHECKING, Protocol

import numpy as np
import numpy.typing as npt

from production_rag.text import content_tokens

if TYPE_CHECKING:
    from production_rag.config import Settings

Vector = npt.NDArray[np.float32]


class Embedder(Protocol):
    @property
    def model_id(self) -> str: ...

    def embed_documents(self, texts: Sequence[str]) -> Vector:
        """Return an ``(len(texts), dimensions)`` matrix."""
        ...

    def embed_query(self, text: str) -> Vector:
        """Return a ``(dimensions,)`` vector."""
        ...


def _normalize_rows(matrix: Vector) -> Vector:
    norms = np.linalg.norm(matrix, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    return (matrix / norms).astype(np.float32)


class HashingEmbedder:
    """Deterministic bag-of-words embedder using signed feature hashing.

    It needs no model download and gives byte-identical results on every machine, which
    makes it the default for tests, CI and offline development. It captures lexical
    overlap only (unigrams and bigrams), not meaning; use ``fastembed`` for semantic
    retrieval and compare the two with ``rag eval``.
    """

    def __init__(self, dimensions: int = 512) -> None:
        self.dimensions = dimensions

    @property
    def model_id(self) -> str:
        return f"hashing-v1-d{self.dimensions}"

    def _features(self, text: str) -> Counter[str]:
        tokens = content_tokens(text)
        features = Counter(tokens)
        features.update(f"{a} {b}" for a, b in pairwise(tokens))
        return features

    def _embed_one(self, text: str) -> Vector:
        vector = np.zeros(self.dimensions, dtype=np.float32)
        for feature, count in self._features(text).items():
            digest = hashlib.blake2b(feature.encode(), digest_size=8).digest()
            value = int.from_bytes(digest, "little")
            sign = 1.0 if value & 1 else -1.0
            vector[(value >> 1) % self.dimensions] += sign * (1.0 + math.log(count))
        return vector

    def embed_documents(self, texts: Sequence[str]) -> Vector:
        if not texts:
            return np.zeros((0, self.dimensions), dtype=np.float32)
        return _normalize_rows(np.stack([self._embed_one(t) for t in texts]))

    def embed_query(self, text: str) -> Vector:
        vector: Vector = self.embed_documents([text])[0]
        return vector


class FastEmbedEmbedder:  # pragma: no cover - exercised by live tests only (downloads a model)
    """Semantic embeddings from a local ONNX model via ``fastembed``."""

    def __init__(self, model_name: str, cache_dir: str | None = None) -> None:
        try:
            from fastembed import TextEmbedding
        except ImportError as exc:
            raise RuntimeError(
                "fastembed is not installed; install the 'local-embeddings' extra"
            ) from exc
        self._model_name = model_name
        self._model = TextEmbedding(model_name=model_name, cache_dir=cache_dir)

    @property
    def model_id(self) -> str:
        return f"fastembed:{self._model_name}"

    def embed_documents(self, texts: Sequence[str]) -> Vector:
        if not texts:
            return np.zeros((0, self._model.embedding_size), dtype=np.float32)
        return _normalize_rows(np.stack(list(self._model.passage_embed(list(texts)))))

    def embed_query(self, text: str) -> Vector:
        vector: Vector = _normalize_rows(np.stack(list(self._model.query_embed(text))))[0]
        return vector


def build_embedder(settings: Settings) -> Embedder:
    if settings.embedding_provider == "fastembed":
        return FastEmbedEmbedder(settings.fastembed_model, settings.fastembed_cache_dir)
    return HashingEmbedder(settings.embedding_dimensions)


def to_blob(vector: Vector) -> bytes:
    return np.asarray(vector, dtype="<f4").tobytes()


def from_blob(blob: bytes) -> Vector:
    return np.frombuffer(blob, dtype="<f4")
