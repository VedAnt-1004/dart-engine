"""Dispatch worker process entrypoint, registered as the `dart-worker`
console script.

Wires a real `httpx.AsyncClient`, Redis client, and all resilience/queue
collaborators into an `AsyncDispatcher`, then drives its worker loop.
Imports nothing from `dart.api` — decoupled from and deployed
independently of the ingestion API, per the approved architecture.
"""

from __future__ import annotations

import asyncio
import signal
import socket
import uuid

import httpx
import redis.asyncio as redis

from dart.core.config import Settings
from dart.core.logging import configure_logging, get_logger
from dart.queue.dlq import DeadLetterQueue
from dart.queue.job_store import JobStore
from dart.queue.ready_queue import ReadyQueue
from dart.queue.redis_client import build_redis_client
from dart.queue.retry_scheduler import RetryScheduler
from dart.resilience.circuit_breaker import CircuitBreaker
from dart.resilience.retry_policy import RetryPolicy
from dart.security.secrets import EnvSigningSecretResolver
from dart.worker.dispatcher import AsyncDispatcher

logger = get_logger(__name__)


def _build_http_client(settings: Settings) -> httpx.AsyncClient:
    """A long-lived, connection-pooled client with granular transport
    timeouts — the Slowloris mitigation from the architecture spec."""
    http_settings = settings.http_client
    timeout = httpx.Timeout(
        connect=http_settings.connect_timeout_seconds,
        read=http_settings.read_timeout_seconds,
        write=http_settings.write_timeout_seconds,
        pool=http_settings.pool_timeout_seconds,
    )
    limits = httpx.Limits(
        max_connections=http_settings.max_connections,
        max_keepalive_connections=http_settings.max_keepalive_connections,
    )
    return httpx.AsyncClient(
        timeout=timeout,
        limits=limits,
        headers={"User-Agent": http_settings.user_agent},
    )


def _build_dispatcher(
    settings: Settings,
    redis_client: redis.Redis,
    http_client: httpx.AsyncClient,
    ready_queue: ReadyQueue,
) -> AsyncDispatcher:
    return AsyncDispatcher(
        client=http_client,
        signer_factory=EnvSigningSecretResolver(),
        circuit_breaker=CircuitBreaker(
            redis_client,
            settings.redis,
            failure_threshold=settings.circuit_breaker.failure_threshold,
            open_cooldown_seconds=settings.circuit_breaker.open_cooldown_seconds,
            half_open_probe_count=settings.circuit_breaker.half_open_probe_count,
        ),
        retry_policy=RetryPolicy(
            base_seconds=settings.retry.base_seconds,
            max_backoff_seconds=settings.retry.max_backoff_seconds,
            max_attempts=settings.retry.max_attempts,
        ),
        job_store=JobStore(redis_client, settings.redis),
        ready_queue=ready_queue,
        retry_scheduler=RetryScheduler(redis_client, settings.redis),
        dlq=DeadLetterQueue(redis_client, settings.redis),
    )


async def _run(settings: Settings, consumer_name: str) -> None:
    redis_client = build_redis_client(settings.redis)
    http_client = _build_http_client(settings)

    ready_queue = ReadyQueue(redis_client, settings.redis)
    await ready_queue.ensure_consumer_group()

    dispatcher = _build_dispatcher(settings, redis_client, http_client, ready_queue)

    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop_event.set)

    logger.info("dart-worker starting", extra={"consumer_name": consumer_name})
    try:
        await dispatcher.run_worker_loop(
            consumer_name,
            batch_size=settings.worker.batch_size,
            block_ms=settings.worker.block_ms,
            stale_min_idle_ms=settings.worker.stale_min_idle_ms,
            stop_event=stop_event,
        )
    finally:
        await http_client.aclose()
        await redis_client.aclose()
        logger.info("dart-worker stopped")


def main() -> None:
    """Synchronous entrypoint registered as the `dart-worker` console script."""
    configure_logging()
    settings = Settings()
    consumer_name = f"{socket.gethostname()}-{uuid.uuid4().hex[:8]}"
    asyncio.run(_run(settings, consumer_name))


if __name__ == "__main__":
    main()
