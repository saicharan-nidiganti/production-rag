"""Uploading a new version of a document: `POST /v1/documents?replaces={id}`.

The replaced document is deleted in the transaction that marks its replacement ready, so a
search finds the old version until the new one is ready and the new one from then on. If
the new version fails, the old one stays.
"""

from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from production_rag.container import Container
from production_rag.db import maintenance as m
from production_rag.ingestion import DocumentStateError, ReplacedDocumentNotFoundError

V1 = b"# Deployment\n\n## Canary\n\nThe canary receives 5% of traffic for 30 minutes.\n"
V2 = b"# Deployment\n\n## Canary\n\nThe canary receives 10% of traffic for 45 minutes.\n"
OTHER = b"# Incidents\n\n## Paging\n\nPage the on-call engineer within five minutes.\n"
NO_TEXT = b"# Title\n\n## Nothing here\n"


def process(container: Container, document_id: str) -> str:
    claim = container.ingestion.queue.claim(document_id)
    assert claim is not None
    return container.ingestion.process(claim).status


def found(container: Container, query: str = "canary traffic") -> set[str]:
    hits = container.rag.retriever.search(query, top_k=5, mode="hybrid").chunks
    return {hit.document_id for hit in hits}


def test_the_old_version_is_searched_until_the_new_one_is_ready(container: Container) -> None:
    v1 = container.ingestion.ingest("deploy.md", V1).document.id
    v2 = container.ingestion.submit("deploy.md", V2, notify=False, replaces=v1).document

    assert v2.status == "queued" and v2.replaces_id == v1
    assert found(container) == {v1}  # still answering from the old version

    assert process(container, v2.id) == "ready"

    assert container.documents.get(v1) is None
    replacement = container.documents.get(v2.id)
    assert replacement is not None and replacement.replaces_id == v1  # kept as lineage
    assert found(container) == {v2.id}
    assert m.verify(m.database_file(container.settings.database_url)).ok  # nothing orphaned


def test_a_failed_new_version_leaves_the_old_one(container: Container) -> None:
    v1 = container.ingestion.ingest("deploy.md", V1).document.id
    broken = container.ingestion.submit("deploy.md", NO_TEXT, notify=False, replaces=v1).document

    assert process(container, broken.id) == "failed"
    assert container.documents.get(v1) is not None
    assert found(container) == {v1}


def test_replacing_a_document_that_doesnt_exist_is_refused(container: Container) -> None:
    with pytest.raises(ReplacedDocumentNotFoundError, match="doesn't exist"):
        container.ingestion.submit("deploy.md", V1, replaces="no-such-document")
    assert container.documents.list_documents(limit=10, offset=0, status=None).total == 0


def test_content_that_is_already_ready_replaces_at_once(container: Container) -> None:
    v1 = container.ingestion.ingest("deploy.md", V1).document.id
    other = container.ingestion.ingest("incidents.md", OTHER).document.id

    result = container.ingestion.submit("incidents.md", OTHER, replaces=v1)

    assert not result.created and result.document.id == other
    assert container.documents.get(v1) is None


def test_content_already_queued_becomes_the_replacement(container: Container) -> None:
    v1 = container.ingestion.ingest("deploy.md", V1).document.id
    queued = container.ingestion.submit("deploy.md", V2, notify=False).document.id

    again = container.ingestion.submit("deploy.md", V2, notify=False, replaces=v1)

    assert again.document.id == queued and again.document.replaces_id == v1
    assert container.documents.get(v1) is not None
    assert process(container, queued) == "ready"
    assert container.documents.get(v1) is None


def test_one_upload_cant_replace_two_documents(container: Container) -> None:
    v1 = container.ingestion.ingest("deploy.md", V1).document.id
    other = container.ingestion.ingest("incidents.md", OTHER).document.id
    container.ingestion.submit("deploy.md", V2, notify=False, replaces=v1)

    with pytest.raises(DocumentStateError, match="another document"):
        container.ingestion.submit("deploy.md", V2, notify=False, replaces=other)


def test_identical_content_replacing_itself_changes_nothing(container: Container) -> None:
    v1 = container.ingestion.ingest("deploy.md", V1).document.id
    result = container.ingestion.submit("deploy.md", V1, replaces=v1)
    assert not result.created and result.document.id == v1
    assert container.documents.get(v1) is not None


def test_the_old_version_may_be_deleted_first(container: Container) -> None:
    v1 = container.ingestion.ingest("deploy.md", V1).document.id
    v2 = container.ingestion.submit("deploy.md", V2, notify=False, replaces=v1).document.id
    assert container.documents.delete(v1)

    assert process(container, v2) == "ready"
    assert found(container) == {v2}


def test_replacement_over_http(client: TestClient) -> None:
    def upload(data: bytes, **params: str) -> dict[str, object]:
        response = client.post("/v1/documents", files={"file": ("deploy.md", data)}, params=params)
        assert response.status_code in (200, 202), response.text
        return response.json()["document"]  # type: ignore[no-any-return]

    def wait(document_id: object) -> dict[str, object]:
        deadline = time.monotonic() + 15
        while (doc := client.get(f"/v1/documents/{document_id}").json())["status"] not in (
            "ready",
            "failed",
        ):
            assert time.monotonic() < deadline
            time.sleep(0.02)
        return doc  # type: ignore[no-any-return]

    v1 = upload(V1)
    assert v1["replaces"] is None
    wait(v1["id"])
    v2 = upload(V2, replaces=str(v1["id"]))
    assert v2["replaces"] == v1["id"]
    assert wait(v2["id"])["status"] == "ready"

    assert client.get(f"/v1/documents/{v1['id']}").status_code == 404
    assert client.get(f"/v1/documents/{v2['id']}").json()["replaces"] == v1["id"]

    unknown = client.post(
        "/v1/documents", files={"file": ("x.md", OTHER)}, params={"replaces": "nope"}
    )
    assert unknown.status_code == 404
    assert unknown.json()["error"]["code"] == "not_found"
