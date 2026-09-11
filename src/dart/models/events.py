"""Ingestion request/response schemas for `POST /api/v1/events`.

Kept distinct from `dart.models.task.WebhookTask` (the internal delivery
record persisted to Redis) — this module defines the *wire contract* at
the ingestion boundary, which is allowed to evolve independently of the
internal task representation.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field, HttpUrl, field_validator

from dart.models.task import TaskStatus
from dart.security.ssrf import is_blocked_literal_host


class EventIngestRequest(BaseModel):
    """Request body for `POST /api/v1/events`.

    Note: `metadata`, if supplied, is persisted onto `WebhookTask` (and
    therefore round-trips through `dart:job:<task_id>`) but is never
    forwarded to `target_url` — it's caller-side context for future
    audit/observability tooling, not part of what the third party
    receives.
    """

    event_type: str = Field(
        min_length=1,
        description="Dot-namespaced event type, e.g. 'invoice.paid'.",
    )
    target_url: HttpUrl = Field(
        description="Destination endpoint DART will POST the payload to."
    )
    payload: dict[str, Any] = Field(
        description="Arbitrary JSON body delivered verbatim to target_url."
    )
    metadata: dict[str, Any] | None = Field(
        default=None,
        description="Optional caller-supplied context for tracing/audit only.",
    )
    idempotency_key: str = Field(
        min_length=1,
        description="Caller-supplied key guaranteeing at-most-one dispatch per unique value.",
    )
    signing_secret_id: str = Field(
        min_length=1,
        description="Reference to the secret used to HMAC-sign this delivery.",
    )
    max_attempts: int | None = Field(
        default=None,
        ge=1,
        description="Override the default max retry attempts for this event.",
    )

    @field_validator("target_url")
    @classmethod
    def _reject_blocked_literal_hosts(cls, value: HttpUrl) -> HttpUrl:
        """SSRF defense, layer 1 of 2: fast, synchronous, zero-I/O check
        that rejects a `target_url` whose host is a LITERAL IP address
        in a blocked range (loopback / RFC 1918 / link-local / multicast
        / etc).

        Deliberately does NOT resolve DNS hostnames — a synchronous
        Pydantic validator can't `await`, and a blocking DNS lookup here
        would stall the event loop for every other concurrent ingestion
        request. A hostname that currently resolves to a public IP but
        later resolves somewhere private is NOT caught at this layer;
        that's exactly what `dart.worker.ssrf_transport.SSRFSafeTransport`
        (layer 2, at dispatch time, right before the connection is
        actually made) exists to close. See `dart.security.ssrf`'s
        module docstring for the full two-layer rationale.
        """
        host = value.host
        if host and is_blocked_literal_host(host):
            raise ValueError(
                f"target_url host {host!r} is a non-public address and is "
                "not allowed."
            )
        return value


class EventIngestResponse(BaseModel):
    """Response body for `POST /api/v1/events`."""

    task_id: UUID
    status: TaskStatus
    idempotency_replay: bool = Field(
        description=(
            "True if this request matched an existing idempotency_key and "
            "no new task was created; the returned task_id/status describe "
            "the original task."
        )
    )
    created_at: datetime
