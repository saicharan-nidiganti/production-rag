"""Application settings, loaded from environment variables (prefix ``RAG_``) and ``.env``."""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import AliasChoices, Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

RetrievalMode = Literal["vector", "lexical", "hybrid"]
EmbeddingProvider = Literal["hashing", "fastembed"]
GeneratorProvider = Literal["extractive", "anthropic"]
RerankerProvider = Literal["none", "fastembed"]

# Scores are not comparable across models, so retrieval-level abstention thresholds are per
# model. Calibrated on the dev split of eval/golden_v2.jsonl so that no answerable question
# abstains, with a margin; the test split reports how they generalize (docs/evaluation.md).
# Recalibrate on your own data.
DEFAULT_ABSTAIN_THRESHOLDS: dict[str, float] = {"hashing": 0.05, "fastembed": 0.55}
DEFAULT_RERANK_THRESHOLDS: dict[str, float] = {"Xenova/ms-marco-MiniLM-L-6-v2": -8.4}


MIN_API_KEY_LENGTH = 16


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="RAG_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        # `RAG_API_KEY=` (as in .env.example) means "unset", not "an empty key".
        env_ignore_empty=True,
    )

    # --- Storage ---
    database_url: str = "sqlite:///data/rag.db"

    # --- Ingestion ---
    max_upload_bytes: int = Field(default=25 * 1024 * 1024, gt=0)
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
    reranker: RerankerProvider = Field(
        default="none", description="Cross-encoder reranking of retrieved candidates."
    )
    reranker_model: str = "Xenova/ms-marco-MiniLM-L-6-v2"
    rerank_candidates: int = Field(
        default=20, ge=1, description="Fused candidates the reranker scores before top_k."
    )
    min_rerank_score: float | None = Field(
        default=None,
        description=(
            "With a reranker, abstain when the best rerank score is below this value. "
            "Unset means the calibrated default for the reranker model."
        ),
    )

    # --- Generation ---
    generator: GeneratorProvider = "extractive"
    anthropic_api_key: SecretStr | None = Field(
        default=None,
        # Read here (from the environment or .env) and passed to the SDK, because the SDK
        # only sees the process environment, not values that come from .env.
        validation_alias=AliasChoices("RAG_ANTHROPIC_API_KEY", "ANTHROPIC_API_KEY"),
        description="Claude API key. Unset lets the SDK resolve credentials itself.",
    )
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

    @property
    def rerank_abstain_threshold(self) -> float | None:
        """None when no reranker runs, or when an uncalibrated model has no explicit value."""
        if self.reranker == "none":
            return None
        if self.min_rerank_score is not None:
            return self.min_rerank_score
        return DEFAULT_RERANK_THRESHOLDS.get(self.reranker_model)

    @field_validator("api_key")
    @classmethod
    def _check_api_key(cls, value: SecretStr | None) -> SecretStr | None:
        if value is not None and len(value.get_secret_value()) < MIN_API_KEY_LENGTH:
            raise ValueError(
                f"RAG_API_KEY must be at least {MIN_API_KEY_LENGTH} characters; generate one "
                'with: python -c "import secrets; print(secrets.token_urlsafe(32))"'
            )
        return value

    @model_validator(mode="after")
    def _check_chunking(self) -> Settings:
        if self.chunk_overlap >= self.chunk_size:
            raise ValueError("chunk_overlap must be smaller than chunk_size")
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
