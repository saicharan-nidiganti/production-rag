"""Exact (brute-force) cosine search over chunk embeddings held in memory.

The matrix is reloaded only when ``index_state.generation`` changes. Triggers bump the
counter on every chunk write, so the cache stays correct even when another process
ingests documents. Exact search is O(n * d) per query; measured on a laptop CPU it takes
~0.5 ms for 10k chunks and ~6 ms for 100k chunks (d=384, 154 MB of vectors).
docs/decisions/0002-storage.md records when to move to an ANN index.
"""

from __future__ import annotations

import threading

import numpy as np
import numpy.typing as npt
from sqlalchemy import select
from sqlalchemy.orm import Session

from production_rag.db.models import Chunk, IndexState
from production_rag.embeddings import Vector, from_blob


class VectorIndex:
    def __init__(self, model_id: str) -> None:
        self.model_id = model_id
        self._lock = threading.Lock()
        self._generation: int | None = None
        self._ids: npt.NDArray[np.int64] = np.zeros(0, dtype=np.int64)
        self._matrix: Vector = np.zeros((0, 0), dtype=np.float32)

    def _refresh(self, session: Session) -> tuple[npt.NDArray[np.int64], Vector]:
        generation = session.scalar(select(IndexState.generation).where(IndexState.id == 1))
        with self._lock:
            if generation != self._generation:
                rows = session.execute(
                    select(Chunk.id, Chunk.embedding)
                    .where(Chunk.embedding_model == self.model_id)
                    .order_by(Chunk.id)
                ).all()
                self._ids = np.array([r.id for r in rows], dtype=np.int64)
                self._matrix = (
                    np.stack([from_blob(r.embedding) for r in rows])
                    if rows
                    else np.zeros((0, 0), dtype=np.float32)
                )
                self._generation = generation
            return self._ids, self._matrix

    def search(self, session: Session, query: Vector, k: int) -> list[tuple[int, float]]:
        ids, matrix = self._refresh(session)
        if len(ids) == 0:
            return []
        scores = matrix @ query
        k = min(k, len(ids))
        top = np.argpartition(-scores, k - 1)[:k]
        top = top[np.argsort(-scores[top], kind="stable")]
        return [(int(ids[i]), float(scores[i])) for i in top]
