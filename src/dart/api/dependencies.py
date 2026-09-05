"""FastAPI dependency providers.

Wires `Settings`, the shared Redis client (stored on `app.state` by the
lifespan handler in `dart.api.app`), and the queue-layer collaborators
(`JobStore`, `IdempotencyGuard`, `ReadyQueue`) into route handlers via
`Depends(...)`.
"""

from __future__ import annotations

from functools import lru_cache

import redis.asyncio as redis
from fastapi import Request

from dart.core.config import Settings
from dart.queue.idempotency import IdempotencyGuard
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


def get_idempotency_guard(request: Request) -> IdempotencyGuard:
    settings = get_settings()
    return IdempotencyGuard(
        get_redis_client(request),
        settings.redis,
        settings.idempotency_key_ttl_seconds,
    )


def get_ready_queue(request: Request) -> ReadyQueue:
    settings = get_settings()
    return ReadyQueue(get_redis_client(request), settings.redis)
