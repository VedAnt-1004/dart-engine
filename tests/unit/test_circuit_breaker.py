"""Unit tests for `dart.resilience.circuit_breaker.CircuitBreaker`.

Uses fakeredis (async) as a Redis test double, so the breaker's
Lua-scripted atomic transitions run exactly as they would against real
Redis — this is not a mock of the breaker's logic, it exercises the real
`CircuitBreaker` class end to end against an in-memory Redis engine.
"""

from __future__ import annotations

import asyncio
from typing import AsyncIterator

import fakeredis
import pytest
import pytest_asyncio

from dart.core.config import RedisSettings
from dart.models.circuit import CircuitState
from dart.resilience.circuit_breaker import CircuitBreaker

DOMAIN = "api.example.com"


@pytest_asyncio.fixture
async def redis_client() -> AsyncIterator[fakeredis.aioredis.FakeRedis]:
    client = fakeredis.aioredis.FakeRedis(decode_responses=True)
    yield client
    await client.aclose()


@pytest.fixture
def settings() -> RedisSettings:
    return RedisSettings()


@pytest_asyncio.fixture
async def breaker(
    redis_client: fakeredis.aioredis.FakeRedis, settings: RedisSettings
) -> CircuitBreaker:
    return CircuitBreaker(
        redis_client,
        settings,
        failure_threshold=5,
        open_cooldown_seconds=30,
        half_open_probe_count=3,
    )


class TestInit:
    def test_rejects_failure_threshold_below_one(
        self, redis_client: fakeredis.aioredis.FakeRedis, settings: RedisSettings
    ) -> None:
        with pytest.raises(ValueError):
            CircuitBreaker(redis_client, settings, failure_threshold=0)

    def test_rejects_open_cooldown_below_one(
        self, redis_client: fakeredis.aioredis.FakeRedis, settings: RedisSettings
    ) -> None:
        with pytest.raises(ValueError):
            CircuitBreaker(redis_client, settings, open_cooldown_seconds=0)

    def test_rejects_half_open_probe_count_below_one(
        self, redis_client: fakeredis.aioredis.FakeRedis, settings: RedisSettings
    ) -> None:
        with pytest.raises(ValueError):
            CircuitBreaker(redis_client, settings, half_open_probe_count=0)


class TestInitialState:
    async def test_unseen_domain_defaults_to_closed(self, breaker: CircuitBreaker) -> None:
        assert await breaker.get_state(DOMAIN) == CircuitState.CLOSED

    async def test_unseen_domain_allows_requests(self, breaker: CircuitBreaker) -> None:
        assert await breaker.allow_request(DOMAIN) is True


class TestClosedToOpen:
    async def test_transitions_to_open_at_exactly_the_threshold(
        self, breaker: CircuitBreaker
    ) -> None:
        for _ in range(4):
            state = await breaker.record_failure(DOMAIN, now=1000.0)
            assert state == CircuitState.CLOSED
        state = await breaker.record_failure(DOMAIN, now=1000.0)
        assert state == CircuitState.OPEN

    async def test_success_resets_failure_streak(self, breaker: CircuitBreaker) -> None:
        await breaker.record_failure(DOMAIN, now=1000.0)
        await breaker.record_failure(DOMAIN, now=1000.0)
        await breaker.record_success(DOMAIN)

        # Should take a full fresh threshold's worth of failures to open.
        for _ in range(4):
            state = await breaker.record_failure(DOMAIN, now=1000.0)
            assert state == CircuitState.CLOSED
        state = await breaker.record_failure(DOMAIN, now=1000.0)
        assert state == CircuitState.OPEN

    async def test_allow_request_true_while_closed(self, breaker: CircuitBreaker) -> None:
        await breaker.record_failure(DOMAIN, now=1000.0)
        assert await breaker.allow_request(DOMAIN, now=1000.0) is True


class TestOpenState:
    async def test_denies_requests_before_cooldown_elapses(
        self, breaker: CircuitBreaker
    ) -> None:
        for _ in range(5):
            await breaker.record_failure(DOMAIN, now=1000.0)
        assert await breaker.get_state(DOMAIN) == CircuitState.OPEN
        assert await breaker.allow_request(DOMAIN, now=1010.0) is False
        assert await breaker.allow_request(DOMAIN, now=1029.9) is False

    async def test_transitions_to_half_open_exactly_at_cooldown_boundary(
        self, breaker: CircuitBreaker
    ) -> None:
        for _ in range(5):
            await breaker.record_failure(DOMAIN, now=1000.0)
        assert await breaker.allow_request(DOMAIN, now=1030.0) is True  # 1000 + 30
        assert await breaker.get_state(DOMAIN) == CircuitState.HALF_OPEN

    async def test_stray_success_while_open_closes_circuit(
        self, breaker: CircuitBreaker
    ) -> None:
        for _ in range(5):
            await breaker.record_failure(DOMAIN, now=1000.0)
        state = await breaker.record_success(DOMAIN)
        assert state == CircuitState.CLOSED


class TestHalfOpenState:
    @staticmethod
    async def _open_then_half_open(breaker: CircuitBreaker) -> None:
        for _ in range(5):
            await breaker.record_failure(DOMAIN, now=1000.0)
        assert await breaker.allow_request(DOMAIN, now=1030.0) is True  # -> HALF_OPEN, probe 1

    async def test_limits_total_probes_issued(self, breaker: CircuitBreaker) -> None:
        await self._open_then_half_open(breaker)
        assert await breaker.allow_request(DOMAIN, now=1030.0) is True  # probe 2
        assert await breaker.allow_request(DOMAIN, now=1030.0) is True  # probe 3
        assert await breaker.allow_request(DOMAIN, now=1030.0) is False  # 4th denied

    async def test_enough_successes_closes_the_circuit(
        self, breaker: CircuitBreaker
    ) -> None:
        await self._open_then_half_open(breaker)
        assert await breaker.record_success(DOMAIN) == CircuitState.HALF_OPEN
        assert await breaker.record_success(DOMAIN) == CircuitState.HALF_OPEN
        assert await breaker.record_success(DOMAIN) == CircuitState.CLOSED

    async def test_single_failure_immediately_reopens(
        self, breaker: CircuitBreaker
    ) -> None:
        await self._open_then_half_open(breaker)
        await breaker.record_success(DOMAIN)  # 1 of 3 successes so far
        state = await breaker.record_failure(DOMAIN, now=1031.0)
        assert state == CircuitState.OPEN

    async def test_reopening_resets_cooldown_from_the_new_failure_time(
        self, breaker: CircuitBreaker
    ) -> None:
        await self._open_then_half_open(breaker)
        await breaker.record_failure(DOMAIN, now=1031.0)
        assert await breaker.allow_request(DOMAIN, now=1032.0) is False
        assert await breaker.allow_request(DOMAIN, now=1061.0) is True  # 1031 + 30


class TestConcurrentAccess:
    """Exit criterion: the circuit breaker survives concurrent-access
    without lost updates or double transitions."""

    async def test_parallel_record_failure_converges_to_correct_state(
        self, redis_client: fakeredis.aioredis.FakeRedis, settings: RedisSettings
    ) -> None:
        breaker = CircuitBreaker(
            redis_client, settings, failure_threshold=50, open_cooldown_seconds=30
        )
        results = await asyncio.gather(
            *[breaker.record_failure(DOMAIN, now=1000.0) for _ in range(50)]
        )
        assert results.count(CircuitState.OPEN) == 1
        assert results.count(CircuitState.CLOSED) == 49
        assert await breaker.get_state(DOMAIN) == CircuitState.OPEN

    async def test_concurrent_overshoot_does_not_corrupt_state(
        self, redis_client: fakeredis.aioredis.FakeRedis, settings: RedisSettings
    ) -> None:
        """More concurrent failures than the threshold: the circuit
        should open exactly once (at the call that crosses the
        threshold) and every subsequent call simply echoes OPEN without
        re-incrementing or re-arming failure_count."""
        breaker = CircuitBreaker(
            redis_client, settings, failure_threshold=10, open_cooldown_seconds=30
        )
        results = await asyncio.gather(
            *[breaker.record_failure(DOMAIN, now=1000.0) for _ in range(30)]
        )
        assert results.count(CircuitState.CLOSED) == 9
        assert results.count(CircuitState.OPEN) == 21
        assert await breaker.get_state(DOMAIN) == CircuitState.OPEN

    async def test_parallel_calls_across_different_domains_are_isolated(
        self, redis_client: fakeredis.aioredis.FakeRedis, settings: RedisSettings
    ) -> None:
        breaker = CircuitBreaker(
            redis_client, settings, failure_threshold=3, open_cooldown_seconds=30
        )
        await asyncio.gather(
            *[breaker.record_failure("a.example.com", now=1000.0) for _ in range(3)]
        )
        assert await breaker.get_state("a.example.com") == CircuitState.OPEN
        assert await breaker.get_state("b.example.com") == CircuitState.CLOSED
