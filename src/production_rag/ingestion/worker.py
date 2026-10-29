"""Background ingestion threads.

Each thread claims a queued document, processes it, and repeats; when the queue is empty it
waits until work is submitted in this process or the poll interval passes. One failing
document never stops a thread: ``IngestionService.process`` records the failure on the
document, and anything unexpected around it is logged and retried after a backoff.

``stop`` asks the threads to finish: a document being processed goes back to the queue at
its next lease renewal (between embedding batches) rather than being worked on to the end.
"""

from __future__ import annotations

import logging
import threading

from production_rag.ingestion.pipeline import IngestionService

logger = logging.getLogger(__name__)

_MAX_BACKOFF_S = 30.0


class IngestionWorker:
    def __init__(
        self, service: IngestionService, *, threads: int = 1, poll_interval_s: float = 2.0
    ) -> None:
        self._service = service
        self._count = threads
        self._poll_interval_s = poll_interval_s
        self._stopping = threading.Event()
        self._threads: list[threading.Thread] = []

    @property
    def alive(self) -> int:
        return sum(thread.is_alive() for thread in self._threads)

    def start(self) -> None:
        for i in range(self._count):
            # Daemon threads: a document still processing at exit is recovered by the next
            # worker once its lease lapses.
            thread = threading.Thread(target=self._run, name=f"ingest-worker-{i}", daemon=True)
            thread.start()
            self._threads.append(thread)
        if self._threads:
            logger.info("ingest.workers_started", extra={"threads": self._count})

    def stop(self, timeout_s: float = 10.0) -> None:
        self._stopping.set()
        self._service.work_available.set()  # wake idle threads
        for thread in self._threads:
            thread.join(timeout_s)
        self._threads = [thread for thread in self._threads if thread.is_alive()]

    def _run(self) -> None:
        backoff = self._poll_interval_s
        while not self._stopping.is_set():
            try:
                claim = self._service.claim_next()
                if claim is None:
                    self._service.work_available.wait(self._poll_interval_s)
                    self._service.work_available.clear()
                    continue
                self._service.process(claim, should_stop=self._stopping.is_set)
                backoff = self._poll_interval_s
            except Exception:
                # For example the database is unavailable, or not migrated yet.
                logger.exception("ingest.worker_error", extra={"retry_in_s": backoff})
                self._stopping.wait(backoff)
                backoff = min(backoff * 2, _MAX_BACKOFF_S)
