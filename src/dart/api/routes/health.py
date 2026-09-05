"""Liveness/readiness probes."""

from __future__ import annotations

import redis.asyncio as redis
from fastapi import APIRouter, Depends

from dart.api.dependencies import get_redis_client

router = APIRouter(tags=["health"])


@router.get("/healthz")
async def liveness() -> dict[str, str]:
    """Liveness probe: the process is up and serving requests."""
    return {"status": "ok"}


@router.get("/readyz")
async def readiness(client: redis.Redis = Depends(get_redis_client)) -> dict[str, str]:
    """Readiness probe: the process is up AND its Redis dependency is reachable."""
    await client.ping()
    return {"status": "ready"}
