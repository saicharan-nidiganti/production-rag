"""Request and response models for the HTTP API."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, cast

from pydantic import BaseModel, ConfigDict, Field

from production_rag.config import RetrievalMode
from production_rag.db.models import Document, DocumentStatus
from production_rag.generation import AnswerStatus
from production_rag.rag import QueryResult
from production_rag.retrieval import RetrievedChunk, SearchResult


def _utc(value: datetime) -> datetime:
    """SQLite returns timestamps without a zone; they are stored in UTC."""
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


class DocumentError(BaseModel):
    code: str = Field(
        description="extraction_failed, unsupported_document_type, interrupted, "
        "upload_missing or internal_error."
    )
    message: str = Field(description="What went wrong and what to do about it.")


class DocumentOut(BaseModel):
    id: str
    source_name: str
    title: str = Field(description="The file name until the document is ready.")
    content_type: str
    size_bytes: int
    num_chunks: int = Field(description="0 until the document is ready.")
    content_hash: str
    metadata: dict[str, Any]
    status: DocumentStatus = Field(
        description="queued -> processing -> ready or failed. Only ready documents are searched."
    )
    error: DocumentError | None = Field(description="Why processing failed; null otherwise.")
    attempts: int = Field(description="Processing attempts since the document was last queued.")
    created_at: datetime
    updated_at: datetime = Field(description="When the status last changed.")

    @classmethod
    def from_model(cls, doc: Document) -> DocumentOut:
        error = None
        if doc.error_code is not None:
            error = DocumentError(code=doc.error_code, message=doc.error_message or "")
        return cls(
            id=doc.id,
            source_name=doc.source_name,
            title=doc.title,
            content_type=doc.content_type,
            size_bytes=doc.size_bytes,
            num_chunks=doc.num_chunks,
            content_hash=doc.content_hash,
            metadata=doc.metadata_,
            status=cast(DocumentStatus, doc.status),
            error=error,
            attempts=doc.attempts,
            created_at=_utc(doc.created_at),
            updated_at=_utc(doc.updated_at),
        )


class IngestResponse(BaseModel):
    document: DocumentOut
    created: bool = Field(description="False when identical content was already submitted.")


class DocumentList(BaseModel):
    items: list[DocumentOut]
    total: int
    limit: int
    offset: int


class SearchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: str = Field(min_length=1, max_length=2000)
    top_k: int | None = Field(default=None, ge=1, le=50)
    mode: RetrievalMode | None = None


class SearchHit(BaseModel):
    chunk_id: int = Field(description="Row id; changes if the document is re-ingested.")
    chunk_key: str = Field(description="Stable for identical content and chunk settings.")
    document_id: str
    document_title: str
    source_name: str
    heading: str | None
    page: int | None
    text: str
    start_char: int
    end_char: int
    score: float
    vector_score: float | None
    lexical_score: float | None
    rerank_score: float | None

    @classmethod
    def from_chunk(cls, chunk: RetrievedChunk) -> SearchHit:
        return cls(
            chunk_id=chunk.chunk_id,
            chunk_key=chunk.chunk_key,
            document_id=chunk.document_id,
            document_title=chunk.document_title,
            source_name=chunk.source_name,
            heading=chunk.heading,
            page=chunk.page,
            text=chunk.text,
            start_char=chunk.start_char,
            end_char=chunk.end_char,
            score=chunk.score,
            vector_score=chunk.vector_score,
            lexical_score=chunk.lexical_score,
            rerank_score=chunk.rerank_score,
        )


class SearchResponse(BaseModel):
    mode: RetrievalMode
    hits: list[SearchHit]
    best_vector_score: float | None
    best_rerank_score: float | None
    timings_ms: dict[str, float]

    @classmethod
    def from_result(cls, result: SearchResult, timings_ms: dict[str, float]) -> SearchResponse:
        return cls(
            mode=result.mode,
            hits=[SearchHit.from_chunk(c) for c in result.chunks],
            best_vector_score=result.best_vector_score,
            best_rerank_score=result.best_rerank_score,
            timings_ms=timings_ms,
        )


class QueryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    question: str = Field(min_length=1, max_length=2000)
    top_k: int | None = Field(default=None, ge=1, le=20)
    mode: RetrievalMode | None = None


class CitationOut(BaseModel):
    chunk_id: int
    chunk_key: str
    document_id: str
    document_title: str
    source_name: str
    heading: str | None
    page: int | None
    start_char: int
    end_char: int
    cited_text: str


class SegmentOut(BaseModel):
    text: str
    citations: list[int] = Field(description="Indexes into the response's `citations` list.")


class QueryResponse(BaseModel):
    status: AnswerStatus
    answer: str
    segments: list[SegmentOut]
    citations: list[CitationOut]
    retrieved: list[SearchHit]
    model: str
    stop_reason: str | None
    usage: dict[str, int] | None
    timings_ms: dict[str, float]

    @classmethod
    def from_result(cls, result: QueryResult) -> QueryResponse:
        answer = result.answer
        return cls(
            status=answer.status,
            answer=answer.text,
            segments=[SegmentOut(text=s.text, citations=s.citations) for s in answer.segments],
            citations=[
                CitationOut(
                    chunk_id=c.chunk.chunk_id,
                    chunk_key=c.chunk.chunk_key,
                    document_id=c.chunk.document_id,
                    document_title=c.chunk.document_title,
                    source_name=c.chunk.source_name,
                    heading=c.chunk.heading,
                    page=c.chunk.page,
                    start_char=c.chunk.start_char,
                    end_char=c.chunk.end_char,
                    cited_text=c.cited_text,
                )
                for c in answer.citations
            ],
            retrieved=[SearchHit.from_chunk(c) for c in result.retrieval.chunks],
            model=answer.model,
            stop_reason=answer.stop_reason,
            usage=answer.usage,
            timings_ms=result.timings_ms,
        )


class ErrorBody(BaseModel):
    code: str
    message: str
    request_id: str | None = None
    details: list[dict[str, Any]] | None = Field(
        default=None, description="Field-level problems; present on validation errors only."
    )


class ErrorResponse(BaseModel):
    error: ErrorBody


class ReadinessResponse(BaseModel):
    status: str
    checks: dict[str, Any]
