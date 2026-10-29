"""Structured JSON logging and lightweight timing helpers."""

from __future__ import annotations

import contextvars
import json
import logging
import sys
import time
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any

request_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "request_id", default=None
)

# Attributes every LogRecord has; anything else was passed through ``extra=`` and is logged.
_RESERVED = set(logging.LogRecord("", 0, "", 0, "", None, None).__dict__) | {"message"}


REDACTED = "[REDACTED]"
_MIN_REDACTED_LENGTH = 8  # shorter values would blank out ordinary words


class JsonFormatter(logging.Formatter):
    """One JSON object per line. ``redact`` values (the configured secrets) are replaced
    wherever they appear, exception text included: a backstop, since no code logs them."""

    def __init__(self, redact: Sequence[str] = ()) -> None:
        super().__init__()
        values = {value for value in redact if len(value) >= _MIN_REDACTED_LENGTH}
        # Each value as written and as json.dumps escapes it inside a string.
        self._redact = sorted(values | {json.dumps(v)[1:-1] for v in values}, key=len, reverse=True)

    def format(self, record: logging.LogRecord) -> str:
        line = self._format(record)
        for value in self._redact:
            line = line.replace(value, REDACTED)
        return line

    def _format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "event": record.getMessage(),
        }
        if (request_id := request_id_var.get()) is not None:
            payload["request_id"] = request_id
        for key, value in record.__dict__.items():
            if key not in _RESERVED and not key.startswith("_"):
                payload[key] = value
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def configure_logging(level: str = "INFO", *, redact: Sequence[str] = ()) -> None:
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(JsonFormatter(redact))
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level.upper())


class Timer:
    """Collects named stage durations in milliseconds."""

    def __init__(self) -> None:
        self.timings_ms: dict[str, float] = {}

    @contextmanager
    def stage(self, name: str) -> Iterator[None]:
        start = time.perf_counter()
        try:
            yield
        finally:
            self.timings_ms[name] = round((time.perf_counter() - start) * 1000, 2)
