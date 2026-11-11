"""Serving the web interface from RAG_WEB_DIR (api/web.py)."""

from __future__ import annotations

import logging
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from production_rag.api import create_app
from production_rag.container import Container

API_KEY = "w" * 24  # obviously made up


@pytest.fixture
def site(tmp_path: Path) -> Path:
    root = tmp_path / "site"
    (root / "assets").mkdir(parents=True)
    (root / "index.html").write_text("<!doctype html><title>Production RAG</title>", "utf-8")
    (root / "favicon.svg").write_text("<svg xmlns='http://www.w3.org/2000/svg'/>", "utf-8")
    (root / "assets" / "index-abc123.js").write_text("console.log('app')", "utf-8")
    (tmp_path / "secret.txt").write_text("outside the site", "utf-8")
    return root


@pytest.fixture
def web(container: Container, site: Path) -> Iterator[TestClient]:
    container.settings.web_dir = str(site)
    container.settings.api_key = SecretStr(API_KEY)
    with TestClient(create_app(container=container)) as client:
        yield client


def test_the_page_loads_without_the_key_but_the_api_still_needs_it(web: TestClient) -> None:
    for path in ("/", "/index.html"):
        page = web.get(path)
        assert page.status_code == 200 and "Production RAG" in page.text
    assert web.get("/favicon.svg").status_code == 200
    assert web.get("/assets/index-abc123.js").text == "console.log('app')"

    assert web.get("/v1/documents").status_code == 401
    assert web.get("/metrics").status_code == 401
    assert web.get("/v1/documents", headers={"X-API-Key": API_KEY}).status_code == 200


def test_the_page_allows_only_its_own_origin_and_no_framing(web: TestClient) -> None:
    headers = web.get("/").headers
    policy = headers["content-security-policy"]
    assert "default-src 'self'" in policy and "frame-ancestors 'none'" in policy
    assert "unsafe-inline" not in policy and "unsafe-eval" not in policy
    assert headers["x-content-type-options"] == "nosniff"
    assert headers["x-frame-options"] == "DENY"
    assert headers["referrer-policy"] == "no-referrer"


def test_the_page_is_revalidated_and_hashed_files_are_cached(web: TestClient) -> None:
    assert web.get("/").headers["cache-control"] == "no-cache"
    asset = web.get("/assets/index-abc123.js").headers
    assert asset["cache-control"] == "public, max-age=31536000, immutable"
    assert asset["x-content-type-options"] == "nosniff"


@pytest.mark.parametrize(
    "path",
    [
        "/assets/../secret.txt",
        "/assets/%2e%2e/secret.txt",
        "/assets/..%2fsecret.txt",
        "/secret.txt",
    ],
)
def test_nothing_outside_the_directory_is_served(web: TestClient, path: str) -> None:
    response = web.get(path)
    assert response.status_code in (401, 404)
    assert "outside the site" not in response.text


def test_no_interface_is_served_unless_configured(container: Container) -> None:
    with TestClient(create_app(container=container)) as client:
        assert client.get("/").status_code == 404
        assert client.get("/assets/index-abc123.js").status_code == 404


def test_a_directory_that_isnt_built_is_reported_not_fatal(
    container: Container, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    container.settings.web_dir = str(tmp_path / "missing")
    with caplog.at_level(logging.WARNING), TestClient(create_app(container=container)) as client:
        assert client.get("/").status_code == 404
        assert client.get("/health").status_code == 200
    assert any(record.message == "web.not_found" for record in caplog.records)


def test_the_built_interface_is_served_when_present(container: Container) -> None:
    """The real build, if `npm run build` has run; skipped otherwise (CI's web job builds it)."""
    dist = Path(__file__).resolve().parents[2] / "web" / "dist"
    if not (dist / "index.html").is_file():
        pytest.skip("web/dist isn't built")
    container.settings.web_dir = str(dist)
    with TestClient(create_app(container=container)) as client:
        page = client.get("/")
        assert page.status_code == 200 and '<div id="root">' in page.text
        script = page.text.split('src="')[1].split('"')[0]
        assert script.startswith("/assets/") and client.get(script).status_code == 200
