"""Async Redis client / connection pool factory."""

from __future__ import annotations

import redis.asyncio as redis

from dart.core.config import RedisSettings


def build_redis_client(settings: RedisSettings) -> redis.Redis:
    """Construct a new async Redis client backed by a bounded connection pool.

    Uses `decode_responses=True` throughout DART: every layer above this
    (job store, idempotency guard, ready queue) assumes it is working
    with `str`, not `bytes`.

    Callers own the lifecycle of the returned client — call `.aclose()`
    on shutdown (see `dart.api.app`'s lifespan handler). This function
    intentionally does not memoize a singleton; process-wide reuse is a
    concern for the caller (API lifespan, worker startup), not this
    factory.
    """
    pool = redis.ConnectionPool(
        host=settings.host,
        port=settings.port,
        db=settings.db,
        password=settings.password,
        max_connections=settings.max_connections,
        socket_connect_timeout=settings.socket_connect_timeout_seconds,
        socket_timeout=settings.socket_timeout_seconds,
        decode_responses=True,
    )
    return redis.Redis(connection_pool=pool)
