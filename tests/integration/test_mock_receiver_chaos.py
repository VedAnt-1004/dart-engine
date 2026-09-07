"""End-to-end tests validating `dart.testing.mock_receiver`'s chaos
behaviors integrate correctly with `AsyncDispatcher`'s classification
logic.

Runs against the real FastAPI mock app in-process via `httpx.ASGITransport`
(no real socket) — unlike `test_dispatcher_e2e.py`'s `respx`-mocked
responses, an actual ASGI application decides every response here, so
this is the closer-to-a-real-server test of the pair.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import AsyncIterator
from uuid import uuid4

import fakeredis
import httpx
import pytest
import pytest_asyncio

from dart.core.config import RedisSettings
from dart.models.task import WebhookTask
from dart.queue.dlq import DeadLetterQueue
from dart.queue.job_store import JobStore
from dart.queue.ready_queue import ReadyQueue
from dart.queue.retry_scheduler import RetryScheduler
from dart.resilience.circuit_breaker import CircuitBreaker
from dart.resilience.retry_policy import RetryPolicy
from dart.security.secrets import StaticSigningSecretResolver
from dart.testing.mock_receiver import app as mock_app
from dart.testing.mock_receiver import received_requests
from dart.worker.dispatcher import AsyncDispatcher

BASE_URL = "http://mock-receiver.test/webhook"
SECRET_ID = "secret_ref_001"


@pytest.fixture(autouse=True)
def _reset_mock_receiver_state() -> None:
    received_requests.clear()


@pytest_asyncio.fixture
async def redis_client() -> AsyncIterator[fakeredis.aioredis.FakeRedis]:
    client = fakeredis.aioredis.FakeRedis(decode_responses=True)
    yield client
    await client.aclose()


@pytest.fixture
def settings() -> RedisSettings:
    return RedisSettings()


@pytest_asyncio.fixture
async def dispatcher(
    redis_client: fakeredis.aioredis.FakeRedis, settings: RedisSettings
) -> AsyncIterator[AsyncDispatcher]:
    transport = httpx.ASGITransport(app=mock_app)
    http_client = httpx.AsyncClient(transport=transport, timeout=httpx.Timeout(2.0))
    d = AsyncDispatcher(
        client=http_client,
        signer_factory=StaticSigningSecretResolver({SECRET_ID: "whsec_test"}),
        circuit_breaker=CircuitBreaker(
            redis_client, settings, failure_threshold=3, open_cooldown_seconds=30
        ),
        retry_policy=RetryPolicy(base_seconds=0.01, max_backoff_seconds=1.0, max_attempts=3),
        job_store=JobStore(redis_client, settings),
        ready_queue=ReadyQueue(redis_client, settings),
        retry_scheduler=RetryScheduler(redis_client, settings),
        dlq=DeadLetterQueue(redis_client, settings),
    )
    yield d
    await http_client.aclose()


def _make_task(behavior: str = "success", **extra_query: float) -> WebhookTask:
    query = "&".join([f"behavior={behavior}"] + [f"{k}={v}" for k, v in extra_query.items()])
    return WebhookTask(
        task_id=uuid4(),
        event_type="invoice.paid",
        target_url=f"{BASE_URL}?{query}",
        payload={"id": "evt_1"},
        idempotency_key=f"idem_{uuid4()}",
        signing_secret_id=SECRET_ID,
        max_attempts=3,
        created_at=datetime.now(timezone.utc),
    )


class TestSuccessBehavior:
    async def test_default_success_delivers(self, dispatcher: AsyncDispatcher) -> None:
        result = await dispatcher.dispatch(_make_task("success"))
        assert result.success is True
        assert result.status_code == 200
        assert len(received_requests) == 1


class TestServerErrorBehavior:
    async def test_server_error_is_a_retryable_failure(self, dispatcher: AsyncDispatcher) -> None:
        result = await dispatcher.dispatch(_make_task("server_error"))
        assert result.success is False
        assert result.status_code == 500
        assert result.circuit_open is False


class TestClientErrorBehavior:
    async def test_client_error_is_a_non_retryable_failure(
        self, dispatcher: AsyncDispatcher
    ) -> None:
        result = await dispatcher.dispatch(_make_task("client_error"))
        assert result.success is False
        assert result.status_code == 400


class TestRateLimitBehavior:
    async def test_429_retry_after_header_is_parsed(self, dispatcher: AsyncDispatcher) -> None:
        result = await dispatcher.dispatch(_make_task("rate_limit", retry_after=37))
        assert result.status_code == 429
        assert result.retry_after_seconds == 37.0

    async def test_default_retry_after_when_unspecified(
        self, dispatcher: AsyncDispatcher
    ) -> None:
        result = await dispatcher.dispatch(_make_task("rate_limit"))
        assert result.status_code == 429
        assert result.retry_after_seconds == 2.0


class TestTimeoutBehavior:
    async def test_timeout_is_classified_as_a_transport_failure(
        self, dispatcher: AsyncDispatcher
    ) -> None:
        # The dispatcher's http_client in this fixture has a 2s timeout;
        # the mock receiver is told to sleep for 5s, guaranteeing a
        # client-side ReadTimeout classified with status_code=None.
        result = await dispatcher.dispatch(_make_task("timeout", timeout_seconds=5))
        assert result.success is False
        assert result.status_code is None
        assert result.error is not None


class TestCircuitBreakerRecordsRealOutcomes:
    async def test_repeated_server_errors_open_the_circuit(
        self, dispatcher: AsyncDispatcher
    ) -> None:
        for _ in range(3):  # matches the fixture's failure_threshold=3
            result = await dispatcher.dispatch(_make_task("server_error"))
            assert result.success is False

        blocked_result = await dispatcher.dispatch(_make_task("success"))
        # Same domain (mock-receiver.test) as the failing requests above,
        # so even a "success"-behavior task is now short-circuited.
        assert blocked_result.circuit_open is True
