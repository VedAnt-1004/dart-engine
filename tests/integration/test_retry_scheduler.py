"""Integration tests for `dart.queue.retry_scheduler.RetryScheduler`.

Exit criterion: the scheduler promotes only due jobs, and does so
without double-delivery even under concurrent callers.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import AsyncIterator
from uuid import uuid4

import fakeredis
import pytest
import pytest_asyncio

from dart.core.config import RedisSettings
from dart.queue.retry_scheduler import RetryScheduler

EPOCH = datetime(2025, 1, 1, tzinfo=timezone.utc)


def _at(offset_seconds: float) -> datetime:
    return EPOCH + timedelta(seconds=offset_seconds)


@pytest_asyncio.fixture
async def redis_client() -> AsyncIterator[fakeredis.aioredis.FakeRedis]:
    client = fakeredis.aioredis.FakeRedis(decode_responses=True)
    yield client
    await client.aclose()


@pytest.fixture
def settings() -> RedisSettings:
    return RedisSettings()


@pytest_asyncio.fixture
async def scheduler(
    redis_client: fakeredis.aioredis.FakeRedis, settings: RedisSettings
) -> RetryScheduler:
    return RetryScheduler(redis_client, settings)


class TestSchedule:
    async def test_schedule_adds_a_pending_member(
        self, scheduler: RetryScheduler, redis_client: fakeredis.aioredis.FakeRedis
    ) -> None:
        task_id = uuid4()
        await scheduler.schedule(task_id, _at(100))
        score = await redis_client.zscore("dart:zset:retry", str(task_id))
        assert score == _at(100).timestamp()

    async def test_count_pending_reflects_scheduled_members(
        self, scheduler: RetryScheduler
    ) -> None:
        assert await scheduler.count_pending() == 0
        await scheduler.schedule(uuid4(), _at(100))
        await scheduler.schedule(uuid4(), _at(200))
        assert await scheduler.count_pending() == 2


class TestPromoteDue:
    async def test_promotes_only_members_whose_time_has_passed(
        self, scheduler: RetryScheduler, redis_client: fakeredis.aioredis.FakeRedis
    ) -> None:
        due_task = uuid4()
        not_due_task = uuid4()
        await scheduler.schedule(due_task, _at(100))
        await scheduler.schedule(not_due_task, _at(999))

        promoted = await scheduler.promote_due(now=_at(150))

        assert promoted == [due_task]
        assert await scheduler.count_pending() == 1
        assert await redis_client.zscore("dart:zset:retry", str(not_due_task)) == _at(999).timestamp()

    async def test_promoted_task_appears_on_the_ready_stream(
        self, scheduler: RetryScheduler, redis_client: fakeredis.aioredis.FakeRedis
    ) -> None:
        task_id = uuid4()
        await scheduler.schedule(task_id, _at(100))

        await scheduler.promote_due(now=_at(150))

        entries = await redis_client.xrange("dart:queue:ready")
        assert any(fields.get("task_id") == str(task_id) for _, fields in entries)

    async def test_exactly_at_due_boundary_is_promoted(
        self, scheduler: RetryScheduler
    ) -> None:
        task_id = uuid4()
        await scheduler.schedule(task_id, _at(100))
        promoted = await scheduler.promote_due(now=_at(100))  # score == now
        assert promoted == [task_id]

    async def test_one_second_before_due_is_not_promoted(
        self, scheduler: RetryScheduler
    ) -> None:
        task_id = uuid4()
        await scheduler.schedule(task_id, _at(100))
        promoted = await scheduler.promote_due(now=_at(99))
        assert promoted == []

    async def test_empty_zset_promotes_nothing_without_error(
        self, scheduler: RetryScheduler
    ) -> None:
        assert await scheduler.promote_due(now=_at(1000)) == []

    async def test_batch_size_limits_promotions_per_call(
        self, scheduler: RetryScheduler
    ) -> None:
        for _ in range(10):
            await scheduler.schedule(uuid4(), _at(50))

        first_batch = await scheduler.promote_due(now=_at(100), batch_size=4)
        assert len(first_batch) == 4
        assert await scheduler.count_pending() == 6

        second_batch = await scheduler.promote_due(now=_at(100), batch_size=4)
        assert len(second_batch) == 4
        assert await scheduler.count_pending() == 2

    async def test_second_call_after_full_promotion_returns_empty(
        self, scheduler: RetryScheduler
    ) -> None:
        await scheduler.schedule(uuid4(), _at(50))
        await scheduler.schedule(uuid4(), _at(60))

        first = await scheduler.promote_due(now=_at(100))
        second = await scheduler.promote_due(now=_at(100))

        assert len(first) == 2
        assert second == []


class TestConcurrentPromotion:
    """Exit criterion: no double-delivery under concurrent scheduler
    replicas polling the same due set."""

    async def test_concurrent_promote_due_calls_do_not_double_deliver(
        self, scheduler: RetryScheduler, redis_client: fakeredis.aioredis.FakeRedis
    ) -> None:
        task_ids = [uuid4() for _ in range(20)]
        for task_id in task_ids:
            await scheduler.schedule(task_id, _at(50))

        # Simulate two concurrent scheduler replicas racing the same due set.
        results = await asyncio.gather(
            scheduler.promote_due(now=_at(100), batch_size=100),
            scheduler.promote_due(now=_at(100), batch_size=100),
        )

        all_promoted = results[0] + results[1]
        assert sorted(all_promoted, key=str) == sorted(task_ids, key=str)
        assert len(all_promoted) == len(set(all_promoted))  # no duplicates

        entries = await redis_client.xrange("dart:queue:ready")
        stream_task_ids = [fields.get("task_id") for _, fields in entries]
        # Every task_id appears on the stream exactly once.
        for task_id in task_ids:
            assert stream_task_ids.count(str(task_id)) == 1
