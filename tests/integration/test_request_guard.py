"""Checks made before a request's body is read: authentication and body size (issue #30)."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from pydantic import SecretStr

from production_rag.api import create_app
from production_rag.api.guard import MULTIPART_ALLOWANCE, RequestGuard, RequestTooLargeError
from production_rag.api.security import require_api_key
from production_rag.config import Settings
from production_rag.container import Container
from production_rag.observability import JsonFormatter

API_KEY = "guard-test-key-0123456789"
CLIENT = ("203.0.113.7", 50000)


def _scope(method: str, path: str, headers: dict[str, str]) -> dict[str, Any]:
    return {
        "type": "http",
        "method": method,
        "path": path,
        "headers": [(k.lower().encode(), v.encode()) for k, v in headers.items()],
        "client": CLIENT,
        "query_string": b"",
        "http_version": "1.1",
        "scheme": "http",
        "server": ("testserver", 80),
        "root_path": "",
    }


class _Downstream:
    """Stands in for the application: reads the whole body, then answers 200."""

    def __init__(self) -> None:
        self.called = False

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        self.called = True
        while (await receive()).get("more_body"):
            pass
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})


def _run(
    settings: Settings, scope: dict[str, Any], chunks: list[bytes] | None = None
) -> tuple[int, dict[str, Any], int, bool]:
    """(status, JSON body, body messages read, whether the application ran)."""
    pending = list(chunks or [b""])
    reads = 0
    sent: list[dict[str, Any]] = []

    async def receive() -> dict[str, Any]:
        nonlocal reads
        reads += 1
        body = pending.pop(0) if pending else b""
        return {"type": "http.request", "body": body, "more_body": bool(pending)}

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    downstream = _Downstream()
    asyncio.run(RequestGuard(downstream, settings)(scope, receive, send))
    status = next(m["status"] for m in sent if m["type"] == "http.response.start")
    raw = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
    body = json.loads(raw) if raw != b"ok" else {}
    return status, body, reads, downstream.called


@pytest.fixture
def secured(settings: Settings) -> Settings:
    settings.api_key = SecretStr(API_KEY)
    return settings


def test_an_unauthenticated_upload_is_refused_before_its_body_is_read(secured: Settings) -> None:
    scope = _scope("POST", "/v1/documents", {"Content-Length": str(10 * 1024 * 1024)})
    status, body, reads, called = _run(secured, scope, [b"x" * 1024] * 8)
    assert (status, body["error"]["code"]) == (401, "unauthorized")
    assert reads == 0 and not called


@pytest.mark.parametrize(
    ("path", "limit_field", "allowance", "code"),
    [
        ("/v1/documents", "max_upload_bytes", MULTIPART_ALLOWANCE, "document_too_large"),
        ("/v1/search", "max_request_bytes", 0, "request_too_large"),
    ],
)
def test_a_declared_oversized_body_is_refused_before_it_is_read(
    secured: Settings, path: str, limit_field: str, allowance: int, code: str
) -> None:
    limit = getattr(secured, limit_field) + allowance
    headers = {"X-API-Key": API_KEY, "Content-Length": str(limit + 1)}
    status, body, reads, called = _run(secured, _scope("POST", path, headers))
    assert (status, body["error"]["code"]) == (413, code)
    assert reads == 0 and not called
    # At the limit itself the request goes through.
    headers["Content-Length"] = str(limit)
    status, _, _, called = _run(secured, _scope("POST", path, headers), [b"x" * limit])
    assert status == 200 and called


def test_an_undeclared_body_is_counted_and_stopped_at_the_limit(settings: Settings) -> None:
    settings.max_request_bytes = 4096
    chunks = [b"x" * 1024] * 100  # 100 KiB, sent without a Content-Length
    with pytest.raises(RequestTooLargeError) as raised:
        _run(settings, _scope("POST", "/v1/search", {}), chunks)
    assert raised.value.status_code == 413 and raised.value.code == "request_too_large"


@pytest.fixture
def secured_client(container: Container) -> Iterator[TestClient]:
    container.settings.api_key = SecretStr(API_KEY)
    with TestClient(create_app(container=container)) as client:
        yield client


def test_chunked_bodies_over_the_limit_get_the_error_envelope(container: Container) -> None:
    container.settings.max_request_bytes = 2048
    container.settings.max_upload_bytes = 1024

    def stream(data: bytes) -> Iterator[bytes]:  # no Content-Length: sent chunked
        yield data

    with TestClient(create_app(container=container)) as client:
        search = client.post(
            "/v1/search",
            content=stream(json.dumps({"query": "x" * 4000}).encode()),
            headers={"Content-Type": "application/json"},
        )
        boundary = "guardtestboundary"
        multipart = (
            f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="a.txt"\r\n'
            f"Content-Type: text/plain\r\n\r\n".encode()
            + b"x" * (1024 + MULTIPART_ALLOWANCE + 1)
            + f"\r\n--{boundary}--\r\n".encode()
        )
        upload = client.post(
            "/v1/documents",
            content=stream(multipart),
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        )
    assert search.status_code == 413 and search.json()["error"]["code"] == "request_too_large"
    assert search.json()["error"]["request_id"] == search.headers["X-Request-ID"]
    assert upload.status_code == 413 and upload.json()["error"]["code"] == "document_too_large"


def test_only_probes_and_api_docs_are_open(secured_client: TestClient) -> None:
    for path in ("/health", "/ready", "/openapi.json", "/docs", "/redoc"):
        assert secured_client.get(path).status_code == 200, path
    # Anything else needs the key, even a route that doesn't exist.
    assert secured_client.get("/v1/nope").status_code == 401
    assert secured_client.get("/admin").status_code == 401
    authorized = secured_client.get("/admin", headers={"Authorization": f"Bearer {API_KEY}"})
    assert authorized.status_code == 404


def test_the_route_dependency_checks_the_key_too(container: Container) -> None:
    """Defense in depth: /v1 routes refuse a bad key even if the guard were bypassed."""
    container.settings.api_key = SecretStr(API_KEY)
    request: Any = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(container=container)))
    with pytest.raises(HTTPException) as raised:
        require_api_key(request, None, "wrong-key")
    assert raised.value.status_code == 401
    require_api_key(request, None, API_KEY)  # accepted


def test_security_events_are_logged_without_credentials_or_bodies(
    secured_client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    wrong_key = "wrong-key-that-must-not-be-logged-42"
    with caplog.at_level(logging.WARNING):
        secured_client.get("/v1/documents")
        secured_client.get("/v1/documents", headers={"X-API-Key": wrong_key})
        secured_client.post(
            "/v1/search",
            headers={"X-API-Key": API_KEY},
            content=b'{"query": "body-text-that-must-not-be-logged"}' + b" " * 70_000,
        )
    events = [r for r in caplog.records if r.getMessage().startswith("security.")]
    assert [(r.getMessage(), getattr(r, "reason", None)) for r in events] == [
        ("security.auth_failed", "missing"),
        ("security.auth_failed", "invalid"),
        ("security.request_too_large", None),
    ]
    assert all(getattr(r, "client", None) == "testclient" for r in events)
    logged = "\n".join(JsonFormatter().format(r) for r in caplog.records)
    for secret in (wrong_key, API_KEY, "body-text-that-must-not-be-logged"):
        assert secret not in logged


def test_validation_errors_do_not_echo_the_rejected_input(client: TestClient) -> None:
    marker = "echoed-input-marker-"
    response = client.post("/v1/query", json={"question": marker * 200})
    assert response.status_code == 422
    assert marker not in response.text
    assert all("input" not in d for d in response.json()["error"]["details"])
