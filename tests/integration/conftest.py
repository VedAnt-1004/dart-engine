"""Shared fixtures for DART integration tests.

Uses `fakeredis` so these tests exercise the real HTTP -> queue-layer
path without a live Redis dependency. The app under test is wired to an
*async* fakeredis client (matching production, which uses
`redis.asyncio`); a separate *synchronous* fakeredis client is used only
for test-side assertions, sharing the same in-memory `FakeServer` so both
see identical state without any event-loop-crossing concerns in the test
bodies themselves.
"""

from __future__ import annotations

from typing import Iterator

import fakeredis
import pytest
from fastapi.testclient import TestClient

from dart.api.app import create_app


@pytest.fixture
def fake_redis_server() -> fakeredis.FakeServer:
    """A single in-memory fake Redis server, shared by the app's async
    client and the test's synchronous inspection client."""
    return fakeredis.FakeServer()


@pytest.fixture
def app_redis_client(
    fake_redis_server: fakeredis.FakeServer,
) -> fakeredis.aioredis.FakeRedis:
    """The async Redis client injected into the FastAPI app under test."""
    return fakeredis.aioredis.FakeRedis(server=fake_redis_server, decode_responses=True)


@pytest.fixture
def verify_redis_client(fake_redis_server: fakeredis.FakeServer) -> fakeredis.FakeRedis:
    """A plain synchronous client for inspecting state written by the app
    during test assertions."""
    return fakeredis.FakeRedis(server=fake_redis_server, decode_responses=True)


@pytest.fixture
def api_client(
    app_redis_client: fakeredis.aioredis.FakeRedis,
) -> Iterator[TestClient]:
    """A `TestClient` for the DART API, wired to the shared fake Redis
    server. Using it as a context manager triggers the app's lifespan
    (startup/shutdown) around the yielded client."""
    app = create_app(redis_client=app_redis_client)
    with TestClient(app) as client:
        yield client
