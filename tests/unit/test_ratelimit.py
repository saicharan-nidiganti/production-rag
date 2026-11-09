"""Per-client token buckets (issue #30): bursts, refill, bounded memory and client keys."""

from __future__ import annotations

import threading

import pytest

from production_rag.api.ratelimit import (
    Operation,
    RateLimiter,
    client_key,
    operation_for,
    per_minute,
    retry_after_header,
)
from production_rag.config import Settings


class _Clock:
    """A monotonic clock that moves only when a test moves it."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clock() -> _Clock:
    return _Clock()


def test_a_client_may_burst_up_to_one_minutes_allowance(clock: _Clock) -> None:
    limiter = RateLimiter(clock=clock)
    assert [limiter.wait_s("a", "query", 3, take=True) for _ in range(3)] == [0.0, 0.0, 0.0]
    # Out of tokens; at 3 a minute the next one arrives in 20 s.
    assert limiter.wait_s("a", "query", 3, take=True) == pytest.approx(20.0)


def test_the_allowance_refills_continuously(clock: _Clock) -> None:
    limiter = RateLimiter(clock=clock)
    for _ in range(6):
        limiter.wait_s("a", "read", 6, take=True)
    clock.now += 5.0  # half a token at 6 a minute
    assert limiter.wait_s("a", "read", 6, take=True) == pytest.approx(5.0)
    clock.now += 5.5
    assert limiter.wait_s("a", "read", 6, take=True) == 0.0
    assert limiter.wait_s("a", "read", 6, take=True) == pytest.approx(9.5)


def test_an_idle_client_saves_up_no_more_than_one_minute(clock: _Clock) -> None:
    limiter = RateLimiter(clock=clock)
    limiter.wait_s("a", "upload", 2, take=True)
    clock.now += 3600.0
    granted = [limiter.wait_s("a", "upload", 2, take=True) == 0.0 for _ in range(3)]
    assert granted == [True, True, False]


def test_checking_without_taking_leaves_the_allowance(clock: _Clock) -> None:
    limiter = RateLimiter(clock=clock)
    assert all(limiter.wait_s("a", "auth_failure", 1, take=False) == 0.0 for _ in range(5))
    assert limiter.wait_s("a", "auth_failure", 1, take=True) == 0.0
    assert limiter.wait_s("a", "auth_failure", 1, take=False) > 0


def test_clients_and_operations_have_separate_allowances(clock: _Clock) -> None:
    limiter = RateLimiter(clock=clock)
    assert limiter.wait_s("a", "query", 1, take=True) == 0.0
    assert limiter.wait_s("a", "query", 1, take=True) > 0
    assert limiter.wait_s("b", "query", 1, take=True) == 0.0
    assert limiter.wait_s("a", "search", 1, take=True) == 0.0


def test_a_limit_of_zero_turns_the_limit_off(clock: _Clock) -> None:
    limiter = RateLimiter(clock=clock)
    assert all(limiter.wait_s("a", "read", 0, take=True) == 0.0 for _ in range(1000))
    assert limiter.tracked == 0


def test_a_lowered_limit_applies_at_once(clock: _Clock) -> None:
    limiter = RateLimiter(clock=clock)
    limiter.wait_s("a", "read", 100, take=True)  # 99 tokens left
    assert limiter.wait_s("a", "read", 1, take=True) == 0.0  # capped to 1, then taken
    assert limiter.wait_s("a", "read", 1, take=True) > 0


def test_memory_is_bounded_by_forgetting_the_least_recently_seen(clock: _Clock) -> None:
    limiter = RateLimiter(clock=clock, max_tracked=2)
    limiter.wait_s("a", "read", 1, take=True)
    limiter.wait_s("b", "read", 1, take=True)
    limiter.wait_s("a", "read", 1, take=True)  # a seen again, so b is now the oldest
    limiter.wait_s("c", "read", 1, take=True)  # over the cap: b is forgotten
    assert limiter.tracked == 2
    assert limiter.wait_s("a", "read", 1, take=True) > 0  # kept, still out of tokens
    assert limiter.wait_s("b", "read", 1, take=True) == 0.0  # forgotten: a fresh allowance


def test_concurrent_requests_never_get_more_than_the_allowance(clock: _Clock) -> None:
    limiter = RateLimiter(clock=clock)
    granted = [0] * 8

    def worker(index: int) -> None:
        granted[index] = sum(limiter.wait_s("a", "read", 500, take=True) == 0.0 for _ in range(100))

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sum(granted) == 500


@pytest.mark.parametrize(
    ("address", "key"),
    [
        ("203.0.113.7", "203.0.113.7"),
        ("2001:db8:1:2:aaaa:bbbb:cccc:dddd", "2001:db8:1:2::/64"),
        ("2001:db8:1:2::1", "2001:db8:1:2::/64"),  # same /64: one allowance
        ("::ffff:203.0.113.7", "203.0.113.7"),  # IPv4-mapped counts as the IPv4 client
        ("testclient", "testclient"),
        (None, "unknown"),
    ],
)
def test_client_keys(address: str | None, key: str) -> None:
    assert client_key(address) == key


@pytest.mark.parametrize(
    ("method", "path", "operation"),
    [
        ("POST", "/v1/documents", "upload"),
        ("POST", "/v1/documents/42/retry", "upload"),
        ("POST", "/v1/query", "query"),
        ("POST", "/v1/search", "search"),
        ("GET", "/v1/documents", "read"),
        ("GET", "/v1/documents/42", "read"),
        ("DELETE", "/v1/documents/42", "read"),
        ("GET", "/v1/query", "read"),
    ],
)
def test_each_request_draws_from_its_operations_allowance(
    method: str, path: str, operation: Operation
) -> None:
    assert operation_for(method, path) == operation


def test_each_operation_reads_its_own_setting(settings: Settings) -> None:
    settings.rate_limit_upload_per_minute = 1
    settings.rate_limit_query_per_minute = 2
    settings.rate_limit_search_per_minute = 3
    settings.rate_limit_read_per_minute = 4
    settings.rate_limit_auth_failures_per_minute = 5
    operations: list[Operation] = ["upload", "query", "search", "read", "auth_failure"]
    assert [per_minute(settings, op) for op in operations] == [1, 2, 3, 4, 5]


@pytest.mark.parametrize(("wait_s", "header"), [(0.01, "1"), (1.0, "1"), (1.2, "2"), (29.5, "30")])
def test_retry_after_is_whole_seconds_rounded_up(wait_s: float, header: str) -> None:
    assert retry_after_header(wait_s) == header
