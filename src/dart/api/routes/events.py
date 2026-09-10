"""`POST /api/v1/events` — the DART ingestion endpoint.

Request flow: validate (Pydantic, automatic 422 on failure) -> build the
candidate `WebhookTask` -> atomically claim the idempotency key, persist
the job record, and enqueue it onto the ready stream in a single Redis
round trip (`EventIngestor.ingest`) -> on a duplicate, look up and
return the *original* task's result (200) -> on a fresh ingestion,
return the new task's result (202).

This replaces the old three-round-trip sequence (claim -> save ->
enqueue), which had a real correctness gap: a crash between claim and
save left the idempotency key pointing at a task_id with no job record,
and every subsequent request with that key silently created a new
orphaned task rather than fixing the mapping — unbounded duplicate
dispatch until the idempotency TTL expired, not a one-time orphan. The
atomic script in `EventIngestor` makes that window structurally
impossible: claim and persist happen as one indivisible unit.
"""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Response, status

from dart.api.dependencies import get_event_ingestor, get_job_store, get_settings
from dart.core.config import Settings
from dart.models.events import EventIngestRequest, EventIngestResponse
from dart.models.task import WebhookTask
from dart.queue.ingestion import EventIngestor
from dart.queue.job_store import JobStore

router = APIRouter(prefix="/api/v1", tags=["events"])


@router.post("/events", response_model=EventIngestResponse)
async def ingest_event(
    body: EventIngestRequest,
    response: Response,
    event_ingestor: EventIngestor = Depends(get_event_ingestor),
    job_store: JobStore = Depends(get_job_store),
    settings: Settings = Depends(get_settings),
) -> EventIngestResponse:
    task = WebhookTask(
        task_id=uuid4(),
        event_type=body.event_type,
        target_url=body.target_url,
        payload=body.payload,
        metadata=body.metadata,
        idempotency_key=body.idempotency_key,
        signing_secret_id=body.signing_secret_id,
        max_attempts=body.max_attempts or settings.retry.max_attempts,
        created_at=datetime.now(timezone.utc),
    )

    result = await event_ingestor.ingest(
        task, idempotency_ttl_seconds=settings.idempotency_key_ttl_seconds
    )

    if result.is_duplicate:
        existing_task = await job_store.get(result.task_id)
        if existing_task is not None:
            response.status_code = status.HTTP_200_OK
            return EventIngestResponse(
                task_id=existing_task.task_id,
                status=existing_task.status,
                idempotency_replay=True,
                created_at=existing_task.created_at,
            )
        # Structurally unreachable now: claim and persist happen
        # atomically in one script, so there is no window in which the
        # idempotency key can point at a task_id with no job record.
        # Unlike the old sequential flow — where this exact situation
        # was silently "fixed" by creating a new orphaned task every
        # time — surfacing it loudly is deliberate. If it ever fires,
        # something corrupted Redis out-of-band (e.g. a manual DEL of
        # the job hash), and hiding that would be worse than a 500.
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=(
                f"Idempotency key claimed for task_id={result.task_id} but no "
                "job record exists; this should be unreachable."
            ),
        )

    response.status_code = status.HTTP_202_ACCEPTED
    return EventIngestResponse(
        task_id=task.task_id,
        status=task.status,
        idempotency_replay=False,
        created_at=task.created_at,
    )
