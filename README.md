# DART (Distributed Asynchronous Reliable Transport)

High-throughput, fault-tolerant webhook delivery engine, engineered for production.

**264/264 tests passing · ~178 req/s under connection-pool saturation**

## Core Guarantees

- **Atomic Ingestion** — Redis Lua scripts eliminate TOCTOU races and unbounded duplicate dispatch.
- **Crash-Recoverable Workers** — Redis Streams (`XREADGROUP` + `XAUTOCLAIM`) reclaim tasks if a worker is `SIGKILL`'d mid-dispatch.
- **Dual-Layer SSRF Defense** — rejects local IPs at ingestion; a custom `httpx.AsyncBaseTransport` blocks DNS-rebinding before connection.
- **Distributed Circuit Breaking** — atomic per-domain `CLOSED → OPEN` transitions isolate failing endpoints, no lost updates.

## Architecture

```
[Caller] ─POST→ [API] ─(Lua Script)→ [Redis Streams/AOF] ─(XREADGROUP)→ [Workers] ─POST→ [External Receivers]
```

## Quick Start

**Production**
```bash
docker compose up -d --build
```

**Dispatch an event**
```bash
curl -X POST http://localhost:8000/api/v1/events \
  -H "Content-Type: application/json" \
  -d '{
        "event_type": "invoice.paid",
        "target_url": "https://receiver.example.com/webhook",
        "payload": {"invoice_id": "inv_123"},
        "idempotency_key": "evt_001",
        "signing_secret_id": "acct_42"
      }'
```

**Local dev**
```bash
pip install -e ".[dev]"
pytest -v
```

## Documentation

- `docs/adr/` — delivery-semantics and durability trade-offs
- `docs/postmortems/` — 5 real architectural gaps found and patched during testing
