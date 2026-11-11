"""Serving the web interface (``web/``), when ``RAG_WEB_DIR`` points at its built files.

The interface is a static single-page app that calls this same origin, so there is no CORS
to configure. Its files are public: they hold no data, and the page needs to load before
anyone can type an API key. Everything it shows comes from ``/v1``, which the key guards.

Responses carry a Content-Security-Policy that allows only this origin: no inline or
third-party scripts, no framing. The page's script and style files have content hashes in
their names, so they are cached for a year; ``index.html`` is never cached, so a new
version is picked up on the next load.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from starlette.responses import Response
from starlette.staticfiles import StaticFiles
from starlette.types import Scope

logger = logging.getLogger(__name__)

# Paths the request guard lets through without the API key.
OPEN_PATHS = frozenset({"/", "/index.html", "/favicon.svg"})
OPEN_PREFIX = "/assets/"

SECURITY_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
        "connect-src 'self'; base-uri 'self'; form-action 'self'; frame-ancestors 'none'"
    ),
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "X-Frame-Options": "DENY",
}


def is_open(path: str) -> bool:
    return path in OPEN_PATHS or path.startswith(OPEN_PREFIX)


class _Assets(StaticFiles):
    """The page's script and style files. Their names carry content hashes, so a file never
    changes under its name and can be cached for good."""

    def file_response(
        self,
        full_path: str | os.PathLike[str],
        stat_result: os.stat_result,
        scope: Scope,
        status_code: int = 200,
    ) -> Response:
        response = super().file_response(full_path, stat_result, scope, status_code)
        response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response


def mount_web(app: FastAPI, directory: str | None) -> bool:
    """Serve the interface from ``directory``; False, with a warning, if it isn't built."""
    if directory is None:
        return False
    root = Path(directory)
    index = root / "index.html"
    if not index.is_file():
        logger.warning(
            "web.not_found",
            extra={"hint": "RAG_WEB_DIR has no index.html; run `npm run build` in web/"},
        )
        return False

    def page() -> FileResponse:
        return FileResponse(index, headers={**SECURITY_HEADERS, "Cache-Control": "no-cache"})

    app.add_api_route("/", page, include_in_schema=False)
    app.add_api_route("/index.html", page, include_in_schema=False)
    favicon = root / "favicon.svg"
    if favicon.is_file():
        app.add_api_route(
            "/favicon.svg",
            lambda: FileResponse(favicon, headers={"Cache-Control": "public, max-age=86400"}),
            include_in_schema=False,
        )
    if (root / "assets").is_dir():
        app.mount(OPEN_PREFIX.rstrip("/"), _Assets(directory=root / "assets"), name="web-assets")
    return True
