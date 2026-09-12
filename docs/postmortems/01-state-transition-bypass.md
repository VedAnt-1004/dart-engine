# Postmortem: State-Transition Bypass in Test Harness

**Severity:** Low (test-only defect; no production code change required)
**Status:** Resolved
**Detected via:** Real `pytest` execution (first full run of the Phase 4 test suite)
**Affected component(s):** `tests/integration/test_dispatcher_e2e.py`, `AsyncDispatcher.dispatch` / `handle_outcome`

## Summary

Six tests in the first Phase 4 test run failed with `dart.core.exceptions.TaskValidationError: Illegal status transition: PENDING -> DELIVERED` (and the equivalent for `RETRY_SCHEDULED` and `DEAD_LETTERED`). The `WebhookTask` state machine correctly rejected the transition; the tests were constructing an invalid precondition before exercising it.

## Timeline / Detection

The first `python -m pytest` run against the completed Phase 4 implementation returned `7 failed, 171 passed`. Six of the seven failures shared an identical root cause; the seventh (an unrelated `httpx.ASGITransport` timeout limitation) is covered in a separate postmortem.

## Root Cause

`WebhookTask`'s lifecycle state machine (locked in Phase 1) only permits `DELIVERED` / `RETRY_SCHEDULED` / `DEAD_LETTERED` to be reached from `IN_FLIGHT` — never directly from `PENDING`. The production code path, `AsyncDispatcher.process_one` (at the time, `_process_one`), correctly performs the `PENDING`/`RETRY_SCHEDULED` → `IN_FLIGHT` transition immediately before calling `dispatch()`.

Several integration tests called `dispatch()` and `handle_outcome()` directly — deliberately, to assert on the intermediate `DispatchResult` before checking the resulting task status — and simply omitted the transition step that the real production path performs first. The state machine's guard fired exactly as designed; it had correctly caught a test-authoring bug rather than a production defect.

The seventh test that specifically exercised crash recovery (`TestCrashRecovery`) had additionally *hand-rolled* the dispatch → outcome → ack sequence rather than calling the real pipeline method — meaning it inherited this same bug independently, which was itself a signal that the pipeline method needed to be easier to reach for correctly than to reimplement incorrectly.

## Resolution

Two changes, addressing both the immediate failures and the pattern that produced them:

1. **`_process_one` was renamed to public `process_one`.** This was not a cosmetic change — it was made specifically so that tests needing the correct claim → transition → dispatch → outcome → ack sequence would call the real, already-correct implementation rather than reimplement it (incorrectly, as `TestCrashRecovery` had just demonstrated).
2. The five tests that legitimately needed `dispatch()`/`handle_outcome()` called separately (to assert on `DispatchResult` before the state transition) had the missing `task.transition_to(TaskStatus.IN_FLIGHT)` call added, mirroring exactly what `process_one` does before dispatching.

`TestCrashRecovery` itself was refactored to call the real `process_one()` directly, eliminating the hand-rolled sequence entirely.

## Impact

None in production. The bug existed only in test setup; the state machine guard being violated was itself evidence the guard was working correctly. No dispatch, retry, or DLQ logic was incorrect at any point during this defect's lifetime.

## Lessons Learned

- When a class exposes both a composed "do the whole thing" method and its individual steps as separately callable public methods, tests will eventually call the steps directly and skip an implicit precondition the composed method enforces. Making the composed method the *easy* path (public, well-named, documented) rather than a private implementation detail is what keeps that path from being bypassed by accident.
- A test that reimplements a production code path instead of calling it inherits every future bug in that path silently, and — as happened here — can independently rediscover the exact same class of bug the real path had already been fixed to avoid.
