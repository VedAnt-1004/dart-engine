# Postmortem: Exception-Forwarding Gap Routed Every Transport Failure to the DLQ

**Severity:** High (genuine production-correctness defect, not test-only)
**Status:** Resolved
**Detected via:** Real `pytest` execution, exercising a real transport-level exception end to end
**Affected component(s):** `AsyncDispatcher.handle_outcome`, `DispatchResult`

## Summary

`handle_outcome` hardcoded `exception=None` when calling `RetryPolicy.should_retry`, regardless of what `dispatch()` had actually caught. For a genuine transport-level failure (timeout, connection refused — anything where no HTTP response was ever received), `status_code` is also `None` by construction. With both `status_code` and `exception` absent, `should_retry` fell through to its "unclassifiable failure, do not retry" branch — meaning **every timeout and every connection error, in real operation, would have been sent straight to the dead-letter queue after a single attempt**, regardless of the configured `max_attempts`, instead of being scheduled for retry as intended.

## Timeline / Detection

Discovered in the second post-fix `pytest` run for Phase 4, specifically by two new tests (`TestTransportLevelFailures::test_read_timeout_is_a_retryable_transport_failure` and `test_connect_error_is_a_retryable_transport_failure`) added to properly exercise transport-exception classification via `respx`-injected exceptions (see the ASGI-timeout postmortem for why the original timeout test couldn't do this). Both new tests failed:

```
status = await dispatcher.handle_outcome(task, result)
assert status == TaskStatus.RETRY_SCHEDULED
AssertionError: assert <TaskStatus.DEAD_LETTERED> == <TaskStatus.RETRY_SCHEDULED>
```

## Root Cause

`DispatchResult` carried the caught exception's **string message** (`error: str | None`, used for logging and for `WebhookTask.last_error`), but never the exception **object** itself. `handle_outcome` therefore had no way to forward a real exception reference to `RetryPolicy.should_retry(attempt, status_code, exception)` — whose contract, already correctly implemented and unit-tested in isolation, treats `exception is not None` as unconditionally retryable regardless of `status_code`. The wiring between two individually-correct components was the defect: `RetryPolicy` passed all 25 of its own unit tests in isolation; `dispatch()`'s exception handling looked correct in isolation; the *data flow between them* silently discarded the one piece of information (`exception`) the retry decision actually depended on for this failure class.

## Resolution

- Added `exception: Exception | None = None` to `DispatchResult`, populated with the real caught exception in `dispatch()`'s `except httpx.HTTPError` branch.
- Changed `handle_outcome` to forward `result.exception` instead of the hardcoded `None`.
- Before relying on the full (unavailable in the immediate development environment) test run to confirm, the fix was verified directly against the real, pure-standard-library `RetryPolicy.should_retry` logic: confirmed the bug reproduced exactly with `exception=None`, and resolved with the real exception object forwarded — isolating the fix from any dependency on the harder-to-run integration path before trusting it.

## Impact

This was a real, latent production defect, not a test artifact. Had it shipped, the practical effect would have been the retry subsystem silently not functioning for the single most common class of delivery failure — transient network errors and timeouts — for every dispatch attempt made before this fix, converting what should have been a brief backoff-and-retry into an immediate, permanent dead-letter for any receiver experiencing an ordinary transient blip.

## Lessons Learned

- Unit-testing each component of a pipeline in isolation (`RetryPolicy` on its own, `dispatch()`'s HTTP handling on its own) does not verify the pipeline. The defect here existed entirely in the *interface* between two correctly-implemented components — a data field silently dropped in transit — and was only visible to a test that exercised both together with a real, concrete failure input.
- When a downstream function's behavior branches meaningfully on a field (`exception is not None` vs. `None`), a caller passing a hardcoded default for that field is a specific, checkable smell worth looking for directly in code review — not just something to hope integration tests catch.
