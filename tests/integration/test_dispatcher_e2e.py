"""End-to-end tests for `AsyncDispatcher`, using `respx` to control the
simulated HTTP responses precisely (status codes, headers, sequences)
without needing a live receiver.

Exercises the full ingest -> dispatch -> {retry | DLQ} -> ... pipeline
against fakeredis for the queue/state layer. Companion file
`test_mock_receiver_chaos.py` covers the same dispatcher against the
*real* `dart.testing.mock_receiver` app instead of respx-mocked
responses.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import AsyncIterator
from uuid import uuid4

import fakeredis
import httpx
import pytest
import pytest_asyncio
import respx

from dart.core.config import RedisSettings
from dart.models.task import TaskStatus, WebhookTask
from dart.queue.dlq import DeadLetterQueue
from dart.queue.job_store import JobStore
from dart.queue.ready_queue import ReadyQueue
from dart.queue.retry_scheduler import RetryScheduler
from dart.resilience.circuit_breaker import CircuitBreaker
from dart.resilience.retry_policy import RetryPolicy
from dart.security.secrets import StaticSigningSecretResolver
from dart.worker.dispatcher import AsyncDispatcher

TARGET_URL = "https://webhooks.example.com/receive"
SECRET_ID = "secret_ref_001"


@pytest_asyncio.fixture
async def redis_client() -> AsyncIterator[fakeredis.aioredis.FakeRedis]:
    client = fakeredis.aioredis.FakeRedis(decode_responses=True)
    yield client
    await client.aclose()


@pytest.fixture
def settings() -> RedisSettings:
    return RedisSettings()


@pytest_asyncio.fixture
async def job_store(redis_client: fakeredis.aioredis.FakeRedis, settings: RedisSettings) -> JobStore:
    return JobStore(redis_client, settings)


@pytest_asyncio.fixture
async def ready_queue(
    redis_client: fakeredis.aioredis.FakeRedis, settings: RedisSettings
) -> ReadyQueue:
    queue = ReadyQueue(redis_client, settings)
    await queue.ensure_consumer_group()
    return queue


@pytest_asyncio.fixture
async def retry_scheduler(
    redis_client: fakeredis.aioredis.FakeRedis, settings: RedisSettings
) -> RetryScheduler:
    return RetryScheduler(redis_client, settings)


@pytest_asyncio.fixture
async def dlq(redis_client: fakeredis.aioredis.FakeRedis, settings: RedisSettings) -> DeadLetterQueue:
    return DeadLetterQueue(redis_client, settings)


@pytest_asyncio.fixture
async def circuit_breaker(
    redis_client: fakeredis.aioredis.FakeRedis, settings: RedisSettings
) -> CircuitBreaker:
    return CircuitBreaker(redis_client, settings, failure_threshold=3, open_cooldown_seconds=30)


@pytest_asyncio.fixture
async def dispatcher(
    job_store: JobStore,
    ready_queue: ReadyQueue,
    retry_scheduler: RetryScheduler,
    dlq: DeadLetterQueue,
    circuit_breaker: CircuitBreaker,
) -> AsyncIterator[AsyncDispatcher]:
    http_client = httpx.AsyncClient()
    d = AsyncDispatcher(
        client=http_client,
        signer_factory=StaticSigningSecretResolver({SECRET_ID: "whsec_test"}),
        circuit_breaker=circuit_breaker,
        retry_policy=RetryPolicy(base_seconds=0.01, max_backoff_seconds=1.0, max_attempts=3),
        job_store=job_store,
        ready_queue=ready_queue,
        retry_scheduler=retry_scheduler,
        dlq=dlq,
    )
    yield d
    await http_client.aclose()


def _make_task(**overrides: object) -> WebhookTask:
    kwargs: dict[str, object] = dict(
        task_id=uuid4(),
        event_type="invoice.paid",
        target_url=TARGET_URL,
        payload={"id": "evt_1", "amount": 100},
        idempotency_key=f"idem_{uuid4()}",
        signing_secret_id=SECRET_ID,
        max_attempts=3,
        created_at=datetime.now(timezone.utc),
    )
    kwargs.update(overrides)
    return WebhookTask(**kwargs)  # type: ignore[arg-type]


class TestSuccessfulDelivery:
    @respx.mock
    async def test_200_response_marks_delivered(
        self, dispatcher: AsyncDispatcher, job_store: JobStore
    ) -> None:
        respx.post(TARGET_URL).mock(return_value=httpx.Response(200))
        task = _make_task()
        await job_store.save(task)

        result = await dispatcher.dispatch(task)
        assert result.success is True
        assert result.status_code == 200

        final_status = await dispatcher.handle_outcome(task, result)
        assert final_status == TaskStatus.DELIVERED

        stored = await job_store.get(task.task_id)
        assert stored is not None
        assert stored.status == TaskStatus.DELIVERED

    @respx.mock
    async def test_signature_header_is_sent_and_well_formed(
        self, dispatcher: AsyncDispatcher, job_store: JobStore
    ) -> None:
        route = respx.post(TARGET_URL).mock(return_value=httpx.Response(200))
        task = _make_task()
        await job_store.save(task)
        await dispatcher.dispatch(task)

        sent_request = route.calls.last.request
        assert "X-DART-Signature" in sent_request.headers
        assert sent_request.headers["X-DART-Signature"].startswith("t=")
        assert ",v1=" in sent_request.headers["X-DART-Signature"]


class TestRetryThenSuccess:
    @respx.mock
    async def test_500_then_200_ends_delivered_via_retry_scheduling(
        self,
        dispatcher: AsyncDispatcher,
        job_store: JobStore,
        retry_scheduler: RetryScheduler,
    ) -> None:
        respx.post(TARGET_URL).mock(side_effect=[httpx.Response(500), httpx.Response(200)])
        task = _make_task()
        await job_store.save(task)

        # First attempt: 500 -> retry scheduled.
        result1 = await dispatcher.dispatch(task)
        assert result1.success is False
        assert result1.status_code == 500
        status1 = await dispatcher.handle_outcome(task, result1)
        assert status1 == TaskStatus.RETRY_SCHEDULED

        stored = await job_store.get(task.task_id)
        assert stored is not None
        assert stored.attempt_count == 1
        assert stored.next_attempt_at is not None
        assert await retry_scheduler.count_pending() == 1

        # Second attempt, simulating the scheduler having promoted it and
        # the worker having re-fetched + transitioned it to IN_FLIGHT.
        stored.transition_to(TaskStatus.IN_FLIGHT)
        result2 = await dispatcher.dispatch(stored)
        assert result2.success is True
        status2 = await dispatcher.handle_outcome(stored, result2)
        assert status2 == TaskStatus.DELIVERED


class TestImmediateDLQ:
    @respx.mock
    async def test_400_response_routes_directly_to_dlq(
        self, dispatcher: AsyncDispatcher, job_store: JobStore, dlq: DeadLetterQueue
    ) -> None:
        respx.post(TARGET_URL).mock(return_value=httpx.Response(400))
        task = _make_task()
        await job_store.save(task)

        result = await dispatcher.dispatch(task)
        assert result.success is False
        assert result.status_code == 400

        final_status = await dispatcher.handle_outcome(task, result)
        assert final_status == TaskStatus.DEAD_LETTERED

        entries = await dlq.list_recent()
        assert any(e["task_id"] == str(task.task_id) for e in entries)


class TestRetryExhaustion:
    @respx.mock
    async def test_repeated_500s_eventually_dlq_at_max_attempts(
        self, dispatcher: AsyncDispatcher, job_store: JobStore
    ) -> None:
        respx.post(TARGET_URL).mock(return_value=httpx.Response(500))
        task = _make_task(max_attempts=3)
        await job_store.save(task)

        statuses = []
        for _ in range(3):
            result = await dispatcher.dispatch(task)
            status = await dispatcher.handle_outcome(task, result)
            statuses.append(status)
            if status == TaskStatus.RETRY_SCHEDULED:
                task.transition_to(TaskStatus.IN_FLIGHT)

        assert statuses == [
            TaskStatus.RETRY_SCHEDULED,
            TaskStatus.RETRY_SCHEDULED,
            TaskStatus.DEAD_LETTERED,
        ]


class TestRetryAfterOverride:
    @respx.mock
    async def test_429_retry_after_overrides_computed_backoff(
        self, dispatcher: AsyncDispatcher, job_store: JobStore
    ) -> None:
        respx.post(TARGET_URL).mock(
            return_value=httpx.Response(429, headers={"Retry-After": "45"})
        )
        task = _make_task()
        await job_store.save(task)

        result = await dispatcher.dispatch(task)
        assert result.retry_after_seconds == 45.0

        before = datetime.now(timezone.utc)
        await dispatcher.handle_outcome(task, result)

        stored = await job_store.get(task.task_id)
        assert stored is not None
        assert stored.next_attempt_at is not None
        delta_seconds = (stored.next_attempt_at - before).total_seconds()
        # ~45s from the Retry-After header, not the tiny (<=0.02s)
        # computed-jitter backoff the fixture's RetryPolicy would give.
        assert 44.0 <= delta_seconds <= 46.0


class TestCircuitBreakerIsolation:
    @respx.mock
    async def test_sustained_failures_open_circuit_for_that_domain_only(
        self, dispatcher: AsyncDispatcher, job_store: JobStore
    ) -> None:
        respx.post("https://failing.example.com/hook").mock(return_value=httpx.Response(500))
        respx.post("https://healthy.example.com/hook").mock(return_value=httpx.Response(200))

        failing_task = _make_task(target_url="https://failing.example.com/hook")
        for _ in range(3):  # matches the circuit_breaker fixture's failure_threshold=3
            result = await dispatcher.dispatch(failing_task)
            assert result.success is False

        # The failing domain's circuit should now be open, short-circuiting
        # further attempts without even reaching the HTTP layer.
        failing_result = await dispatcher.dispatch(failing_task)
        assert failing_result.circuit_open is True

        # A different domain is entirely unaffected.
        healthy_task = _make_task(target_url="https://healthy.example.com/hook")
        healthy_result = await dispatcher.dispatch(healthy_task)
        assert healthy_result.success is True
        assert healthy_result.circuit_open is False


class TestCrashRecovery:
    """Exit criterion: a worker crashing mid-dispatch doesn't lose the
    task — `claim_stale` (XAUTOCLAIM) recovers it for another worker.

    This validates the `ReadyQueue`/`AsyncDispatcher` mechanics directly;
    it is NOT the full docker-compose black-box scenario from the
    roadmap's exit criteria (real separate worker processes, real
    Redis) — that requires actual infrastructure this test environment
    doesn't have. It's the closest automated proxy: simulate a crash by
    reading an entry into a consumer's PEL and simply never acking it,
    then confirm a second consumer's `claim_stale` picks it up.
    """

    @respx.mock
    async def test_entry_read_but_never_acked_is_reclaimed_by_another_consumer(
        self,
        dispatcher: AsyncDispatcher,
        job_store: JobStore,
        ready_queue: ReadyQueue,
    ) -> None:
        respx.post(TARGET_URL).mock(return_value=httpx.Response(200))
        task = _make_task()
        await job_store.save(task)
        await ready_queue.enqueue(task.task_id)

        # "Crashed worker": reads the entry (creating a PEL entry under
        # consumer-a) but never acks it — simulating a process death
        # between read and ack.
        entries = await ready_queue.read_new("consumer-a", count=10, block_ms=100)
        assert len(entries) == 1
        crashed_entry_id, crashed_task_id = entries[0]
        assert crashed_task_id == task.task_id

        # A second read by the same or another consumer sees nothing new
        # — the entry is "stuck" in consumer-a's PEL, not lost, but also
        # not visible to a fresh XREADGROUP read.
        further_new_entries = await ready_queue.read_new("consumer-b", count=10, block_ms=100)
        assert further_new_entries == []

        # A healthy consumer reclaims it via claim_stale (min_idle_ms=0
        # so it doesn't need to actually wait out a real idle window).
        reclaimed = await ready_queue.claim_stale("consumer-b", min_idle_ms=0, count=10)
        assert reclaimed == [(crashed_entry_id, task.task_id)]

        # The reclaiming consumer can now process and ack it normally.
        result = await dispatcher.dispatch(task)
        assert result.success is True
        await dispatcher.handle_outcome(task, result)
        await ready_queue.ack(crashed_entry_id)

        stored = await job_store.get(task.task_id)
        assert stored is not None
        assert stored.status == TaskStatus.DELIVERED
