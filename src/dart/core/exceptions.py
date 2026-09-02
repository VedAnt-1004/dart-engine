"""Domain exception hierarchy for DART.

All DART-specific errors inherit from `DartError` so callers can catch the
whole family with a single `except DartError:` when that's appropriate,
while still being able to target specific failure modes precisely.
"""

from __future__ import annotations


class DartError(Exception):
    """Base class for every DART domain exception."""


class ConfigurationError(DartError):
    """Raised when application configuration is invalid, missing, or
    internally inconsistent (e.g. max_backoff_seconds < base_seconds)."""


class SecurityError(DartError):
    """Base class for signing- and verification-related errors."""


class SignatureFormatError(SecurityError):
    """Raised when an `X-DART-Signature` header does not match the
    expected `t=<timestamp>,v1=<hex_digest>` format.

    This is distinct from a *failed* verification (wrong secret, expired
    timestamp, tampered payload) — those are routine and are represented
    as a `False` return from `SignatureVerifier.verify`, not an exception.
    A malformed header indicates a structurally broken caller/client.
    """


class TaskValidationError(DartError):
    """Raised for domain-level `WebhookTask` invariant violations that
    fall outside plain Pydantic field validation — most notably illegal
    `TaskStatus` transitions (e.g. `DELIVERED -> RETRY_SCHEDULED`)."""


class IdempotencyConflictError(DartError):
    """Raised when a duplicate idempotency key is detected during
    event ingestion."""


class CircuitOpenError(DartError):
    """Raised when a dispatch is attempted against a domain whose
    circuit breaker is currently in the OPEN state."""


class QueueError(DartError):
    """Base class for Redis queue/stream operation failures
    (ready stream, retry ZSET, DLQ)."""


class RetryExhaustedError(DartError):
    """Raised when a task has exhausted its configured `max_attempts`
    and is being routed to the dead-letter queue."""
