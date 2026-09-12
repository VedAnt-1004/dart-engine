# ADR 0001: At-Least-Once Delivery Semantics

**Status:** Accepted
**Deciders:** DART core engineering
**Context date:** Phase 1 (architecture lock), reaffirmed through Phase 4 (dispatch worker) and Phase 2 productization (atomic ingestion)

## Context

DART delivers webhook events to third-party HTTP endpoints it does not control. A delivery attempt can fail, partially succeed, or have its outcome lost to the caller at any of several points: the network can drop the response after the receiver has already processed the request, the worker process can crash after receiving a 2xx but before acknowledging the corresponding Redis Stream entry, or the receiver itself can be slow enough to trigger a client-side timeout despite eventually completing the request server-side.

Three delivery-semantics models are available to a system in this position:

- **At-most-once** — attempt delivery once; if the outcome is uncertain, do not retry. Simple, but risks silent, permanent message loss on any transient failure or crash — unacceptable for a system whose entire purpose is reliable delivery.
- **Exactly-once** — guarantee each event is delivered to the receiver exactly one time, with no duplicates and no loss. This requires either a distributed transaction spanning DART and the receiver, or idempotency enforcement on the receiver's side that DART can verify before considering delivery complete. HTTP has no transactional semantics, and the receiver is a third party outside DART's control — DART cannot verify receiver-side idempotency, and cannot roll back a receiver's side effects if a post-delivery step fails. Exactly-once delivery to an arbitrary, uncontrolled HTTP endpoint is not achievable by the sending system alone.
- **At-least-once** — guarantee every event is eventually delivered (or exhausted to the DLQ after `max_attempts`), accepting that a given event may be delivered to the receiver more than once under specific failure conditions.

## Decision

**DART implements at-least-once delivery semantics.**

Mechanically, this rests on the ready-stream consumer-group design (`dart:queue:ready`, `XREADGROUP`/`XACK`/`XAUTOCLAIM`): a worker claims a stream entry, attempts dispatch, and only acknowledges the entry (`XACK`) after successfully recording the outcome (`DELIVERED`, `RETRY_SCHEDULED`, or `DEAD_LETTERED`) via `AsyncDispatcher.handle_outcome`. If the worker crashes after the receiver has already returned a 2xx but before that acknowledgment completes, the stream entry remains in the consumer group's Pending Entries List. A surviving or restarted worker's `claim_stale` call (backed by `XAUTOCLAIM`) reclaims it after `stale_min_idle_ms` and retries the dispatch from scratch — **including re-sending the HTTP request**, since from the system's point of view the first attempt's outcome was never durably recorded.

This is the same trade every mainstream webhook provider (Stripe, GitHub, Svix) makes, and it is deliberate, not an oversight: it is the strongest guarantee achievable without receiver-side cooperation, and it fails safe (toward redundant delivery) rather than unsafe (toward silent loss).

### Two distinct idempotency boundaries — do not conflate them

DART's `idempotency_key` (enforced atomically at ingestion via `EventIngestor`'s Lua script) guarantees **at most one task is created per unique idempotency key.** This is an *ingestion-time* guarantee: it prevents duplicate task creation if a caller retries their `POST /api/v1/events` call (e.g., after a client-side timeout on their end).

It does **not** guarantee at most one delivery per task. Those are separate concerns addressed at different layers, and treating ingestion-side idempotency as if it also solved delivery-side duplication is a real mistake to avoid when reasoning about this system.

## Consequences

**Positive:**
- No event is ever silently lost due to a worker crash, network partition, or process restart — every task is either eventually `DELIVERED` or explicitly routed to `DEAD_LETTERED` for manual inspection. This is verified end-to-end by `scripts/blackbox_crash_recovery_test.py` against real Docker infrastructure (a hard `SIGKILL` mid-dispatch, recovered by a freshly started worker).
- The system fails toward the safer of the two failure modes available to it (over-delivery, not under-delivery).

**Negative — and this is the load-bearing consequence of this ADR:**
- **Receivers must implement their own idempotency handling if duplicate side effects matter to them** (e.g., a payment receiver that must not double-charge). DART cannot prevent duplicate delivery in all failure modes, by the argument above.
- **Known gap, not yet closed:** DART does not currently include a stable delivery identifier in the outbound request. `AsyncDispatcher.dispatch()` sends `Content-Type`, `X-DART-Signature`, and `X-DART-Event-Type` headers, but no `X-DART-Delivery-Id` (or similar, mapped to `task_id`). Without this, a receiver has no DART-provided value to key its own deduplication logic on — it would need to derive one from the payload itself, which is not always possible. **Recommended follow-up:** add a stable delivery-identifier header so receivers have a concrete, DART-guaranteed key to deduplicate against. This does not change DART's own semantics; it makes receiver-side idempotency handling actually practical to implement, which today it is not.

## Alternatives Considered

| Alternative | Why rejected |
|---|---|
| At-most-once | Silent data loss on any transient failure; directly contradicts the system's purpose. |
| Exactly-once via distributed transaction | Not achievable across an HTTP boundary to an uncontrolled third party; no rollback mechanism exists for a receiver's side effects. |
| Pre-send deduplication log (check-then-send) | Does not close the race: a crash between the log-check and the actual send reproduces the exact same uncertainty this ADR already accepts. Adds a Redis round-trip per attempt for no closed gap. |
