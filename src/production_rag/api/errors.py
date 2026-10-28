"""Maps domain exceptions to HTTP responses with a consistent error body."""

from __future__ import annotations

import logging
from typing import Any

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from production_rag.api.schemas import ErrorBody, ErrorResponse
from production_rag.generation import GenerationError
from production_rag.ingestion import (
    DocumentTooLargeError,
    ExtractionError,
    UnsupportedDocumentError,
)
from production_rag.observability import request_id_var

logger = logging.getLogger(__name__)


class NotFoundError(LookupError):
    pass


def error_response(
    status: int, code: str, message: str, *, details: list[dict[str, Any]] | None = None
) -> JSONResponse:
    body = ErrorResponse(
        error=ErrorBody(
            code=code, message=message, request_id=request_id_var.get(), details=details
        )
    )
    exclude = {"error": {"details"}} if details is None else None
    return JSONResponse(status_code=status, content=body.model_dump(exclude=exclude))


def register_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(NotFoundError)
    async def _not_found(_: Request, exc: NotFoundError) -> JSONResponse:
        return error_response(404, "not_found", str(exc))

    @app.exception_handler(UnsupportedDocumentError)
    async def _unsupported(_: Request, exc: UnsupportedDocumentError) -> JSONResponse:
        return error_response(415, "unsupported_document_type", str(exc))

    @app.exception_handler(DocumentTooLargeError)
    async def _too_large(_: Request, exc: DocumentTooLargeError) -> JSONResponse:
        return error_response(413, "document_too_large", str(exc))

    @app.exception_handler(ExtractionError)
    async def _extraction(_: Request, exc: ExtractionError) -> JSONResponse:
        return error_response(422, "extraction_failed", str(exc))

    @app.exception_handler(GenerationError)
    async def _generation(_: Request, exc: GenerationError) -> JSONResponse:
        logger.error("generation.failed", extra={"retryable": exc.retryable}, exc_info=exc)
        if exc.retryable:
            return error_response(503, "generation_unavailable", str(exc))
        return error_response(502, "generation_failed", str(exc))

    @app.exception_handler(RequestValidationError)
    async def _validation(_: Request, exc: RequestValidationError) -> JSONResponse:
        return error_response(
            422,
            "validation_error",
            "request validation failed",
            details=jsonable_encoder(exc.errors()),
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http(_: Request, exc: StarletteHTTPException) -> JSONResponse:
        response = error_response(
            exc.status_code, _HTTP_CODES.get(exc.status_code, "http_error"), str(exc.detail)
        )
        for name, value in (exc.headers or {}).items():
            response.headers[name] = value
        return response


_HTTP_CODES = {401: "unauthorized", 403: "forbidden", 404: "not_found", 405: "method_not_allowed"}
