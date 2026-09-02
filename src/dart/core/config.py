"""Application configuration via `pydantic-settings`.

Settings are grouped into scoped sub-models (Redis, HTTP client, retry,
circuit breaker, security) rather than one flat namespace, so each
component (ingestion API, dispatch worker, retry scheduler) can depend on
only the slice of configuration it actually needs. Every group reads from
environment variables using its own prefix, and the root `Settings`
additionally supports a `.env` file for local development.
"""

from __future__ import annotations

from typing import Any

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class RedisSettings(BaseSettings):
    """Connection, pooling, and key-naming configuration for Redis."""

    model_config = SettingsConfigDict(env_prefix="DART_REDIS_", extra="ignore")

    host: str = "localhost"
    port: int = Field(default=6379, ge=1, le=65535)
    db: int = Field(default=0, ge=0)
    password: str | None = None
    max_connections: int = Field(default=50, ge=1)
    socket_connect_timeout_seconds: float = Field(default=5.0, gt=0)
    socket_timeout_seconds: float = Field(default=5.0, gt=0)

    ready_stream_key: str = "dart:queue:ready"
    retry_zset_key: str = "dart:zset:retry"
    dlq_stream_key: str = "dart:queue:dlq"
    consumer_group: str = "dart-workers"
    job_key_prefix: str = "dart:job:"
    circuit_key_prefix: str = "dart:circuit:"
    idempotency_key_prefix: str = "dart:idempotency:"

    @property
    def url(self) -> str:
        """A `redis://` connection URL assembled from the discrete fields."""
        auth = f":{self.password}@" if self.password else ""
        return f"redis://{auth}{self.host}:{self.port}/{self.db}"


class HTTPClientSettings(BaseSettings):
    """Granular transport timeouts and pool limits for the long-lived,
    connection-pooled `httpx.AsyncClient` used by dispatch workers.

    Separate connect/read/write/pool timeouts (rather than one blanket
    timeout) are what prevent a slowloris-style endpoint from starving
    the worker pool's connections.
    """

    model_config = SettingsConfigDict(env_prefix="DART_HTTP_", extra="ignore")

    connect_timeout_seconds: float = Field(default=5.0, gt=0)
    read_timeout_seconds: float = Field(default=10.0, gt=0)
    write_timeout_seconds: float = Field(default=10.0, gt=0)
    pool_timeout_seconds: float = Field(default=5.0, gt=0)
    max_connections: int = Field(default=100, ge=1)
    max_keepalive_connections: int = Field(default=20, ge=0)
    user_agent: str = "dart-engine/1.0"


class RetrySettings(BaseSettings):
    """Exponential-backoff-with-full-jitter tuning:
    `sleep = uniform(0, min(max_backoff, base * 2**attempt))`.
    """

    model_config = SettingsConfigDict(env_prefix="DART_RETRY_", extra="ignore")

    base_seconds: float = Field(default=1.0, gt=0)
    max_backoff_seconds: float = Field(default=300.0, gt=0)
    max_attempts: int = Field(default=8, ge=1)

    @field_validator("max_backoff_seconds")
    @classmethod
    def _max_backoff_at_least_base(cls, v: float, info: Any) -> float:
        base = info.data.get("base_seconds")
        if base is not None and v < base:
            raise ValueError("max_backoff_seconds must be >= base_seconds")
        return v


class CircuitBreakerSettings(BaseSettings):
    """Per-domain circuit breaker thresholds governing the
    CLOSED -> OPEN -> HALF_OPEN -> {CLOSED | OPEN} state machine."""

    model_config = SettingsConfigDict(env_prefix="DART_CIRCUIT_", extra="ignore")

    failure_threshold: int = Field(default=5, ge=1)
    open_cooldown_seconds: int = Field(default=30, ge=1)
    half_open_probe_count: int = Field(default=3, ge=1)


class SecuritySettings(BaseSettings):
    """Defaults for HMAC signing and anti-replay verification."""

    model_config = SettingsConfigDict(env_prefix="DART_SECURITY_", extra="ignore")

    signature_tolerance_seconds: int = Field(default=300, ge=0)


class Settings(BaseSettings):
    """Root application settings, composed of the scoped groups above.

    Instantiate once per process (API, worker, or scheduler) via
    `Settings()`; pydantic-settings resolves values from environment
    variables and an optional `.env` file automatically.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    environment: str = "development"
    idempotency_key_ttl_seconds: int = Field(default=86_400, ge=1)

    redis: RedisSettings = Field(default_factory=RedisSettings)
    http_client: HTTPClientSettings = Field(default_factory=HTTPClientSettings)
    retry: RetrySettings = Field(default_factory=RetrySettings)
    circuit_breaker: CircuitBreakerSettings = Field(default_factory=CircuitBreakerSettings)
    security: SecuritySettings = Field(default_factory=SecuritySettings)
