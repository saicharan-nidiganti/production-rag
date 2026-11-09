"""Per-client rate limits (issue #30, NFR-2), applied by ``guard.RequestGuard``.

Each client address has a token bucket per kind of operation: ``upload``, ``query`` (which can
call a paid model), ``search`` and ``read`` (everything else under ``/v1``), plus one for failed
API-key attempts. A bucket holds up to a minute's allowance, so a client may burst, and refills
continuously at the per-minute rate.

The buckets live in this process's memory, and that is the honest scope of these limits:

- With ``uvicorn --workers N`` each process keeps its own buckets, so a client can make up to
  N times a limit, and a restart forgets them all. A limit that holds across processes or
  machines belongs at the reverse proxy, or needs a shared store that v0 doesn't have
  (ADR-0002). See docs/operations.md.
- The client address is what uvicorn reports: the peer, or the ``X-Forwarded-For`` address
  when the peer is a proxy listed in ``--forwarded-allow-ips``. Behind an unlisted proxy every
  client shares the proxy's address and therefore one set of buckets.
- IPv6 clients are grouped by /64, the block a single host usually controls.
- At most ``MAX_TRACKED`` buckets are kept; the least recently used is dropped beyond that, so
  memory stays bounded however many addresses a client cycles through.
"""

from __future__ import annotations

import ipaddress
import math
import threading
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

from production_rag.config import Settings

Operation = Literal["upload", "query", "search", "read", "auth_failure"]

MAX_TRACKED = 50_000


def operation_for(method: str, path: str) -> Operation:
    """The bucket a request draws from. Only called for paths that need the API key."""
    if method == "POST":
        if path == "/v1/documents" or (
            path.startswith("/v1/documents/") and path.endswith("/retry")
        ):
            return "upload"
        if path in ("/v1/query", "/v1/query/stream"):
            return "query"
        if path == "/v1/search":
            return "search"
    return "read"


def per_minute(settings: Settings, operation: Operation) -> int:
    limits: dict[Operation, int] = {
        "upload": settings.rate_limit_upload_per_minute,
        "query": settings.rate_limit_query_per_minute,
        "search": settings.rate_limit_search_per_minute,
        "read": settings.rate_limit_read_per_minute,
        "auth_failure": settings.rate_limit_auth_failures_per_minute,
    }
    return limits[operation]


def client_key(address: str | None) -> str:
    """The address a bucket is kept for: IPv6 grouped by /64, anything else as given."""
    if address is None:
        return "unknown"
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return address  # not an IP address (a test client, a Unix socket)
    if isinstance(ip, ipaddress.IPv6Address):
        if ip.ipv4_mapped is not None:
            return str(ip.ipv4_mapped)
        return str(ipaddress.IPv6Network((ip, 64), strict=False))
    return str(ip)


@dataclass
class _Bucket:
    tokens: float
    updated: float


class RateLimiter:
    def __init__(
        self, *, clock: Callable[[], float] = time.monotonic, max_tracked: int = MAX_TRACKED
    ) -> None:
        self._clock = clock
        self._max_tracked = max_tracked
        self._buckets: OrderedDict[tuple[str, Operation], _Bucket] = OrderedDict()
        self._lock = threading.Lock()

    def wait_s(self, client: str, operation: Operation, limit: int, *, take: bool) -> float:
        """0 if the client may go ahead (taking a token when ``take``), else seconds to wait.

        ``limit`` is per minute; 0 means unlimited.
        """
        if limit <= 0:
            return 0.0
        rate = limit / 60.0
        now = self._clock()
        key = (client, operation)
        with self._lock:
            bucket = self._buckets.get(key)
            if bucket is None:
                bucket = self._buckets[key] = _Bucket(tokens=float(limit), updated=now)
                if len(self._buckets) > self._max_tracked:
                    self._buckets.popitem(last=False)
            else:
                bucket.tokens = min(float(limit), bucket.tokens + (now - bucket.updated) * rate)
                bucket.updated = now
                self._buckets.move_to_end(key)
            if bucket.tokens >= 1.0:
                if take:
                    bucket.tokens -= 1.0
                return 0.0
            return (1.0 - bucket.tokens) / rate

    @property
    def tracked(self) -> int:
        return len(self._buckets)


def retry_after_header(wait_s: float) -> str:
    return str(max(1, math.ceil(wait_s)))
