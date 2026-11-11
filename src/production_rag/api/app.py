"""FastAPI application factory."""

from __future__ import annotations

import logging
import re
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Response
from starlette.datastructures import Headers

from production_rag import __version__, metrics
from production_rag.accounts import Principal
from production_rag.api.auth_routes import auth_router, users_router
from production_rag.api.errors import error_response, register_error_handlers
from production_rag.api.guard import RequestGuard
from production_rag.api.monitoring import metrics_router
from production_rag.api.ratelimit import RateLimiter
from production_rag.api.routes import api_router, health_router
from production_rag.api.security import resolve_principal
from production_rag.api.web import mount_web
from production_rag.config import ConfigurationError, Settings, get_settings
from production_rag.container import Container, build_container
from production_rag.db.migrate import is_at_head
from production_rag.ingestion import IngestionWorker
from production_rag.observability import configure_logging, request_id_var

logger = logging.getLogger(__name__)

_VALID_REQUEST_ID = re.compile(r"^[A-Za-z0-9._-]{1,64}$")


def _route(request: Request) -> str:
    """The matched route's template (``/v1/documents/{document_id}``), never the raw path, so
    a metric gets one series per route rather than one per URL a client tries."""
    route = request.scope.get("route")
    path = getattr(route, "path", None)
    return path if isinstance(path, str) else "unmatched"


def create_app(settings: Settings | None = None, *, container: Container | None = None) -> FastAPI:
    settings = settings or (container.settings if container else get_settings())
    owns_container = container is None

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.container = container or build_container(settings)
        if not is_at_head(app.state.container.engine):
            # Migrations are an explicit deploy step; /ready reports 503 until they run.
            # Ingestion workers back off and retry until then.
            logger.error("startup.schema_not_at_head")
        worker = IngestionWorker(
            app.state.container.ingestion,
            threads=settings.ingest_workers,
            poll_interval_s=settings.ingest_poll_interval_s,
        )
        worker.start()
        app.state.ingest_worker = worker
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
                "accounts_enabled": settings.accounts_enabled,
                "ingest_workers": settings.ingest_workers,
                # Per client and per process: with N workers a client gets up to N times.
                "rate_limits_per_minute": {
                    "scope": "process",
                    "upload": settings.rate_limit_upload_per_minute,
                    "query": settings.rate_limit_query_per_minute,
                    "search": settings.rate_limit_search_per_minute,
                    "read": settings.rate_limit_read_per_minute,
                    "auth_failures": settings.rate_limit_auth_failures_per_minute,
                },
            },
        )
        yield
        worker.stop()
        if owns_container:
            app.state.container.close()

    app = FastAPI(
        title="Production RAG",
        version=__version__,
        description="Retrieval-augmented generation with grounded, cited answers.",
        lifespan=lifespan,
        # The docs are served without the API key; RAG_API_DOCS_ENABLED=false removes them.
        docs_url="/docs" if settings.api_docs_enabled else None,
        redoc_url="/redoc" if settings.api_docs_enabled else None,
        openapi_url="/openapi.json" if settings.api_docs_enabled else None,
    )
    # Added before request_context, so it runs inside it: refusals get a request id and are
    # logged like any other response.
    limiter = RateLimiter()
    app.state.rate_limiter = limiter  # the sign-in route counts failures against it too

    def authenticate(headers: Headers) -> Principal | None:
        # Looked up per request: the container is built when the app starts.
        return resolve_principal(headers, app.state.container.accounts)

    app.add_middleware(RequestGuard, settings=settings, limiter=limiter, authenticator=authenticate)

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
            elapsed = time.perf_counter() - start
            logger.info(
                "http.request",
                extra={
                    "method": request.method,
                    "path": request.url.path,
                    "status": response.status_code,
                    "duration_ms": round(elapsed * 1000, 2),
                },
            )
            metrics.observe_http(request.method, _route(request), response.status_code, elapsed)
            return response
        finally:
            request_id_var.reset(token)

    register_error_handlers(app)
    app.include_router(health_router)
    app.include_router(api_router)
    app.include_router(metrics_router)
    app.include_router(auth_router)
    app.include_router(users_router)
    mount_web(app, settings.web_dir)
    return app


def app_factory() -> FastAPI:
    """Entry point for ``uvicorn --factory production_rag.api.app:app_factory``."""
    try:
        settings = get_settings()
    except ConfigurationError as exc:
        raise SystemExit(str(exc)) from None  # a clean message, without the rejected values
    configure_logging(settings.log_level, redact=settings.secret_values())
    return create_app(settings)
