"""Request and response models for the HTTP API."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from production_rag.config import RetrievalMode
from production_rag.db.models import Document
from production_rag.generation import AnswerStatus
from production_rag.rag import QueryResult
from production_rag.retrieval import RetrievedChunk, SearchResult


class DocumentOut(BaseModel):
    id: str
    source_name: str
    title: str
    content_type: str
    size_bytes: int
    num_chunks: int
    content_hash: str
    metadata: dict[str, Any]
    created_at: datetime

    @classmethod
    def from_model(cls, doc: Document) -> DocumentOut:
        return cls(
            id=doc.id,
            source_name=doc.source_name,
            title=doc.title,
            content_type=doc.content_type,
            size_bytes=doc.size_bytes,
            num_chunks=doc.num_chunks,
            content_hash=doc.content_hash,
            metadata=doc.metadata_,
            created_at=doc.created_at,
        )


class IngestResponse(BaseModel):
    document: DocumentOut
    created: bool = Field(description="False when identical content was already ingested.")


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
    chunk_id: int
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
