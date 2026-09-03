"""Unit tests for `dart.models.task` (WebhookTask, TaskStatus, transitions)."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from dart.core.exceptions import TaskValidationError
from dart.models.task import TaskStatus, WebhookTask, is_valid_transition

VALID_KWARGS: dict[str, Any] = dict(
    task_id=uuid4(),
    event_type="invoice.paid",
    target_url="https://example.com/webhooks/dart",
    payload={"id": "evt_123", "amount": 4200, "currency": "usd"},
    idempotency_key="idem_abc123",
    signing_secret_id="secret_ref_001",
)


def make_task(**overrides: object) -> WebhookTask:
    kwargs = {**VALID_KWARGS, **overrides}
    return WebhookTask(**kwargs)  # type: ignore[arg-type]


class TestFieldValidation:
    def test_valid_task_constructs_with_expected_defaults(self) -> None:
        task = make_task()
        assert task.status == TaskStatus.PENDING
        assert task.attempt_count == 0
        assert task.max_attempts == 8

    def test_rejects_empty_event_type(self) -> None:
        with pytest.raises(ValidationError):
            make_task(event_type="")

    def test_rejects_empty_idempotency_key(self) -> None:
        with pytest.raises(ValidationError):
            make_task(idempotency_key="")

    def test_rejects_empty_signing_secret_id(self) -> None:
        with pytest.raises(ValidationError):
            make_task(signing_secret_id="")

    def test_rejects_invalid_target_url(self) -> None:
        with pytest.raises(ValidationError):
            make_task(target_url="not-a-url")

    def test_rejects_negative_attempt_count(self) -> None:
        with pytest.raises(ValidationError):
            make_task(attempt_count=-1)

    def test_rejects_zero_max_attempts(self) -> None:
        with pytest.raises(ValidationError):
            make_task(max_attempts=0)

    def test_rejects_negative_max_attempts(self) -> None:
        with pytest.raises(ValidationError):
            make_task(max_attempts=-3)

    def test_rejects_attempt_count_exceeding_max_attempts(self) -> None:
        with pytest.raises(ValidationError):
            make_task(attempt_count=9, max_attempts=8)

    def test_allows_attempt_count_equal_to_max_attempts(self) -> None:
        task = make_task(attempt_count=8, max_attempts=8)
        assert task.attempt_count == task.max_attempts

    def test_defaults_created_at_to_timezone_aware_now(self) -> None:
        task = make_task()
        assert task.created_at.tzinfo is not None

    def test_payload_must_be_a_mapping(self) -> None:
        with pytest.raises(ValidationError):
            make_task(payload="not-a-dict")  # type: ignore[arg-type]

    def test_validate_assignment_enforced_on_mutation(self) -> None:
        task = make_task()
        with pytest.raises(ValidationError):
            task.attempt_count = -5

    def test_last_status_code_and_error_default_to_none(self) -> None:
        task = make_task()
        assert task.last_status_code is None
        assert task.last_error is None
        assert task.next_attempt_at is None


class TestIsValidTransitionPureFunction:
    @pytest.mark.parametrize(
        "from_status,to_status",
        [
            (TaskStatus.PENDING, TaskStatus.IN_FLIGHT),
            (TaskStatus.IN_FLIGHT, TaskStatus.DELIVERED),
            (TaskStatus.IN_FLIGHT, TaskStatus.RETRY_SCHEDULED),
            (TaskStatus.IN_FLIGHT, TaskStatus.DEAD_LETTERED),
            (TaskStatus.RETRY_SCHEDULED, TaskStatus.IN_FLIGHT),
        ],
    )
    def test_valid_transitions(
        self, from_status: TaskStatus, to_status: TaskStatus
    ) -> None:
        assert is_valid_transition(from_status, to_status) is True

    @pytest.mark.parametrize(
        "from_status,to_status",
        [
            (TaskStatus.PENDING, TaskStatus.DELIVERED),
            (TaskStatus.PENDING, TaskStatus.DEAD_LETTERED),
            (TaskStatus.PENDING, TaskStatus.RETRY_SCHEDULED),
            (TaskStatus.IN_FLIGHT, TaskStatus.PENDING),
            (TaskStatus.DELIVERED, TaskStatus.IN_FLIGHT),
            (TaskStatus.DELIVERED, TaskStatus.RETRY_SCHEDULED),
            (TaskStatus.DELIVERED, TaskStatus.PENDING),
            (TaskStatus.DEAD_LETTERED, TaskStatus.IN_FLIGHT),
            (TaskStatus.DEAD_LETTERED, TaskStatus.PENDING),
            (TaskStatus.RETRY_SCHEDULED, TaskStatus.DELIVERED),
            (TaskStatus.RETRY_SCHEDULED, TaskStatus.DEAD_LETTERED),
            (TaskStatus.RETRY_SCHEDULED, TaskStatus.PENDING),
        ],
    )
    def test_invalid_transitions(
        self, from_status: TaskStatus, to_status: TaskStatus
    ) -> None:
        assert is_valid_transition(from_status, to_status) is False


class TestTransitionToMethod:
    def test_valid_transition_mutates_status(self) -> None:
        task = make_task()
        assert task.status == TaskStatus.PENDING
        task.transition_to(TaskStatus.IN_FLIGHT)
        assert task.status == TaskStatus.IN_FLIGHT

    def test_full_success_lifecycle(self) -> None:
        task = make_task()
        task.transition_to(TaskStatus.IN_FLIGHT)
        task.transition_to(TaskStatus.DELIVERED)
        assert task.status == TaskStatus.DELIVERED
        assert task.is_terminal() is True

    def test_full_retry_then_success_lifecycle(self) -> None:
        task = make_task()
        task.transition_to(TaskStatus.IN_FLIGHT)
        task.transition_to(TaskStatus.RETRY_SCHEDULED)
        task.transition_to(TaskStatus.IN_FLIGHT)
        task.transition_to(TaskStatus.DELIVERED)
        assert task.status == TaskStatus.DELIVERED

    def test_full_dlq_lifecycle(self) -> None:
        task = make_task()
        task.transition_to(TaskStatus.IN_FLIGHT)
        task.transition_to(TaskStatus.DEAD_LETTERED)
        assert task.is_terminal() is True

    def test_multiple_retry_cycles_before_success(self) -> None:
        task = make_task()
        task.transition_to(TaskStatus.IN_FLIGHT)
        for _ in range(3):
            task.transition_to(TaskStatus.RETRY_SCHEDULED)
            task.transition_to(TaskStatus.IN_FLIGHT)
        task.transition_to(TaskStatus.DELIVERED)
        assert task.status == TaskStatus.DELIVERED

    def test_invalid_transition_raises_task_validation_error(self) -> None:
        task = make_task()
        with pytest.raises(TaskValidationError):
            task.transition_to(TaskStatus.DELIVERED)

    def test_status_unchanged_after_failed_transition(self) -> None:
        task = make_task()
        with pytest.raises(TaskValidationError):
            task.transition_to(TaskStatus.DELIVERED)
        assert task.status == TaskStatus.PENDING

    def test_transition_out_of_delivered_terminal_state_raises(self) -> None:
        task = make_task()
        task.transition_to(TaskStatus.IN_FLIGHT)
        task.transition_to(TaskStatus.DELIVERED)
        with pytest.raises(TaskValidationError):
            task.transition_to(TaskStatus.IN_FLIGHT)

    def test_transition_out_of_dead_lettered_terminal_state_raises(self) -> None:
        task = make_task()
        task.transition_to(TaskStatus.IN_FLIGHT)
        task.transition_to(TaskStatus.DEAD_LETTERED)
        with pytest.raises(TaskValidationError):
            task.transition_to(TaskStatus.PENDING)


class TestRedisHashRoundTrip:
    def test_round_trip_minimal_task(self) -> None:
        original = make_task()
        restored = WebhookTask.from_redis_hash(original.to_redis_hash())
        assert restored == original

    def test_round_trip_preserves_task_id_as_uuid(self) -> None:
        original = make_task()
        restored = WebhookTask.from_redis_hash(original.to_redis_hash())
        assert isinstance(restored.task_id, UUID)
        assert restored.task_id == original.task_id

    def test_round_trip_with_all_optional_fields_populated(self) -> None:
        original = make_task(
            status=TaskStatus.IN_FLIGHT,
            attempt_count=3,
            next_attempt_at=datetime(2025, 6, 1, 12, 0, 0, tzinfo=timezone.utc),
            last_status_code=503,
            last_error="Service Unavailable: upstream timeout after 10s",
        )
        restored = WebhookTask.from_redis_hash(original.to_redis_hash())
        assert restored == original
        assert restored.next_attempt_at == original.next_attempt_at
        assert restored.last_status_code == 503
        assert restored.last_error == original.last_error

    def test_round_trip_with_optional_fields_unset(self) -> None:
        original = make_task()
        assert original.next_attempt_at is None
        assert original.last_status_code is None
        assert original.last_error is None

        restored = WebhookTask.from_redis_hash(original.to_redis_hash())
        assert restored.next_attempt_at is None
        assert restored.last_status_code is None
        assert restored.last_error is None

    def test_round_trip_preserves_nested_payload_structure(self) -> None:
        original = make_task(
            payload={
                "id": "evt_999",
                "nested": {"a": 1, "b": [1, 2, 3]},
                "flag": True,
                "note": None,
            }
        )
        restored = WebhookTask.from_redis_hash(original.to_redis_hash())
        assert restored.payload == original.payload

    def test_to_redis_hash_returns_all_string_values(self) -> None:
        task = make_task(
            next_attempt_at=datetime(2025, 6, 1, 12, 0, 0, tzinfo=timezone.utc),
            last_status_code=500,
        )
        hash_data = task.to_redis_hash()
        assert all(isinstance(v, str) for v in hash_data.values())

    def test_to_redis_hash_uses_empty_string_sentinel_for_none(self) -> None:
        task = make_task()
        hash_data = task.to_redis_hash()
        assert hash_data["next_attempt_at"] == ""
        assert hash_data["last_status_code"] == ""
        assert hash_data["last_error"] == ""

    def test_round_trip_preserves_status_and_attempt_count(self) -> None:
        original = make_task(status=TaskStatus.IN_FLIGHT, attempt_count=2)
        restored = WebhookTask.from_redis_hash(original.to_redis_hash())
        assert restored.status == TaskStatus.IN_FLIGHT
        assert restored.attempt_count == 2

    def test_double_round_trip_is_stable(self) -> None:
        original = make_task()
        once = WebhookTask.from_redis_hash(original.to_redis_hash())
        twice = WebhookTask.from_redis_hash(once.to_redis_hash())
        assert once == twice == original
