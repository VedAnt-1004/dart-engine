"""`AsyncDispatcher`: delivers `WebhookTask`s over HTTP with signing,
per-domain circuit breaking, and retry/DLQ routing.

Two design notes relative to the original interface sketch in the
architecture blueprint, worth flagging explicitly:

1. `dispatch()` DOES touch Redis — but only for circuit-breaker
   bookkeeping (`allow_request` / `record_success` / `record_failure`),
   not job-state routing. The circuit breaker has to gate the attempt
   *before* the HTTP call happens and record the outcome atomically with
   it, so this coupling is closer to unavoidable. Job-state Redis writes
   (DELIVERED / RETRY_SCHEDULED / DEAD_LETTERED) are deliberately kept
   out of `dispatch()` and live in `handle_outcome()` instead, per the
   blueprint's intent of separating "what happened on the wire" from
   "what do we do about it."
2. `handle_outcome()` and the four extra constructor dependencies
   (`job_store`, `ready_queue`, `retry_scheduler`, `dlq`) are additions
   beyond the original 4-argument blueprint stub — extracting outcome
   routing into its own method makes it independently testable without
   needing a full `XREADGROUP` consumer loop running.
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import urlparse
from uuid import UUID

import httpx

from dart.core.logging import get_logger
from dart.models.task import TaskStatus, WebhookTask
from dart.queue.dlq import DeadLetterQueue
from dart.queue.job_store import JobStore
from dart.queue.ready_queue import ReadyQueue
from dart.queue.retry_scheduler import RetryScheduler
from dart.resilience.circuit_breaker import CircuitBreaker
from dart.resilience.retry_policy import RetryPolicy
from dart.security.secrets import SigningSecretResolver
from dart.worker.ssrf_transport import SSRFBlockedError

logger = get_logger(__name__)


@dataclass(frozen=True)
class DispatchResult:
    """Outcome of a single delivery attempt."""

    success: bool
    status_code: int | None
    latency_ms: float
    error: str | None
    retry_after_seconds: float | None
    circuit_open: bool = False
    """True if the attempt was skipped entirely because the domain's
    circuit breaker denied it — distinct from an actual network/HTTP
    failure, since no request was sent at all."""
    exception: Exception | None = None
    """The actual transport-level exception `dispatch()` caught, if any.
    Distinct from `error` (a string, suitable for logging/storage as
    `last_error`) — `handle_outcome()` needs the real exception object to
    forward to `RetryPolicy.should_retry()`, whose contract treats
    `exception is not None` as unconditionally retryable regardless of
    `status_code` (which is always `None` for a transport failure,
    since no HTTP response was ever received)."""


def _extract_domain(url: str) -> str:
    """Circuit breaker keys are per-domain, not per-URL."""
    return urlparse(url).netloc


def _parse_retry_after(value: str | None) -> float | None:
    """Parse a `Retry-After` header value: either delta-seconds or an
    HTTP-date. Returns `None` if absent or unparseable, in which case
    the caller falls back to the computed exponential-jitter backoff.
    """
    if not value:
        return None
    try:
        return float(value)
    except ValueError:
        pass
    try:
        target = parsedate_to_datetime(value)
        if target.tzinfo is None:
            target = target.replace(tzinfo=timezone.utc)
        delta_seconds = (target - datetime.now(timezone.utc)).total_seconds()
        return max(delta_seconds, 0.0)
    except (TypeError, ValueError):
        return None


class AsyncDispatcher:
    """Delivers `WebhookTask`s over HTTP and routes the outcome."""

    def __init__(
        self,
        client: httpx.AsyncClient,
        signer_factory: SigningSecretResolver,
        circuit_breaker: CircuitBreaker,
        retry_policy: RetryPolicy,
        job_store: JobStore,
        ready_queue: ReadyQueue,
        retry_scheduler: RetryScheduler,
        dlq: DeadLetterQueue,
    ) -> None:
        self._client = client
        self._signer_factory = signer_factory
        self._circuit_breaker = circuit_breaker
        self._retry_policy = retry_policy
        self._job_store = job_store
        self._ready_queue = ready_queue
        self._retry_scheduler = retry_scheduler
        self._dlq = dlq

    async def dispatch(self, task: WebhookTask) -> DispatchResult:
        """Attempt a single delivery of `task`.

        Does not mutate job state in Redis (see the module docstring for
        the one exception: circuit-breaker bookkeeping). The caller —
        `handle_outcome`, or a test — decides what happens next.
        """
        domain = _extract_domain(str(task.target_url))

        if not await self._circuit_breaker.allow_request(domain):
            logger.info(
                "Dispatch skipped: circuit open",
                extra={"task_id": str(task.task_id), "domain": domain},
            )
            return DispatchResult(
                success=False,
                status_code=None,
                latency_ms=0.0,
                error=f"circuit breaker open for domain={domain}",
                retry_after_seconds=None,
                circuit_open=True,
            )

        signer = self._signer_factory(task.signing_secret_id)
        body = json.dumps(task.payload, separators=(",", ":")).encode("utf-8")
        signature_header = signer.sign(body)

        start = time.monotonic()
        try:
            response = await self._client.post(
                str(task.target_url),
                content=body,
                headers={
                    "Content-Type": "application/json",
                    "X-DART-Signature": signature_header,
                    "X-DART-Event-Type": task.event_type,
                },
            )
        except SSRFBlockedError as exc:
            latency_ms = (time.monotonic() - start) * 1000
            # Deliberately NOT recorded against the circuit breaker: an
            # SSRF block is a security decision about this target, not
            # a domain-health signal, and mixing the two would let a
            # single rebinding attempt against a domain start
            # circuit-breaking otherwise-legitimate future traffic to
            # it. `exception` is left at its default (None) so
            # `RetryPolicy.should_retry` falls through its "no status,
            # no exception" branch -- non-retryable, straight to DLQ,
            # since retrying an SSRF-blocked target can never succeed.
            logger.warning(
                "Dispatch attempt blocked by SSRF guard",
                extra={
                    "task_id": str(task.task_id),
                    "target_url": str(task.target_url),
                    "attempt": task.attempt_count,
                    "latency_ms": latency_ms,
                    "error": str(exc),
                },
            )
            return DispatchResult(
                success=False,
                status_code=None,
                latency_ms=latency_ms,
                error=str(exc),
                retry_after_seconds=None,
            )
        except httpx.HTTPError as exc:
            latency_ms = (time.monotonic() - start) * 1000
            await self._circuit_breaker.record_failure(domain)
            logger.warning(
                "Dispatch attempt failed (transport error)",
                extra={
                    "task_id": str(task.task_id),
                    "target_url": str(task.target_url),
                    "attempt": task.attempt_count,
                    "latency_ms": latency_ms,
                    "error": str(exc),
                },
            )
            return DispatchResult(
                success=False,
                status_code=None,
                latency_ms=latency_ms,
                error=str(exc),
                retry_after_seconds=None,
                exception=exc,
            )

        latency_ms = (time.monotonic() - start) * 1000
        success = 200 <= response.status_code < 300

        if success:
            await self._circuit_breaker.record_success(domain)
        else:
            await self._circuit_breaker.record_failure(domain)

        logger.info(
            "Dispatch attempt completed",
            extra={
                "task_id": str(task.task_id),
                "target_url": str(task.target_url),
                "attempt": task.attempt_count,
                "status_code": response.status_code,
                "latency_ms": latency_ms,
            },
        )

        return DispatchResult(
            success=success,
            status_code=response.status_code,
            latency_ms=latency_ms,
            error=None if success else f"HTTP {response.status_code}",
            retry_after_seconds=_parse_retry_after(response.headers.get("retry-after")),
        )

    async def handle_outcome(self, task: WebhookTask, result: DispatchResult) -> TaskStatus:
        """Apply a `DispatchResult` to Redis: mark delivered, schedule a
        retry, or route to the DLQ. Mutates `task` in place (status,
        attempt_count, last_status_code, last_error, next_attempt_at) and
        persists it via `JobStore`. Returns the resulting status.
        """
        if result.success:
            task.transition_to(TaskStatus.DELIVERED)
            task.last_status_code = result.status_code
            task.last_error = None
            await self._job_store.save(task)
            return TaskStatus.DELIVERED

        task.attempt_count += 1
        task.last_status_code = result.status_code
        task.last_error = result.error

        if result.circuit_open:
            # There was no real attempt to classify by status/exception —
            # a circuit-open skip is retryable purely on attempt budget.
            should_retry = task.attempt_count < task.max_attempts
        else:
            should_retry = self._retry_policy.should_retry(
                attempt=task.attempt_count,
                status_code=result.status_code,
                exception=result.exception,
            )

        if should_retry:
            backoff_seconds = self._retry_policy.resolve_backoff(
                task.attempt_count, retry_after_seconds=result.retry_after_seconds
            )
            next_attempt_at = datetime.now(timezone.utc) + timedelta(seconds=backoff_seconds)
            task.next_attempt_at = next_attempt_at
            task.transition_to(TaskStatus.RETRY_SCHEDULED)
            await self._job_store.save(task)
            await self._retry_scheduler.schedule(task.task_id, next_attempt_at)
            return TaskStatus.RETRY_SCHEDULED

        task.transition_to(TaskStatus.DEAD_LETTERED)
        await self._job_store.save(task)
        await self._dlq.add(task, reason=result.error or "max attempts exhausted")
        return TaskStatus.DEAD_LETTERED

    async def process_one(self, entry_id: str, task_id: UUID) -> None:
        """Fetch, transition to IN_FLIGHT, dispatch, route the outcome,
        and ack a single already-claimed stream entry.

        Public (not `_process_one`) deliberately: it's the correct,
        state-machine-safe way to drive a single task through dispatch
        from a stream entry, and tests should use it directly rather
        than hand-rolling the same sequence — a hand-rolled version in
        this codebase's own test suite once forgot the PENDING ->
        IN_FLIGHT transition `dispatch()`/`handle_outcome()` require,
        which is exactly the class of mistake exposing this as a public
        method guards against.
        """
        task = await self._job_store.get(task_id)
        if task is None:
            logger.warning(
                "Ready-stream entry referenced a missing job; acking and skipping",
                extra={"task_id": str(task_id)},
            )
            await self._ready_queue.ack(entry_id)
            return

        if task.status in (TaskStatus.PENDING, TaskStatus.RETRY_SCHEDULED):
            task.transition_to(TaskStatus.IN_FLIGHT)
            await self._job_store.save(task)
        elif task.status == TaskStatus.IN_FLIGHT:
            # Reclaimed via claim_stale() after a worker crashed mid-
            # dispatch: already IN_FLIGHT, no transition needed — just
            # retry the attempt.
            pass
        elif task.is_terminal():
            # Already DELIVERED/DEAD_LETTERED — e.g. a stream entry left
            # over from a race between two promotions. Nothing to do.
            await self._ready_queue.ack(entry_id)
            return

        result = await self.dispatch(task)
        await self.handle_outcome(task, result)
        await self._ready_queue.ack(entry_id)

    async def run_worker_loop(
        self,
        consumer_name: str,
        batch_size: int = 10,
        block_ms: int = 5000,
        stale_min_idle_ms: int = 30_000,
        stop_event: asyncio.Event | None = None,
    ) -> None:
        """`XREADGROUP` loop: reclaim stale entries, claim new ones,
        dispatch, route the outcome, and ack — until `stop_event` is set.
        """
        event = stop_event if stop_event is not None else asyncio.Event()

        while not event.is_set():
            reclaimed = await self._ready_queue.claim_stale(
                consumer_name, min_idle_ms=stale_min_idle_ms, count=batch_size
            )
            for entry_id, task_id in reclaimed:
                await self.process_one(entry_id, task_id)

            entries = await self._ready_queue.read_new(
                consumer_name, count=batch_size, block_ms=block_ms
            )
            for entry_id, task_id in entries:
                await self.process_one(entry_id, task_id)
