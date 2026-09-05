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

from pydantic import BaseModel, Field, HttpUrl

from dart.models.task import TaskStatus


class EventIngestRequest(BaseModel):
    """Request body for `POST /api/v1/events`.

    Note: `metadata`, if supplied, is validated here but is not currently
    persisted onto `WebhookTask` or forwarded to `target_url` — it's
    accepted at the boundary per the ingestion-layer spec ("validating
    payloads, target URLs, and metadata") but wiring it into the task
    record is deferred; see the Phase 2 handoff notes.
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
