"""Retry scheduler process entrypoint, registered as the `dart-scheduler`
console script.

Continuously promotes due members of `dart:zset:retry` onto
`dart:queue:ready`. Intentionally imports nothing from `dart.api` — this
process is decoupled from and deployed independently of the ingestion
API, per the approved architecture.
"""

from __future__ import annotations

import asyncio
import signal

from dart.core.config import Settings
from dart.core.logging import configure_logging, get_logger
from dart.queue.redis_client import build_redis_client
from dart.queue.retry_scheduler import RetryScheduler

logger = get_logger(__name__)


async def run_scheduler_loop(
    scheduler: RetryScheduler,
    poll_interval_seconds: float = 1.0,
    batch_size: int = 100,
    stop_event: asyncio.Event | None = None,
) -> None:
    """Continuously promote due retries until `stop_event` is set.

    Exposed as a standalone coroutine (rather than inlined into `main`)
    so it can be driven directly in tests with a short-lived
    `stop_event`, without needing to spin up the full process/signal
    handling in `main`.
    """
    event = stop_event if stop_event is not None else asyncio.Event()

    while not event.is_set():
        promoted = await scheduler.promote_due(batch_size=batch_size)
        if promoted:
            logger.info(
                "Promoted due retries to ready queue",
                extra={"promoted_count": len(promoted)},
            )
        try:
            await asyncio.wait_for(event.wait(), timeout=poll_interval_seconds)
        except asyncio.TimeoutError:
            pass


async def _run(settings: Settings) -> None:
    client = build_redis_client(settings.redis)
    scheduler = RetryScheduler(client, settings.redis)
    stop_event = asyncio.Event()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop_event.set)

    logger.info("dart-scheduler starting")
    try:
        await run_scheduler_loop(scheduler, stop_event=stop_event)
    finally:
        await client.aclose()
        logger.info("dart-scheduler stopped")


def main() -> None:
    """Synchronous entrypoint registered as the `dart-scheduler` console script."""
    configure_logging()
    settings = Settings()
    asyncio.run(_run(settings))


if __name__ == "__main__":
    main()
