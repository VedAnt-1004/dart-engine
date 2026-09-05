"""`POST /api/v1/events` — the DART ingestion endpoint.

Request flow: validate (Pydantic, automatic 422 on failure) -> claim the
idempotency key -> on a duplicate, return the original task's result
(200) -> on a fresh claim, persist the `WebhookTask` job record and
enqueue it onto the ready stream (202).
"""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

from fastapi import APIRouter, Depends, Response, status

from dart.api.dependencies import (
    get_idempotency_guard,
    get_job_store,
    get_ready_queue,
    get_settings,
)
from dart.core.config import Settings
from dart.models.events import EventIngestRequest, EventIngestResponse
from dart.models.task import WebhookTask
from dart.queue.idempotency import IdempotencyGuard
from dart.queue.job_store import JobStore
from dart.queue.ready_queue import ReadyQueue

router = APIRouter(prefix="/api/v1", tags=["events"])


@router.post("/events", response_model=EventIngestResponse)
async def ingest_event(
    body: EventIngestRequest,
    response: Response,
    idempotency_guard: IdempotencyGuard = Depends(get_idempotency_guard),
    job_store: JobStore = Depends(get_job_store),
    ready_queue: ReadyQueue = Depends(get_ready_queue),
    settings: Settings = Depends(get_settings),
) -> EventIngestResponse:
    candidate_task_id = uuid4()

    existing_task_id = await idempotency_guard.claim(body.idempotency_key, candidate_task_id)
    if existing_task_id is not None:
        existing_task = await job_store.get(existing_task_id)
        if existing_task is not None:
            response.status_code = status.HTTP_200_OK
            return EventIngestResponse(
                task_id=existing_task.task_id,
                status=existing_task.status,
                idempotency_replay=True,
                created_at=existing_task.created_at,
            )
        # The idempotency key was claimed but its job record is missing
        # (should not happen in normal operation — e.g. manual Redis
        # intervention). Fall through and treat this as a fresh request
        # rather than failing the caller with a 500.

    task = WebhookTask(
        task_id=candidate_task_id,
        event_type=body.event_type,
        target_url=body.target_url,
        payload=body.payload,
        idempotency_key=body.idempotency_key,
        signing_secret_id=body.signing_secret_id,
        max_attempts=body.max_attempts or settings.retry.max_attempts,
        created_at=datetime.now(timezone.utc),
    )

    await job_store.save(task)
    await ready_queue.enqueue(task.task_id)

    response.status_code = status.HTTP_202_ACCEPTED
    return EventIngestResponse(
        task_id=task.task_id,
        status=task.status,
        idempotency_replay=False,
        created_at=task.created_at,
    )
