"""Ready-for-dispatch queue: a Redis Stream at `dart:queue:ready`.

Phase 2 implements only the producer (ingestion) side — `enqueue`, via a
plain `XADD`, which implicitly creates the stream on first write. The
consumer-group machinery (`XGROUP CREATE`, `XREADGROUP`, `XACK`,
`XCLAIM`/`XAUTOCLAIM` for crash recovery) that dispatch workers rely on
is added in Phase 4, once there's a worker pool to actually drive it —
creating the group before then buys nothing and adds a dependency-compat
surface (e.g. `XGROUP CREATE` support in test doubles) this phase
doesn't need.
"""

from __future__ import annotations

from uuid import UUID

import redis.asyncio as redis

from dart.core.config import RedisSettings


class ReadyQueue:
    """Producer-side interface to the `dart:queue:ready` stream."""

    def __init__(self, client: redis.Redis, settings: RedisSettings) -> None:
        self._client = client
        self._stream_key = settings.ready_stream_key

    async def enqueue(self, task_id: UUID) -> str:
        """Append a `task_id` reference to the ready stream.

        Returns:
            The Redis-assigned stream entry ID.
        """
        entry_id: str = await self._client.xadd(
            self._stream_key,
            {"task_id": str(task_id)},
        )
        return entry_id
