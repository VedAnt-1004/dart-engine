# dart-engine

**DART (Dispatch Async Relay & Transport)** — a high-throughput, fault-tolerant
webhook delivery engine in asynchronous Python.

## Local development (fakeredis-backed test suite)

```bash
pip install -e ".[dev]"
pytest -v
```

## Real infrastructure (Docker) — black-box crash-recovery test

The `pytest` suite above runs entirely against `fakeredis`. To validate
DART against **real** Redis and **real**, separately-crashable
`dart-worker`/`dart-scheduler`/`dart-api` processes — the Phase 4
roadmap exit criterion that fakeredis can only approximate — use the
Docker Compose stack:

```bash
docker compose up -d --build      # redis + dart-api + dart-worker + dart-scheduler + mock-receiver
curl http://localhost:8000/healthz
docker compose down -v
```

Or run the automated crash-recovery scenario end-to-end (brings the
stack up, posts an event, `SIGKILL`s `dart-worker` mid-dispatch, starts
a fresh worker, and confirms it recovers the task via `XAUTOCLAIM`, then
tears everything down):

```bash
python scripts/blackbox_crash_recovery_test.py
```

Requires Docker Desktop (or another `docker compose`-v2-compatible
engine) running locally.

See the architecture blueprint for system design, Redis schema, and the
phased implementation roadmap. All four implementation phases (Foundation,
Ingestion API, Resilience, Dispatch Worker) are complete.
