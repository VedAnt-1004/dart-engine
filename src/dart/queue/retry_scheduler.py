"""Delayed retry scheduling via `dart:zset:retry`.

A Redis Sorted Set with `score = next_attempt_epoch` eliminates database
polling: the scheduler just asks Redis for members whose score has
already passed, rather than running a periodic query against a task
table. `promote_due` moves due members onto the ready stream atomically,
so multiple concurrent scheduler replicas can safely poll the same ZSET
without double-promoting the same task.
"""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID

import redis.asyncio as redis

from dart.core.config import RedisSettings

# KEYS[1] = retry zset key
# KEYS[2] = ready stream key
# ARGV[1] = now (epoch seconds)
# ARGV[2] = batch_size
#
# For each due member, ZREM-then-XADD atomically as a pair: if two
# scheduler replicas both read the same due member before either writes,
# only one's ZREM will return 1 (removed); the other sees 0 and skips the
# XADD entirely. This is what makes promotion double-delivery-safe even
# under concurrent schedulers, without needing a distributed lock.
_PROMOTE_DUE_SCRIPT = """
local due = redis.call('ZRANGEBYSCORE', KEYS[1], '-inf', ARGV[1], 'LIMIT', 0, ARGV[2])
local promoted = {}
for _, task_id in ipairs(due) do
    local removed = redis.call('ZREM', KEYS[1], task_id)
    if removed == 1 then
        redis.call('XADD', KEYS[2], '*', 'task_id', task_id)
        table.insert(promoted, task_id)
    end
end
return promoted
"""


class RetryScheduler:
    """Schedules delayed retries and promotes due ones to the ready queue."""

    def __init__(self, client: redis.Redis, settings: RedisSettings) -> None:
        self._client = client
        self._zset_key = settings.retry_zset_key
        self._stream_key = settings.ready_stream_key
        self._promote_due_script = client.register_script(_PROMOTE_DUE_SCRIPT)

    async def schedule(self, task_id: UUID, next_attempt_at: datetime) -> None:
        """Schedule `task_id` for retry at `next_attempt_at`."""
        score = next_attempt_at.timestamp()
        await self._client.zadd(self._zset_key, {str(task_id): score})

    async def promote_due(
        self, now: datetime | None = None, batch_size: int = 100
    ) -> list[UUID]:
        """Move members whose scheduled time has passed onto the ready
        stream, atomically, in a single batch.

        Args:
            now: Reference time for "due". Defaults to the current time;
                exposed as a parameter for deterministic testing.
            batch_size: Maximum number of members to promote in one call.

        Returns:
            The `task_id`s actually promoted by this call. Under
            concurrent callers, a member due to be promoted may be
            claimed by a different caller — such members simply won't
            appear in this call's return value.
        """
        now_epoch = (now or datetime.now(timezone.utc)).timestamp()
        promoted_raw = await self._promote_due_script(
            keys=[self._zset_key, self._stream_key],
            args=[now_epoch, batch_size],
        )
        return [UUID(raw_id) for raw_id in promoted_raw]

    async def count_pending(self) -> int:
        """Total number of members currently scheduled (due or not).
        Convenience for observability/tests."""
        return await self._client.zcard(self._zset_key)
