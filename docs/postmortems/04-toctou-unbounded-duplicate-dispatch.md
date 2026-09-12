# Postmortem: TOCTOU Race Enabling Unbounded Duplicate Dispatch

**Severity:** High (genuine production-correctness defect; unusually severe failure mode)
**Status:** Resolved
**Detected via:** Architectural review during Phase 2 (Atomic Ingestion) design, prior to implementation — not caught by a failing test, since no existing test simulated a process crash mid-ingestion
**Affected component(s):** Original `POST /api/v1/events` ingestion flow (`IdempotencyGuard.claim` → `JobStore.save` → `ReadyQueue.enqueue`, three sequential Redis round trips)

## Summary

The original ingestion flow performed idempotency-claim, job-record persistence, and ready-stream enqueue as three separate, sequential Redis operations. If the API process crashed between the first and second step, the idempotency key would be left permanently pointing at a `task_id` with no corresponding job record. The existing fallback logic for "idempotency key claimed but job missing" treated this as `fall through and create a new task` — but crucially, it never corrected the idempotency key's mapping to point at the new task. Every subsequent request using that same idempotency key would hit this exact branch again, creating another new, equally orphaned task. This was not a one-time orphan: it was **unbounded duplicate task creation and dispatch for that key**, persisting until the idempotency key's TTL naturally expired (up to 24 hours by default configuration).

## Timeline / Detection

Identified during design review for Phase 2's atomic-ingestion work, when explicitly reasoning through every point at which the three-step sequence could be interrupted by a process crash. This class of bug is inherently difficult to catch via testing without deliberately injecting a process kill at an exact, narrow point mid-request — no test in the suite at the time did this, and none would have caught it without being specifically designed to.

## Root Cause

Non-atomicity across a multi-step operation performed at the application layer against shared state, where the fallback behavior for the resulting inconsistent state ("job missing for a claimed key") was itself incorrect: it silently created a new inconsistent state instead of either repairing the existing one or failing loudly.

## Resolution

The entire claim + persist + enqueue sequence was collapsed into a single atomic Redis Lua script (`EventIngestor` / `_INGEST_EVENT_SCRIPT`), executed via `EVAL`. Redis guarantees a Lua script runs to completion as an indivisible unit with respect to all other commands and scripts — there is no process-crash window *within* the script's execution from any other client's perspective, and the API process crashing before the script call completes simply means the call never happened at all (no partial state is possible, since Redis either received and fully executed the atomic operation or did not receive it).

This is a structural fix, not a narrowed window: the failure mode is eliminated, not made statistically rarer. A residual, much narrower edge case (the idempotency key's TTL expiring in the exact interval between two separate atomic script calls) is handled explicitly within the same script by reclaiming the key and proceeding as a fresh ingestion — verified via a direct Python transliteration of the script's logic, executed against four scenarios: fresh ingestion, a duplicate call with a different payload (confirming the original job's data is never overwritten and a second job is never written), independent-key isolation, and the narrow TTL-race fallback.

The route's behavior for the now-genuinely-unreachable "claimed but missing job" state was also changed: rather than silently falling through to create another orphaned task (the original bug), it now raises a loud `500`, on the reasoning that if this state is ever observed post-fix, it indicates Redis was mutated out-of-band (e.g., a manual deletion) rather than a normal race — and surfacing that loudly is preferable to perpetuating incorrect behavior silently.

## Impact

This was a severe defect in the original design, caught before deployment rather than in production. Its severity comes from two properties working together: the trigger condition (a process crash in one specific narrow window) is rare enough that it could plausibly go undetected for a long time in a running system, while its consequence (unbounded, silent, ongoing duplicate task creation for the affected key) compounds for as long as the idempotency TTL remains active — potentially producing dozens of duplicate deliveries for a single logical event before the key finally expires, with no single obvious point of failure to diagnose from the symptoms alone.

## Lessons Learned

- Any "claim, then act" sequence against shared state needs either true atomicity or an explicitly designed and tested reconciliation path for every point a crash could occur between steps. "Fall through and treat as fresh" is close to always the wrong default for an idempotency check's crash-recovery branch, because it is precisely the case where correctness matters most that this default silently abandons it.
- This class of bug is not reliably found by conventional testing — it requires either deliberately injecting a process kill at a specific point mid-operation, or (as happened here) explicitly reasoning through every crash point during design review before the code is trusted. Both are worth doing for any multi-step operation against an idempotency guarantee; neither should be skipped in favor of "the test suite is green."
