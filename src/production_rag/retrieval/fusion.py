"""Reciprocal Rank Fusion (Cormack, Clarke & Buettcher, 2009)."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence


def reciprocal_rank_fusion(
    rankings: Sequence[Sequence[int]], k: int = 60
) -> list[tuple[int, float]]:
    """Fuse ranked id lists; score(d) = sum over lists of 1 / (k + rank(d)), rank from 1.

    RRF uses ranks only, so it needs no calibration between BM25 and cosine scores. Ties
    break by first appearance, which makes the output deterministic.
    """
    scores: dict[int, float] = defaultdict(float)
    first_seen: dict[int, int] = {}
    for ranking in rankings:
        for rank, item in enumerate(ranking, start=1):
            scores[item] += 1.0 / (k + rank)
            first_seen.setdefault(item, len(first_seen))
    return sorted(scores.items(), key=lambda kv: (-kv[1], first_seen[kv[0]]))
