"""WebhookTask domain model and its `dart:job:<task_id>` Redis Hash
serialization contract.

`WebhookTask` is the sole source of truth for a delivery task's state, per
the approved architecture: the ready stream, retry ZSET, and DLQ stream
all hold only `task_id` references back to this record.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any, Mapping
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, model_validator

from dart.core.exceptions import TaskValidationError


class TaskStatus(StrEnum):
    """Lifecycle states for a `WebhookTask`.

    State machine (see architecture blueprint §1, "Task Lifecycle State
    Machine")::

        PENDING -> IN_FLIGHT -> DELIVERED           (terminal, success)
                             -> RETRY_SCHEDULED -> IN_FLIGHT (loops via scheduler)
                             -> DEAD_LETTERED         (terminal, exhausted / 4xx)
    """

    PENDING = "PENDING"
    IN_FLIGHT = "IN_FLIGHT"
    DELIVERED = "DELIVERED"
    RETRY_SCHEDULED = "RETRY_SCHEDULED"
    DEAD_LETTERED = "DEAD_LETTERED"


#: Explicit transition table for the task lifecycle state machine. Kept as
#: a module-level constant (rather than inlined logic) so it can double as
#: living documentation of legal transitions.
_ALLOWED_TRANSITIONS: dict[TaskStatus, frozenset[TaskStatus]] = {
    TaskStatus.PENDING: frozenset({TaskStatus.IN_FLIGHT}),
    TaskStatus.IN_FLIGHT: frozenset(
        {TaskStatus.DELIVERED, TaskStatus.RETRY_SCHEDULED, TaskStatus.DEAD_LETTERED}
    ),
    TaskStatus.RETRY_SCHEDULED: frozenset({TaskStatus.IN_FLIGHT}),
    TaskStatus.DELIVERED: frozenset(),
    TaskStatus.DEAD_LETTERED: frozenset(),
}

#: Statuses from which no further transition is legal.
TERMINAL_STATUSES: frozenset[TaskStatus] = frozenset(
    {TaskStatus.DELIVERED, TaskStatus.DEAD_LETTERED}
)


def is_valid_transition(from_status: TaskStatus, to_status: TaskStatus) -> bool:
    """Pure predicate for the task lifecycle state machine.

    Exposed as a standalone function (in addition to
    `WebhookTask.transition_to`) so the transition table can be unit
    tested without constructing a full model instance.
    """
    return to_status in _ALLOWED_TRANSITIONS.get(from_status, frozenset())


class WebhookTask(BaseModel):
    """Canonical representation of a single webhook delivery task.

    Mirrors the `dart:job:<task_id>` Redis Hash schema defined in the
    architecture blueprint (§2.1). Field-level constraints enforce basic
    invariants (non-empty identifiers, non-negative counters); the
    `TaskStatus` state machine is enforced separately via
    `transition_to`, since "is this a legal transition" depends on the
    *current* state and isn't expressible as a static field constraint.
    """

    model_config = ConfigDict(validate_assignment=True)

    task_id: UUID
    event_type: str = Field(min_length=1)
    target_url: HttpUrl
    payload: dict[str, Any]
    idempotency_key: str = Field(min_length=1)
    signing_secret_id: str = Field(min_length=1)
    status: TaskStatus = TaskStatus.PENDING
    attempt_count: int = Field(default=0, ge=0)
    max_attempts: int = Field(default=8, ge=1)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    next_attempt_at: datetime | None = None
    last_status_code: int | None = None
    last_error: str | None = None

    @model_validator(mode="after")
    def _attempt_count_within_bounds(self) -> "WebhookTask":
        if self.attempt_count > self.max_attempts:
            raise ValueError("attempt_count cannot exceed max_attempts")
        return self

    def transition_to(self, new_status: TaskStatus) -> None:
        """Mutate `status` in place, enforcing the task lifecycle state
        machine.

        Raises:
            TaskValidationError: if `new_status` is not reachable from the
                current `status` (e.g. `DELIVERED -> RETRY_SCHEDULED`, or
                any transition out of a terminal state). The status is
                left unchanged when this raises.
        """
        if not is_valid_transition(self.status, new_status):
            raise TaskValidationError(
                f"Illegal status transition: {self.status} -> {new_status}"
            )
        self.status = new_status

    def is_terminal(self) -> bool:
        """True if no further transitions are legal from the current status."""
        return self.status in TERMINAL_STATUSES

    def to_redis_hash(self) -> dict[str, str]:
        """Serialize to a flat `str -> str` mapping suitable for
        `HSET dart:job:<task_id>`.

        Optional fields that are `None` are encoded as the empty string
        sentinel `""`, inverted by `from_redis_hash`.
        """
        return {
            "task_id": str(self.task_id),
            "event_type": self.event_type,
            "target_url": str(self.target_url),
            "payload": json.dumps(self.payload, separators=(",", ":")),
            "idempotency_key": self.idempotency_key,
            "signing_secret_id": self.signing_secret_id,
            "status": self.status.value,
            "attempt_count": str(self.attempt_count),
            "max_attempts": str(self.max_attempts),
            "created_at": self.created_at.isoformat(),
            "next_attempt_at": (
                self.next_attempt_at.isoformat() if self.next_attempt_at else ""
            ),
            "last_status_code": (
                str(self.last_status_code)
                if self.last_status_code is not None
                else ""
            ),
            "last_error": self.last_error or "",
        }

    @classmethod
    def from_redis_hash(cls, data: Mapping[str, str]) -> "WebhookTask":
        """Reconstruct a `WebhookTask` from a `dart:job:<task_id>` Redis
        hash, inverting `to_redis_hash`.

        Empty-string sentinels for optional fields (`next_attempt_at`,
        `last_status_code`, `last_error`) map back to `None`.
        """

        def _optional(key: str) -> str | None:
            value = data.get(key, "")
            return value or None

        next_attempt_raw = _optional("next_attempt_at")
        last_status_code_raw = _optional("last_status_code")

        return cls(
            task_id=UUID(data["task_id"]),
            event_type=data["event_type"],
            target_url=data["target_url"],  # type: ignore[arg-type]
            payload=json.loads(data["payload"]),
            idempotency_key=data["idempotency_key"],
            signing_secret_id=data["signing_secret_id"],
            status=TaskStatus(data["status"]),
            attempt_count=int(data["attempt_count"]),
            max_attempts=int(data["max_attempts"]),
            created_at=datetime.fromisoformat(data["created_at"]),
            next_attempt_at=(
                datetime.fromisoformat(next_attempt_raw) if next_attempt_raw else None
            ),
            last_status_code=(
                int(last_status_code_raw) if last_status_code_raw else None
            ),
            last_error=_optional("last_error"),
        )
