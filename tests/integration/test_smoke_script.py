"""Tests of scripts/smoke_test.py itself (issue #39): it passes against a healthy server and
fails cleanly, with exit code 1, on every way a deployment can be broken.

Failures are simulated with httpx.MockTransport (no network) and, for a dead server, a
real child process that exits at once.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from collections.abc import Callable
from types import ModuleType

import httpx
import pytest
from fastapi.testclient import TestClient

from tests.conftest import REPO_ROOT

Handler = Callable[[httpx.Request], httpx.Response]


def _load_script() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "smoke_test", REPO_ROOT / "scripts" / "smoke_test.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


smoke = _load_script()
DOC_ID = "d1"


def _healthy(request: httpx.Request) -> httpx.Response:
    """A minimal server that answers every check correctly."""
    path, method = request.url.path, request.method
    if path == "/ready":
        ingestion = {"ready": 1, "workers": 1}
        return httpx.Response(200, json={"status": "ready", "checks": {"ingestion": ingestion}})
    if path == "/health":
        return httpx.Response(200, json={"status": "ok"})
    if path == "/v1/documents" and method == "POST":
        return httpx.Response(
            202,
            json={"document": {"id": DOC_ID}},
            headers={"location": f"/v1/documents/{DOC_ID}"},
        )
    if path == f"/v1/documents/{DOC_ID}":
        return httpx.Response(200, json={"status": "ready", "num_chunks": 5})
    if path == "/v1/search":
        return httpx.Response(200, json={"hits": [{"source_name": smoke.DOCUMENT.name}]})
    if path == "/v1/query":
        body = {"status": "answered", "answer": "It runs for 30 minutes.", "citations": [{}]}
        return httpx.Response(200, json=body)
    return httpx.Response(404)


def _client(handler: Handler) -> httpx.Client:
    return httpx.Client(base_url="http://smoke.test", transport=httpx.MockTransport(handler))


def _with(path: str, response: Callable[[httpx.Request], httpx.Response]) -> Handler:
    """The healthy server, except for one path."""

    def handler(request: httpx.Request) -> httpx.Response:
        return response(request) if request.url.path == path else _healthy(request)

    return handler


def _fails(handler: Handler, match: str, **limits: float) -> None:
    with _client(handler) as client, pytest.raises(smoke.SmokeTestError, match=match):
        smoke.check(client, ready_timeout_s=0.3, ingest_timeout_s=0.3, **limits)


def test_passes_against_a_healthy_server() -> None:
    with _client(_healthy) as client:
        smoke.check(client)


def test_passes_against_the_real_application(client: TestClient) -> None:
    smoke.check(client, ready_timeout_s=30, ingest_timeout_s=30)


def test_fails_when_the_service_never_becomes_ready() -> None:
    # For example, migrations not applied: /ready answers 503 (tests/integration/test_api.py).
    _fails(_with("/ready", lambda _: httpx.Response(503)), "timed out .* GET /ready")


def test_fails_on_a_response_that_isnt_json() -> None:
    _fails(_with("/health", lambda _: httpx.Response(200, text="<html>")), "unexpected response")


def test_fails_on_a_response_missing_a_field() -> None:
    _fails(
        _with("/v1/query", lambda _: httpx.Response(200, json={"status": "answered"})),
        "unexpected response: KeyError",
    )


def test_fails_on_a_wrong_status_code() -> None:
    _fails(
        _with("/v1/documents", lambda _: httpx.Response(500, json={"error": {}})),
        r"POST /v1/documents is accepted \(202\)",
    )


def test_fails_when_ingestion_fails() -> None:
    doc = {"status": "failed", "num_chunks": 0}
    _fails(
        _with(f"/v1/documents/{DOC_ID}", lambda _: httpx.Response(200, json=doc)),
        "background ingestion: ready",
    )


def test_fails_when_ingestion_never_finishes() -> None:
    doc = {"status": "processing", "num_chunks": 0}
    _fails(
        _with(f"/v1/documents/{DOC_ID}", lambda _: httpx.Response(200, json=doc)),
        "timed out .* finish processing",
    )


def test_fails_when_the_answer_has_no_citation() -> None:
    body = {"status": "answered", "answer": "30 minutes", "citations": []}
    _fails(
        _with("/v1/query", lambda _: httpx.Response(200, json=body)),
        "answers with a citation",
    )


def test_fails_cleanly_when_a_request_times_out() -> None:
    def slow(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out", request=request)

    _fails(_with("/v1/search", slow), "request timed out: POST .*/v1/search")


def test_fails_when_the_server_process_dies() -> None:
    dead = subprocess.Popen([sys.executable, "-c", "raise SystemExit(3)"])
    dead.wait(timeout=30)

    def refused(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    with (
        _client(refused) as client,
        pytest.raises(smoke.SmokeTestError, match="exited with code 3"),
    ):
        smoke.check(client, dead, ready_timeout_s=5)


def test_main_exits_1_and_reports_the_failure(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def broken(_: object) -> None:
        raise smoke.SmokeTestError("GET /health")

    monkeypatch.setattr(smoke, "run", broken)
    assert smoke.main() == 1
    assert "FAILED: GET /health" in capsys.readouterr().err


def test_main_exits_0_when_every_check_passes(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(smoke, "run", lambda _: None)
    assert smoke.main() == 0
    assert "smoke test passed" in capsys.readouterr().out


def test_mock_responses_match_the_real_api_shape(client: TestClient) -> None:
    """Guards the fakes above against drifting from what the API really returns."""
    ready = client.get("/ready").json()
    assert {"ready", "workers"} <= ready["checks"]["ingestion"].keys()
    assert (
        json.loads(_healthy(httpx.Request("GET", "http://x/health")).text)
        == {"status": "ok"}
        == client.get("/health").json()
    )
