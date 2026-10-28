"""FastAPI application factory."""

from __future__ import annotations

import logging
import re
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Response

from production_rag import __version__
from production_rag.api.errors import error_response, register_error_handlers
from production_rag.api.routes import api_router, health_router
from production_rag.config import Settings, get_settings
from production_rag.container import Container, build_container
from production_rag.db.migrate import is_at_head
from production_rag.observability import configure_logging, request_id_var

logger = logging.getLogger(__name__)

_VALID_REQUEST_ID = re.compile(r"^[A-Za-z0-9._-]{1,64}$")


def create_app(settings: Settings | None = None, *, container: Container | None = None) -> FastAPI:
    settings = settings or (container.settings if container else get_settings())
    owns_container = container is None

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.container = container or build_container(settings)
        if not is_at_head(app.state.container.engine):
            # Migrations are an explicit deploy step; /ready reports 503 until they run.
            logger.error("startup.schema_not_at_head")
        if settings.embedding_provider == "hashing":
            logger.warning(
                "startup.lexical_only_embeddings",
                extra={"hint": "set RAG_EMBEDDING_PROVIDER=fastembed for semantic retrieval"},
            )
        logger.info(
            "startup.complete",
            extra={
                "version": __version__,
                "generator": app.state.container.rag.generator.model_id,
                "embedding_model": app.state.container.embedder.model_id,
                "auth_enabled": settings.api_key is not None,
            },
        )
        yield
        if owns_container:
            app.state.container.close()

    app = FastAPI(
        title="Production RAG",
        version=__version__,
        description="Retrieval-augmented generation with grounded, cited answers.",
        lifespan=lifespan,
    )

    @app.middleware("http")
    async def request_context(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        incoming = request.headers.get("x-request-id", "")
        request_id = incoming if _VALID_REQUEST_ID.match(incoming) else uuid.uuid4().hex
        token = request_id_var.set(request_id)
        start = time.perf_counter()
        try:
            try:
                response = await call_next(request)
            except Exception as exc:
                # Handled here rather than by an app-level handler so the request id is
                # still set when the error is logged and returned.
                logger.error("request.unhandled_error", exc_info=exc)
                response = error_response(500, "internal_error", "internal server error")
            response.headers["X-Request-ID"] = request_id
            logger.info(
                "http.request",
                extra={
                    "method": request.method,
                    "path": request.url.path,
                    "status": response.status_code,
                    "duration_ms": round((time.perf_counter() - start) * 1000, 2),
                },
            )
            return response
        finally:
            request_id_var.reset(token)

    register_error_handlers(app)
    app.include_router(health_router)
    app.include_router(api_router)
    return app


def app_factory() -> FastAPI:
    """Entry point for ``uvicorn --factory production_rag.api.app:app_factory``."""
    settings = get_settings()
    configure_logging(settings.log_level)
    return create_app(settings)
