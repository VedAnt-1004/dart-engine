#!/usr/bin/env python3
"""Black-box crash-recovery test for DART, run against real Docker
infrastructure (real Redis, real separate dart-api/dart-worker/
dart-scheduler/mock-receiver containers) — NOT part of the pytest suite,
since it needs Docker, takes tens of seconds, and mutates real
containers rather than in-memory test doubles.

This is the actual Phase 4 roadmap exit criterion:

    "a worker-crash-mid-processing simulation validating XCLAIM
    recovery" against docker-compose infrastructure.

`tests/integration/test_dispatcher_e2e.py::TestCrashRecovery` validates
the same underlying mechanism against fakeredis, in-process — a
legitimate test of the logic, but not proof that it survives an actual
process getting SIGKILLed mid-`await`. This script is that proof.

What it does:
    1. Brings up the full stack (`docker compose up -d --build`).
    2. POSTs an event whose target_url points at the mock receiver with
       a `timeout` behavior — the receiver will sleep for several
       seconds before responding, giving a reliable window in which
       dart-worker is actually mid-dispatch (blocked awaiting the HTTP
       response), not just idle.
    3. Waits briefly for dart-worker to claim the task, then SIGKILLs
       the dart-worker container outright — no graceful shutdown, a
       real crash.
    4. Starts a fresh dart-worker container (a genuinely different
       consumer, per `--force-recreate`).
    5. Polls Redis directly for the task to reach DELIVERED, proving
       the fresh worker reclaimed the abandoned entry via XAUTOCLAIM
       and completed it.
    6. Tears the stack down (`docker compose down -v`), even on failure.

Usage:
    python scripts/blackbox_crash_recovery_test.py

Requires: Docker Desktop (or another docker-compose-v2-compatible
engine) running locally, and this project's runtime dependencies
installed (`pip install -e .` or `-e ".[dev]"`) for the `httpx`/`redis`
clients this script itself uses.
"""

from __future__ import annotations

import subprocess
import sys
import time
import uuid

import httpx
import redis

COMPOSE_CMD = ["docker", "compose"]
API_URL = "http://localhost:8000"
REDIS_URL = "redis://localhost:6379/0"
SIGNING_SECRET_ID = "blackbox_test_secret"

# How long the mock receiver sleeps before responding: long enough that
# we can reliably kill the worker while it's genuinely mid-request, but
# short enough that the recovering worker's own attempt doesn't stall
# the script for too long.
MOCK_SLEEP_SECONDS = 6
TIME_TO_LET_WORKER_CLAIM_SECONDS = 1.5
MAX_WAIT_FOR_DELIVERY_SECONDS = 40


def run(cmd: list[str]) -> None:
    print(f"$ {' '.join(cmd)}")
    subprocess.run(cmd, check=True)


def check_docker_available() -> None:
    try:
        subprocess.run(
            COMPOSE_CMD + ["version"],
            check=True,
            capture_output=True,
        )
    except FileNotFoundError:
        print("ERROR: 'docker' was not found on PATH. Install Docker Desktop and retry.")
        raise SystemExit(1)
    except subprocess.CalledProcessError as exc:
        print("ERROR: 'docker compose version' failed — is Docker Desktop running?")
        print(exc.stderr.decode(errors="replace") if exc.stderr else "")
        raise SystemExit(1)


def wait_for_redis(client: redis.Redis, timeout_seconds: float = 30) -> None:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        try:
            if client.ping():
                return
        except redis.exceptions.ConnectionError:
            pass
        time.sleep(1)
    raise TimeoutError("Redis did not become reachable within the timeout")


def wait_for_api(timeout_seconds: float = 30) -> None:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        try:
            response = httpx.get(f"{API_URL}/healthz", timeout=2)
            if response.status_code == 200:
                return
        except httpx.HTTPError:
            pass
        time.sleep(1)
    raise TimeoutError("dart-api did not become reachable within the timeout")


def main() -> int:
    check_docker_available()

    print("=== DART black-box crash-recovery test ===")
    run(COMPOSE_CMD + ["up", "-d", "--build"])

    try:
        redis_client = redis.Redis.from_url(REDIS_URL, decode_responses=True)

        print("Waiting for redis...")
        wait_for_redis(redis_client)
        print("Waiting for dart-api...")
        wait_for_api()

        idempotency_key = f"blackbox-{uuid.uuid4()}"
        payload = {
            "event_type": "blackbox.crash_recovery_test",
            "target_url": (
                "http://mock-receiver:8000/webhook"
                f"?behavior=timeout&timeout_seconds={MOCK_SLEEP_SECONDS}"
            ),
            "payload": {"probe": "crash-recovery"},
            "idempotency_key": idempotency_key,
            "signing_secret_id": SIGNING_SECRET_ID,
            "max_attempts": 5,
        }

        print(f"Posting event (idempotency_key={idempotency_key})...")
        response = httpx.post(f"{API_URL}/api/v1/events", json=payload, timeout=5)
        response.raise_for_status()
        task_id = response.json()["task_id"]
        print(f"task_id = {task_id}")

        print(f"Waiting {TIME_TO_LET_WORKER_CLAIM_SECONDS}s for dart-worker to claim it...")
        time.sleep(TIME_TO_LET_WORKER_CLAIM_SECONDS)

        job_key = f"dart:job:{task_id}"
        status_before_kill = redis_client.hget(job_key, "status")
        print(f"Task status just before kill: {status_before_kill!r} (expect IN_FLIGHT)")
        if status_before_kill != "IN_FLIGHT":
            print(
                "WARNING: expected IN_FLIGHT at this point — the timing window may be "
                "too tight (or too loose) on this machine. Continuing anyway; if the "
                "test fails below, try increasing TIME_TO_LET_WORKER_CLAIM_SECONDS."
            )

        print("Hard-killing dart-worker (SIGKILL — simulating a real crash, no graceful shutdown)...")
        run(COMPOSE_CMD + ["kill", "-s", "SIGKILL", "dart-worker"])

        print("Starting a fresh dart-worker container to recover the abandoned task...")
        run(COMPOSE_CMD + ["up", "-d", "--force-recreate", "dart-worker"])

        print(f"Polling Redis for up to {MAX_WAIT_FOR_DELIVERY_SECONDS}s for DELIVERED status...")
        deadline = time.monotonic() + MAX_WAIT_FOR_DELIVERY_SECONDS
        final_status = None
        while time.monotonic() < deadline:
            final_status = redis_client.hget(job_key, "status")
            if final_status in ("DELIVERED", "DEAD_LETTERED"):
                break
            time.sleep(1)

        print(f"Final status: {final_status!r}")
        if final_status == "DELIVERED":
            print("PASS: a freshly started worker recovered and completed the crashed task.")
            return 0

        print(f"FAIL: expected DELIVERED, got {final_status!r}")
        return 1

    finally:
        print("Tearing down (docker compose down -v)...")
        run(COMPOSE_CMD + ["down", "-v"])


if __name__ == "__main__":
    sys.exit(main())
