"""Read/write access to the `dart:job:<task_id>` Redis Hash.

Per the approved architecture, this Hash is the sole source of truth for
a task's state; every queue (ready stream, retry ZSET, DLQ) holds only a
`task_id` reference back to the record this module manages.
"""

from __future__ import annotations

from uuid import UUID

import redis.asyncio as redis

from dart.core.config import RedisSettings
from dart.models.task import WebhookTask


class JobStore:
    """CRUD access to `WebhookTask` records persisted as Redis Hashes."""

    def __init__(self, client: redis.Redis, settings: RedisSettings) -> None:
        self._client = client
        self._key_prefix = settings.job_key_prefix

    def _key(self, task_id: UUID) -> str:
        return f"{self._key_prefix}{task_id}"

    async def save(self, task: WebhookTask) -> None:
        """Write (or overwrite) the full task record."""
        await self._client.hset(self._key(task.task_id), mapping=task.to_redis_hash())

    async def get(self, task_id: UUID) -> WebhookTask | None:
        """Fetch a task record, or `None` if it doesn't exist."""
        data = await self._client.hgetall(self._key(task_id))
        if not data:
            return None
        return WebhookTask.from_redis_hash(data)
