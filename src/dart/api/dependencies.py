"""FastAPI dependency providers.

Wires `Settings`, the shared Redis client (stored on `app.state` by the
lifespan handler in `dart.api.app`), and the queue-layer collaborators
(`JobStore`, `EventIngestor`, `IdempotencyGuard`, `ReadyQueue`) into
route handlers via `Depends(...)`.
"""

from __future__ import annotations

from functools import lru_cache

import redis.asyncio as redis
from fastapi import Request

from dart.core.config import Settings
from dart.queue.idempotency import IdempotencyGuard
from dart.queue.ingestion import EventIngestor
from dart.queue.job_store import JobStore
from dart.queue.ready_queue import ReadyQueue


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Process-wide `Settings` singleton.

    Cached since `Settings` is immutable after construction and
    environment variables aren't expected to change mid-process.
    """
    return Settings()


def get_redis_client(request: Request) -> redis.Redis:
    """Retrieve the shared Redis client stored on `app.state` by the
    lifespan handler in `dart.api.app.create_app`."""
    client: redis.Redis = request.app.state.redis_client
    return client


def get_job_store(request: Request) -> JobStore:
    settings = get_settings()
    return JobStore(get_redis_client(request), settings.redis)


def get_event_ingestor(request: Request) -> EventIngestor:
    """Atomic idempotency-claim + job-persist + ready-stream-enqueue,
    used by `POST /api/v1/events`. Supersedes the old sequential
    `get_idempotency_guard` + `get_job_store` + `get_ready_queue`
    combination for that specific route."""
    settings = get_settings()
    return EventIngestor(get_redis_client(request), settings.redis)


def get_idempotency_guard(request: Request) -> IdempotencyGuard:
    """Kept for any caller that wants a standalone idempotency check
    outside the atomic ingestion path (e.g. future admin tooling); no
    longer used by the ingestion route itself."""
    settings = get_settings()
    return IdempotencyGuard(
        get_redis_client(request),
        settings.redis,
        settings.idempotency_key_ttl_seconds,
    )


def get_ready_queue(request: Request) -> ReadyQueue:
    """Kept for any caller that wants to enqueue directly (e.g. a
    future manual DLQ-replay endpoint); no longer used by the ingestion
    route itself, which enqueues atomically via `EventIngestor`."""
    settings = get_settings()
    return ReadyQueue(get_redis_client(request), settings.redis)
