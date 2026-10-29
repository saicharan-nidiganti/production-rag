"""The ingestion queue: document processing state, changed only by compare-and-set.

The ``documents`` table is the queue (docs/decisions/0006-background-ingestion.md).
Every transition is one ``UPDATE ... WHERE`` that names the state it expects, and succeeds
only if it changed exactly one row, so any number of threads and processes can share the
database without double-processing a document:

- **claim**: ``queued`` -> ``processing`` with a fresh token and a lease. Also takes over a
  ``processing`` document whose lease has lapsed (its worker stopped), or fails it after
  ``RAG_INGEST_MAX_ATTEMPTS`` interruptions.
- **renew**: extends the lease while the token still matches.
- **complete**, **fail**, **release**: end a claim; each applies only while the token
  matches, so a worker that lost its claim can't overwrite the new owner's state.
- **requeue_failed**: ``failed`` -> ``queued``, on retry or an identical re-upload.
"""

from __future__ import annotations

import logging
import threading
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, cast

from sqlalchemy import ColumnElement, CursorResult, and_, delete, or_, select, update
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, sessionmaker

from production_rag.config import Settings
from production_rag.db.models import Chunk, Document, DocumentFile, utcnow

logger = logging.getLogger(__name__)


class ClaimLost(Exception):
    """The document was deleted, or another worker took it over, while being processed."""


@dataclass(frozen=True)
class Claim:
    document_id: str
    token: str
    attempt: int


def _token_is(token: str | None) -> ColumnElement[bool]:
    return Document.claim_token.is_(None) if token is None else Document.claim_token == token


def _transition(
    session: Session, document_id: str, conditions: Sequence[ColumnElement[bool]], **values: Any
) -> bool:
    """Apply ``values`` if the row still meets ``conditions``; True if it did."""
    result = cast(
        "CursorResult[Any]",
        session.execute(
            update(Document)
            .where(Document.id == document_id, *conditions)
            .values(**values)
            .execution_options(synchronize_session=False)
        ),
    )
    return result.rowcount == 1


def _ended(now: datetime, **values: Any) -> dict[str, Any]:
    """Values for a transition that ends any claim."""
    return {"claim_token": None, "lease_expires_at": None, "updated_at": now, **values}


class IngestionQueue:
    def __init__(self, sessions: sessionmaker[Session], settings: Settings) -> None:
        self._sessions = sessions
        self._settings = settings
        # Set when work is queued by this process, so its idle workers start at once
        # instead of at their next poll. Workers in other processes poll.
        self.work_available = threading.Event()

    @property
    def _lease(self) -> timedelta:
        return timedelta(seconds=self._settings.ingest_lease_s)

    def claim_next(self) -> Claim | None:
        """Claim the oldest queued document, or take over one whose worker has stopped."""
        while True:
            now = utcnow()
            with self._sessions() as session:
                row = session.execute(
                    select(Document.id, Document.status, Document.claim_token, Document.attempts)
                    .where(
                        or_(
                            Document.status == "queued",
                            and_(Document.status == "processing", Document.lease_expires_at < now),
                        )
                    )
                    .order_by(Document.updated_at, Document.id)
                    .limit(1)
                ).first()
                if row is None:
                    return None
                interrupted_too_often = row.attempts >= self._settings.ingest_max_attempts
                if row.status == "processing" and interrupted_too_often:
                    self._give_up(session, row.id, row.claim_token, row.attempts, now)
                    session.commit()
                    continue
                claim = self._claim(session, row.id, row.status, row.claim_token, row.attempts)
                session.commit()
            if claim is not None:
                if row.status == "processing":
                    logger.warning(
                        "ingest.recovered",
                        extra={"document_id": claim.document_id, "attempt": claim.attempt},
                    )
                return claim
            # Another worker claimed it between the read and the update: look again.

    def claim(self, document_id: str) -> Claim | None:
        """Claim one queued document; None if it isn't queued (another worker has it)."""
        with self._sessions() as session:
            attempts = session.scalar(
                select(Document.attempts).where(
                    Document.id == document_id, Document.status == "queued"
                )
            )
            if attempts is None:
                return None
            claim = self._claim(session, document_id, "queued", None, attempts)
            session.commit()
        return claim

    def renew(self, claim: Claim) -> None:
        """Extend the lease. Raises ClaimLost if the claim is no longer this worker's."""
        try:
            with self._sessions() as session:
                renewed = _transition(
                    session,
                    claim.document_id,
                    [Document.claim_token == claim.token],
                    lease_expires_at=utcnow() + self._lease,
                )
                session.commit()
        except OperationalError:
            # Usually "database is locked". Skipping one renewal is safe: if the lease lapses
            # and another worker takes over, this worker's completion is discarded.
            logger.warning("ingest.lease_renewal_failed", extra={"document_id": claim.document_id})
            return
        if not renewed:
            raise ClaimLost

    def complete(self, claim: Claim, chunks: Sequence[Chunk], **fields: Any) -> None:
        """Mark the document ready with ``fields``, insert its chunks and drop the upload,
        in one transaction. Raises ClaimLost, writing nothing, if the claim was lost."""
        with self._sessions() as session:
            if not _transition(
                session,
                claim.document_id,
                [Document.claim_token == claim.token],
                **_ended(utcnow(), status="ready", **fields),
            ):
                raise ClaimLost
            session.add_all(chunks)
            session.execute(
                delete(DocumentFile).where(DocumentFile.document_id == claim.document_id)
            )
            session.commit()

    def fail(self, claim: Claim, code: str, message: str) -> None:
        """Mark the document failed; the upload is kept so it can be retried."""
        with self._sessions() as session:
            _transition(
                session,
                claim.document_id,
                [Document.claim_token == claim.token],
                **_ended(utcnow(), status="failed", error_code=code, error_message=message),
            )
            session.commit()

    def release(self, claim: Claim) -> None:
        """Back to the queue, without counting the attempt: the worker chose to stop."""
        with self._sessions() as session:
            _transition(
                session,
                claim.document_id,
                [Document.claim_token == claim.token],
                **_ended(utcnow(), status="queued", attempts=Document.attempts - 1),
            )
            session.commit()

    def requeue_failed(self, session: Session, document_id: str) -> bool:
        """``failed`` -> ``queued`` within the caller's transaction; False if not failed."""
        return _transition(
            session,
            document_id,
            [Document.status == "failed"],
            **_ended(utcnow(), status="queued", error_code=None, error_message=None, attempts=0),
        )

    def _claim(
        self, session: Session, document_id: str, status: str, token: str | None, attempts: int
    ) -> Claim | None:
        now = utcnow()
        new_token = str(uuid.uuid4())
        claimed = _transition(
            session,
            document_id,
            [Document.status == status, _token_is(token)],
            status="processing",
            claim_token=new_token,
            attempts=attempts + 1,
            lease_expires_at=now + self._lease,
            error_code=None,
            error_message=None,
            updated_at=now,
        )
        return Claim(document_id, new_token, attempts + 1) if claimed else None

    def _give_up(
        self, session: Session, document_id: str, token: str | None, attempts: int, now: datetime
    ) -> None:
        message = (
            f"processing was interrupted {attempts} time{'s' if attempts != 1 else ''} (the "
            "worker stopped before finishing). Retry; if it keeps happening, check the server "
            "log and resources for this document"
        )
        if _transition(
            session,
            document_id,
            [_token_is(token)],
            **_ended(now, status="failed", error_code="interrupted", error_message=message),
        ):
            logger.warning(
                "ingest.failed",
                extra={
                    "document_id": document_id,
                    "attempt": attempts,
                    "error_code": "interrupted",
                },
            )
