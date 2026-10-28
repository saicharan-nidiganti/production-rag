"""Application settings, loaded from environment variables (prefix ``RAG_``) and ``.env``."""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

RetrievalMode = Literal["vector", "lexical", "hybrid"]
EmbeddingProvider = Literal["hashing", "fastembed"]
GeneratorProvider = Literal["extractive", "anthropic"]

# Cosine scores are not comparable across embedding models, so the retrieval-level abstention
# threshold is per provider. Calibrated on eval/golden_v1.jsonl so that no answerable question
# abstains, with a margin (docs/evaluation.md). Recalibrate on your own data.
DEFAULT_ABSTAIN_THRESHOLDS: dict[str, float] = {"hashing": 0.05, "fastembed": 0.55}


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="RAG_", env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    # --- Storage ---
    database_url: str = "sqlite:///data/rag.db"

    # --- Ingestion ---
    max_upload_bytes: int = Field(default=10 * 1024 * 1024, gt=0)
    max_pdf_pages: int = Field(default=500, gt=0)
    chunk_size: int = Field(default=1000, ge=100, description="Target chunk size in characters.")
    chunk_overlap: int = Field(default=150, ge=0)

    # --- Embeddings ---
    embedding_provider: EmbeddingProvider = "hashing"
    embedding_dimensions: int = Field(default=512, ge=32, description="Hashing embedder only.")
    fastembed_model: str = "BAAI/bge-small-en-v1.5"
    fastembed_cache_dir: str | None = Field(
        default=None, description="Where fastembed stores downloaded models (default: temp dir)."
    )

    # --- Retrieval ---
    retrieval_mode: RetrievalMode = "hybrid"
    top_k: int = Field(default=5, ge=1, le=50)
    candidate_pool: int = Field(
        default=30, ge=1, description="Candidates fetched per retriever before fusion."
    )
    rrf_k: int = Field(default=60, ge=1)
    min_vector_score: float | None = Field(
        default=None,
        ge=-1.0,
        le=1.0,
        description=(
            "Abstain when the best cosine similarity is below this value. "
            "Unset means the calibrated default for the embedding provider."
        ),
    )

    # --- Generation ---
    generator: GeneratorProvider = "extractive"
    anthropic_model: str = "claude-opus-5-5"
    anthropic_effort: Literal["low", "medium", "high", "xhigh", "max"] = "medium"
    anthropic_max_tokens: int = Field(default=16000, ge=256)
    anthropic_timeout_s: float = Field(default=120.0, gt=0)
    anthropic_max_retries: int = Field(default=2, ge=0)
    anthropic_fallbacks: bool = Field(
        default=True, description="Server-side refusal fallback (beta) on the Claude API."
    )

    # --- API ---
    api_key: SecretStr | None = Field(
        default=None, description="When set, every non-health endpoint requires this key."
    )
    log_level: str = "INFO"

    @property
    def abstain_threshold(self) -> float:
        if self.min_vector_score is not None:
            return self.min_vector_score
        return DEFAULT_ABSTAIN_THRESHOLDS[self.embedding_provider]

    @model_validator(mode="after")
    def _check_chunking(self) -> Settings:
        if self.chunk_overlap >= self.chunk_size:
            raise ValueError("chunk_overlap must be smaller than chunk_size")
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
