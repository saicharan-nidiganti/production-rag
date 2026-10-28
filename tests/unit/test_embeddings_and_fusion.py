import numpy as np

from production_rag.embeddings import HashingEmbedder, from_blob, to_blob
from production_rag.retrieval import build_fts_query, reciprocal_rank_fusion


def test_hashing_embedder_is_deterministic_and_normalised() -> None:
    a, b = HashingEmbedder(64), HashingEmbedder(64)
    texts = ["rollback the deployment", "canary traffic share"]
    np.testing.assert_array_equal(a.embed_documents(texts), b.embed_documents(texts))
    norms = np.linalg.norm(a.embed_documents(texts), axis=1)
    np.testing.assert_allclose(norms, 1.0, rtol=1e-6)
    assert a.model_id == "hashing-v1-d64"


def test_hashing_embedder_ranks_overlapping_text_higher() -> None:
    emb = HashingEmbedder()
    query = emb.embed_query("how do I roll back a deployment")
    related, unrelated = emb.embed_documents(
        ["To roll back a deployment run the undo command.", "Sourdough needs a starter."]
    )
    assert float(query @ related) > float(query @ unrelated)


def test_empty_inputs() -> None:
    emb = HashingEmbedder(32)
    assert emb.embed_documents([]).shape == (0, 32)
    assert np.all(emb.embed_query("") == 0)


def test_blob_round_trip() -> None:
    vector = HashingEmbedder(16).embed_query("hello world")
    np.testing.assert_array_equal(from_blob(to_blob(vector)), vector)


def test_rrf_combines_and_breaks_ties_deterministically() -> None:
    fused = reciprocal_rank_fusion([[1, 2, 3], [3, 1, 4]], k=60)
    ids = [i for i, _ in fused]
    assert ids[0] == 1  # ranks 1 and 2
    assert ids[1] == 3  # ranks 3 and 1
    assert set(ids) == {1, 2, 3, 4}
    assert dict(fused)[1] == 1 / 61 + 1 / 62
    # 2 (rank 2 in one list) beats 4 (rank 3 in one list)
    assert ids.index(2) < ids.index(4)


def test_rrf_with_empty_lists() -> None:
    assert reciprocal_rank_fusion([[], []]) == []
    assert reciprocal_rank_fusion([[7], []]) == [(7, 1 / 61)]


def test_fts_query_quotes_terms_and_neutralises_operators() -> None:
    assert build_fts_query('rollback NEAR("x") OR *drop* "quote') == (
        '"rollback" OR "near" OR "x" OR "drop" OR "quote"'
    )
    assert build_fts_query("the of and") == '"the" OR "of" OR "and"'  # stopwords kept if all stop
    assert build_fts_query("  ?!  ") is None
