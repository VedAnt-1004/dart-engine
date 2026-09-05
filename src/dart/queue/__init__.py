"""Redis-backed queue, job store, and idempotency primitives.

Phase 2 implements the producer side only (job persistence, idempotency
guard, ready-stream enqueue). Consumer-side stream operations
(XREADGROUP / XACK / XCLAIM, consumer-group creation) are added in
Phase 4 alongside the dispatch worker that drives them.
"""
