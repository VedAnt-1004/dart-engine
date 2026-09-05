"""Idempotency key deduplication guard.

Backed by `dart:idempotency:<key>` — a Redis string with TTL, holding the
`task_id` that first claimed it. `SET ... NX EX ...` gives an atomic
"claim, or discover the existing owner" primitive in a single round trip.
"""

from __future__ import annotations

from uuid import UUID

import redis.asyncio as redis

from dart.core.config import RedisSettings


class IdempotencyGuard:
    """Enforces at-most-one-dispatch-per-idempotency-key semantics."""

    def __init__(
        self, client: redis.Redis, settings: RedisSettings, ttl_seconds: int
    ) -> None:
        self._client = client
        self._key_prefix = settings.idempotency_key_prefix
        self._ttl_seconds = ttl_seconds

    def _key(self, idempotency_key: str) -> str:
        return f"{self._key_prefix}{idempotency_key}"

    async def claim(self, idempotency_key: str, task_id: UUID) -> UUID | None:
        """Attempt to claim `idempotency_key` on behalf of `task_id`.

        Returns:
            `None` if the claim succeeded — this is a new, unique
                request and the caller should proceed to create and
                enqueue a task under `task_id`.
            The existing `task_id` if the key was already claimed by a
                prior request — this is a duplicate; the caller should
                look up and return the *original* task's result rather
                than enqueue a new one.
        """
        claimed = await self._client.set(
            self._key(idempotency_key),
            str(task_id),
            nx=True,
            ex=self._ttl_seconds,
        )
        if claimed:
            return None

        existing_raw = await self._client.get(self._key(idempotency_key))
        if existing_raw is None:
            # Narrow race: the key expired between our failed SET NX and
            # this GET. Retry once rather than surfacing a spurious 500.
            return await self.claim(idempotency_key, task_id)
        return UUID(existing_raw)
