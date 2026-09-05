"""Integration tests for `POST /api/v1/events`.

Exit criteria (per the Phase 2 roadmap): end-to-end POST -> Redis stream
entry, no duplicate dispatch on repeated idempotency key.
"""

from __future__ import annotations

import json
from typing import Any

import fakeredis
from fastapi.testclient import TestClient

VALID_BODY: dict[str, Any] = {
    "event_type": "invoice.paid",
    "target_url": "https://example.com/webhooks/dart",
    "payload": {"id": "evt_123", "amount": 4200, "currency": "usd"},
    "idempotency_key": "idem_abc123",
    "signing_secret_id": "secret_ref_001",
}


def _post(api_client: TestClient, **overrides: Any) -> Any:
    body = {**VALID_BODY, **overrides}
    return api_client.post("/api/v1/events", json=body)


class TestSuccessfulIngestion:
    def test_returns_202_with_pending_task(self, api_client: TestClient) -> None:
        response = _post(api_client, idempotency_key="idem_202_check")
        assert response.status_code == 202
        data = response.json()
        assert data["status"] == "PENDING"
        assert data["idempotency_replay"] is False
        assert "task_id" in data
        assert "created_at" in data

    def test_persists_job_hash_with_correct_fields(
        self, api_client: TestClient, verify_redis_client: fakeredis.FakeRedis
    ) -> None:
        response = _post(api_client, idempotency_key="idem_job_hash_check")
        task_id = response.json()["task_id"]

        job_hash = verify_redis_client.hgetall(f"dart:job:{task_id}")
        assert job_hash["event_type"] == "invoice.paid"
        assert job_hash["status"] == "PENDING"
        assert job_hash["attempt_count"] == "0"
        assert json.loads(job_hash["payload"]) == VALID_BODY["payload"]

    def test_enqueues_a_ready_stream_entry(
        self, api_client: TestClient, verify_redis_client: fakeredis.FakeRedis
    ) -> None:
        before = verify_redis_client.xlen("dart:queue:ready")
        response = _post(api_client, idempotency_key="idem_stream_check")
        after = verify_redis_client.xlen("dart:queue:ready")
        assert after == before + 1

        task_id = response.json()["task_id"]
        entries = verify_redis_client.xrange("dart:queue:ready")
        assert any(fields.get("task_id") == task_id for _, fields in entries)

    def test_respects_max_attempts_override(
        self, api_client: TestClient, verify_redis_client: fakeredis.FakeRedis
    ) -> None:
        response = _post(api_client, idempotency_key="idem_max_attempts", max_attempts=3)
        task_id = response.json()["task_id"]
        job_hash = verify_redis_client.hgetall(f"dart:job:{task_id}")
        assert job_hash["max_attempts"] == "3"

    def test_defaults_max_attempts_when_omitted(
        self, api_client: TestClient, verify_redis_client: fakeredis.FakeRedis
    ) -> None:
        response = _post(api_client, idempotency_key="idem_default_max_attempts")
        task_id = response.json()["task_id"]
        job_hash = verify_redis_client.hgetall(f"dart:job:{task_id}")
        assert job_hash["max_attempts"] == "8"


class TestIdempotency:
    def test_duplicate_idempotency_key_returns_original_task(
        self, api_client: TestClient
    ) -> None:
        first = _post(api_client, idempotency_key="idem_duplicate_test")
        second = _post(api_client, idempotency_key="idem_duplicate_test")

        assert first.status_code == 202
        assert second.status_code == 200
        assert second.json()["task_id"] == first.json()["task_id"]
        assert second.json()["idempotency_replay"] is True
        assert first.json()["idempotency_replay"] is False

    def test_duplicate_idempotency_key_does_not_double_enqueue(
        self, api_client: TestClient, verify_redis_client: fakeredis.FakeRedis
    ) -> None:
        _post(api_client, idempotency_key="idem_no_double_enqueue")
        before = verify_redis_client.xlen("dart:queue:ready")
        _post(api_client, idempotency_key="idem_no_double_enqueue")
        after = verify_redis_client.xlen("dart:queue:ready")
        assert after == before

    def test_duplicate_idempotency_key_does_not_overwrite_original_job(
        self, api_client: TestClient, verify_redis_client: fakeredis.FakeRedis
    ) -> None:
        first = _post(api_client, idempotency_key="idem_no_overwrite", payload={"v": 1})
        _post(api_client, idempotency_key="idem_no_overwrite", payload={"v": 2})

        task_id = first.json()["task_id"]
        job_hash = verify_redis_client.hgetall(f"dart:job:{task_id}")
        assert json.loads(job_hash["payload"]) == {"v": 1}

    def test_different_idempotency_keys_create_separate_tasks(
        self, api_client: TestClient
    ) -> None:
        first = _post(api_client, idempotency_key="idem_key_one")
        second = _post(api_client, idempotency_key="idem_key_two")
        assert first.json()["task_id"] != second.json()["task_id"]


class TestValidation:
    def test_rejects_invalid_target_url(self, api_client: TestClient) -> None:
        response = _post(api_client, target_url="not-a-url")
        assert response.status_code == 422

    def test_rejects_missing_event_type(self, api_client: TestClient) -> None:
        body = {k: v for k, v in VALID_BODY.items() if k != "event_type"}
        response = api_client.post("/api/v1/events", json=body)
        assert response.status_code == 422

    def test_rejects_missing_target_url(self, api_client: TestClient) -> None:
        body = {k: v for k, v in VALID_BODY.items() if k != "target_url"}
        response = api_client.post("/api/v1/events", json=body)
        assert response.status_code == 422

    def test_rejects_non_object_payload(self, api_client: TestClient) -> None:
        response = _post(api_client, payload="not-an-object")
        assert response.status_code == 422

    def test_rejects_empty_idempotency_key(self, api_client: TestClient) -> None:
        response = _post(api_client, idempotency_key="")
        assert response.status_code == 422

    def test_rejects_empty_event_type(self, api_client: TestClient) -> None:
        response = _post(api_client, event_type="")
        assert response.status_code == 422

    def test_rejects_zero_max_attempts(self, api_client: TestClient) -> None:
        response = _post(api_client, max_attempts=0)
        assert response.status_code == 422

    def test_accepts_optional_metadata(self, api_client: TestClient) -> None:
        response = _post(
            api_client,
            idempotency_key="idem_with_metadata",
            metadata={"source": "billing-service", "trace_id": "trc_1"},
        )
        assert response.status_code == 202


class TestHealth:
    def test_liveness(self, api_client: TestClient) -> None:
        response = api_client.get("/healthz")
        assert response.status_code == 200
        assert response.json() == {"status": "ok"}

    def test_readiness(self, api_client: TestClient) -> None:
        response = api_client.get("/readyz")
        assert response.status_code == 200
        assert response.json() == {"status": "ready"}
