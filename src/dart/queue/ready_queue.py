"""Ready-for-dispatch queue: a Redis Stream at `dart:queue:ready`.

Phase 2 implemented only the producer (ingestion) side — `enqueue`, via a
plain `XADD`, which implicitly creates the stream on first write. Phase 4
adds the consumer-group side that dispatch workers rely on:
`ensure_consumer_group`, `read_new` (XREADGROUP), `ack` (XACK), and
`claim_stale` (XAUTOCLAIM) for recovering entries left in a worker's
Pending Entries List (PEL) after a crash mid-dispatch.
"""

from __future__ import annotations

from uuid import UUID

import redis.asyncio as redis

from dart.core.config import RedisSettings


class ReadyQueue:
    """Producer- and consumer-side interface to the `dart:queue:ready` stream."""

    def __init__(self, client: redis.Redis, settings: RedisSettings) -> None:
        self._client = client
        self._stream_key = settings.ready_stream_key
        self._consumer_group = settings.consumer_group

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

    async def ensure_consumer_group(self) -> None:
        """Idempotently create the consumer group at stream position
        `"0"` (i.e. it will see all history plus new entries), creating
        the stream itself if it doesn't exist yet. Safe to call on every
        worker startup — a `BUSYGROUP` error (group already exists) is
        swallowed; any other error propagates.
        """
        try:
            await self._client.xgroup_create(
                name=self._stream_key,
                groupname=self._consumer_group,
                id="0",
                mkstream=True,
            )
        except redis.ResponseError as exc:
            if "BUSYGROUP" not in str(exc):
                raise

    async def read_new(
        self, consumer_name: str, count: int = 10, block_ms: int = 5000
    ) -> list[tuple[str, UUID]]:
        """Read up to `count` never-before-delivered entries via
        `XREADGROUP`, blocking up to `block_ms` if none are immediately
        available.

        Returns:
            `(entry_id, task_id)` pairs, in delivery order.
        """
        response = await self._client.xreadgroup(
            groupname=self._consumer_group,
            consumername=consumer_name,
            streams={self._stream_key: ">"},
            count=count,
            block=block_ms,
        )
        return self._parse_xreadgroup_response(response)

    async def ack(self, entry_id: str) -> None:
        """Acknowledge successful processing, removing `entry_id` from
        the consumer group's Pending Entries List (PEL)."""
        await self._client.xack(self._stream_key, self._consumer_group, entry_id)

    async def claim_stale(
        self, consumer_name: str, min_idle_ms: int = 30_000, count: int = 10
    ) -> list[tuple[str, UUID]]:
        """Claim entries idle for at least `min_idle_ms` in the group's
        PEL — i.e. delivered to some consumer that crashed before
        acking — reassigning them to `consumer_name` for a retry.

        This is the mechanism that makes `dart:queue:ready` fault-tolerant
        against a worker crashing mid-dispatch: the entry isn't lost, it
        just sits in the PEL until some worker's `claim_stale` call
        picks it back up.
        """
        result = await self._client.xautoclaim(
            name=self._stream_key,
            groupname=self._consumer_group,
            consumername=consumer_name,
            min_idle_time=min_idle_ms,
            start_id="0",
            count=count,
        )
        # redis-py's XAUTOCLAIM reply shape is
        # (next_cursor, [(entry_id, fields), ...], [deleted_entry_ids]).
        _next_cursor, entries, *_ = result
        return self._parse_entries(entries)

    @staticmethod
    def _parse_entries(entries: list[tuple[str, dict[str, str]]]) -> list[tuple[str, UUID]]:
        parsed: list[tuple[str, UUID]] = []
        for entry_id, fields in entries:
            task_id_raw = fields.get("task_id")
            if task_id_raw:
                parsed.append((entry_id, UUID(task_id_raw)))
        return parsed

    @classmethod
    def _parse_xreadgroup_response(cls, response: object) -> list[tuple[str, UUID]]:
        """Flatten redis-py's XREADGROUP reply shape —
        `[(stream_key, [(entry_id, fields), ...])]` — down to
        `[(entry_id, task_id)]`. An empty/`None` reply (block timed out
        with nothing available) yields an empty list.
        """
        if not response:
            return []
        parsed: list[tuple[str, UUID]] = []
        for _stream_key, entries in response:  # type: ignore[misc]
            parsed.extend(cls._parse_entries(entries))
        return parsed
