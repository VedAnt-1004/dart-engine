"""Dead-letter queue: `dart:queue:dlq`, a Redis Stream of terminal-failure
audit records.

Per the architecture blueprint (§2.4), the full `WebhookTask` record is
never deleted from `dart:job:<task_id>` when a task is dead-lettered —
this stream holds only a lightweight reference plus the failure reason
and timestamp, so a future manual-replay/inspection tool can look up the
full task via `JobStore` from the `task_id` alone.
"""

from __future__ import annotations

from datetime import datetime, timezone

import redis.asyncio as redis

from dart.core.config import RedisSettings
from dart.models.task import WebhookTask


class DeadLetterQueue:
    """Writer/reader for terminal-failure records on `dart:queue:dlq`."""

    def __init__(self, client: redis.Redis, settings: RedisSettings) -> None:
        self._client = client
        self._stream_key = settings.dlq_stream_key

    async def add(self, task: WebhookTask, reason: str) -> str:
        """Append a terminal-failure record for `task`.

        Returns:
            The Redis-assigned stream entry ID.
        """
        entry_id: str = await self._client.xadd(
            self._stream_key,
            {
                "task_id": str(task.task_id),
                "final_status": task.status.value,
                "failure_reason": reason,
                "dead_lettered_at": datetime.now(timezone.utc).isoformat(),
            },
        )
        return entry_id

    async def list_recent(self, count: int = 100) -> list[dict[str, str]]:
        """The most recent DLQ entries, newest first.

        Each dict includes an `entry_id` key alongside the fields
        written by `add`, for a future manual-replay/inspection tool.
        """
        entries = await self._client.xrevrange(self._stream_key, count=count)
        return [dict(fields, entry_id=entry_id) for entry_id, fields in entries]
