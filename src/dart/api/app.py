"""FastAPI application factory for the DART ingestion API."""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import AsyncIterator

import redis.asyncio as redis
from fastapi import FastAPI

from dart.api.dependencies import get_settings
from dart.api.routes import events, health
from dart.core.logging import configure_logging
from dart.queue.redis_client import build_redis_client


def create_app(redis_client: redis.Redis | None = None) -> FastAPI:
    """Build the DART FastAPI application.

    Args:
        redis_client: An optional pre-built Redis client to use instead
            of constructing one from `Settings`. Tests inject a fakeredis
            instance here so the app never attempts a real network
            connection; when omitted (normal runtime), a real client is
            built and its lifecycle is owned by this app's lifespan.
    """
    settings = get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        configure_logging()

        client = redis_client if redis_client is not None else build_redis_client(
            settings.redis
        )
        app.state.redis_client = client
        owns_client = redis_client is None

        try:
            yield
        finally:
            if owns_client:
                await client.aclose()

    app = FastAPI(
        title="DART — Dispatch Async Relay & Transport",
        version="0.1.0",
        lifespan=lifespan,
    )
    app.include_router(health.router)
    app.include_router(events.router)
    return app


app = create_app()
