"""HTTP routes. Handlers are sync (``def``) so FastAPI runs blocking DB and model calls in its
threadpool rather than on the event loop."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, File, Query, Request, Response, UploadFile, status
from fastapi.responses import JSONResponse
from sqlalchemy import text

from production_rag.api.errors import NotFoundError
from production_rag.api.schemas import (
    DocumentList,
    DocumentOut,
    ErrorResponse,
    IngestResponse,
    QueryRequest,
    QueryResponse,
    ReadinessResponse,
    SearchRequest,
    SearchResponse,
)
from production_rag.api.security import require_api_key
from production_rag.container import Container
from production_rag.db.migrate import current_revision, head_revision
from production_rag.db.models import DocumentStatus
from production_rag.ingestion import DocumentTooLargeError, IngestionWorker
from production_rag.observability import Timer


def get_container(request: Request) -> Container:
    container: Container = request.app.state.container
    return container


ContainerDep = Annotated[Container, Depends(get_container)]

health_router = APIRouter(tags=["health"])
api_router = APIRouter(prefix="/v1", dependencies=[Depends(require_api_key)])

_ERRORS: dict[int | str, dict[str, object]] = {
    code: {"model": ErrorResponse} for code in (401, 404, 409, 413, 415, 422, 502, 503)
}


@health_router.get("/health")
def health() -> dict[str, str]:
    """Liveness: the process is up. Checks no dependencies."""
    return {"status": "ok"}


@health_router.get("/ready", response_model=ReadinessResponse)
def ready(request: Request, container: ContainerDep) -> JSONResponse:
    """Readiness: the database is reachable and its schema is at the latest migration.

    Also reports documents by status and the live ingestion threads, for monitoring; neither
    affects readiness.
    """
    checks: dict[str, object] = {"embedding_model": container.embedder.model_id}
    ok = True
    try:
        with container.engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        current, head = current_revision(container.engine), head_revision()
        checks["schema_revision"] = current
        checks["expected_revision"] = head
        if current != head:
            ok = False
            checks["schema"] = "not at head; run `rag db upgrade`"
        else:
            checks.update(container.index_stats())
            worker: IngestionWorker | None = getattr(request.app.state, "ingest_worker", None)
            checks["ingestion"] = {
                **container.documents.status_counts(),
                "workers": worker.alive if worker else 0,
            }
    except Exception as exc:
        ok = False
        checks["database"] = f"unavailable: {type(exc).__name__}"
    body = ReadinessResponse(status="ready" if ok else "not_ready", checks=checks)
    return JSONResponse(status_code=200 if ok else 503, content=body.model_dump())


@api_router.post(
    "/documents",
    response_model=IngestResponse,
    status_code=status.HTTP_202_ACCEPTED,
    responses={
        200: {"model": IngestResponse, "description": "Identical content is already ready."},
        **_ERRORS,
    },
)
def upload_document(
    container: ContainerDep, response: Response, file: Annotated[UploadFile, File()]
) -> IngestResponse:
    """Queue a document for processing. Poll its `Location` until the status is final."""
    limit = container.settings.max_upload_bytes
    data = file.file.read(limit + 1)
    if len(data) > limit:
        raise DocumentTooLargeError(
            f"document exceeds the {limit}-byte limit (RAG_MAX_UPLOAD_BYTES)"
        )
    result = container.ingestion.submit(file.filename or "untitled.txt", data)
    if result.document.status == "ready":
        response.status_code = status.HTTP_200_OK
    response.headers["Location"] = f"/v1/documents/{result.document.id}"
    return IngestResponse(document=DocumentOut.from_model(result.document), created=result.created)


@api_router.get("/documents", response_model=DocumentList)
def list_documents(
    container: ContainerDep,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
    status_filter: Annotated[DocumentStatus | None, Query(alias="status")] = None,
) -> DocumentList:
    page = container.documents.list_documents(limit=limit, offset=offset, status=status_filter)
    items = [DocumentOut.from_model(d) for d in page.items]
    return DocumentList(items=items, total=page.total, limit=limit, offset=offset)


@api_router.get("/documents/{document_id}", response_model=DocumentOut, responses=_ERRORS)
def get_document(container: ContainerDep, document_id: str) -> DocumentOut:
    doc = container.documents.get(document_id)
    if doc is None:
        raise NotFoundError(f"document {document_id} not found")
    return DocumentOut.from_model(doc)


@api_router.post(
    "/documents/{document_id}/retry",
    response_model=DocumentOut,
    status_code=status.HTTP_202_ACCEPTED,
    responses=_ERRORS,
)
def retry_document(container: ContainerDep, document_id: str) -> DocumentOut:
    """Queue a failed document for processing again (409 in any other status)."""
    doc = container.ingestion.retry(document_id)
    if doc is None:
        raise NotFoundError(f"document {document_id} not found")
    return DocumentOut.from_model(doc)


@api_router.delete(
    "/documents/{document_id}", status_code=status.HTTP_204_NO_CONTENT, responses=_ERRORS
)
def delete_document(container: ContainerDep, document_id: str) -> Response:
    if not container.documents.delete(document_id):
        raise NotFoundError(f"document {document_id} not found")
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@api_router.post("/search", response_model=SearchResponse, responses=_ERRORS)
def search(container: ContainerDep, body: SearchRequest) -> SearchResponse:
    timer = Timer()
    with timer.stage("retrieval"):
        result = container.rag.search(body.query, top_k=body.top_k, mode=body.mode)
    if result.rerank_ms is not None:
        timer.timings_ms["rerank"] = result.rerank_ms  # included in "retrieval"
    return SearchResponse.from_result(result, timer.timings_ms)


@api_router.post("/query", response_model=QueryResponse, responses=_ERRORS)
def query(container: ContainerDep, body: QueryRequest) -> QueryResponse:
    result = container.rag.answer(body.question, top_k=body.top_k, mode=body.mode)
    return QueryResponse.from_result(result)
