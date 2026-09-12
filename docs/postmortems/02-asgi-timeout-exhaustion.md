# Postmortem: `httpx.ASGITransport` Cannot Enforce Timeouts

**Severity:** Low (test-coverage gap; no production code defect)
**Status:** Resolved
**Detected via:** Real `pytest` execution (first full run of the Phase 4 test suite)
**Affected component(s):** `tests/integration/test_mock_receiver_chaos.py`

## Summary

A test intended to prove that DART classifies a slow/unresponsive upstream as a retryable transport failure configured the dispatcher's HTTP client with a 2-second timeout against a mock receiver instructed to sleep for 5 seconds. The request completed successfully at ~5 seconds with a 200 response — the configured timeout never fired at all — because `httpx.ASGITransport` has no timeout-enforcement mechanism, a fact that is not evident from reading the calling code and was discovered only by running the test.

## Timeline / Detection

Same test run as the state-transition-bypass postmortem: `7 failed, 171 passed`. This was the one failure among the seven unrelated to the state-machine issue.

```
assert result.success is False
E   assert True is False
E    +  where True = DispatchResult(success=True, status_code=200, latency_ms=5013.24...).success
```

The captured log confirmed the request actually took ~5 seconds (matching the mock's sleep duration exactly) and returned 200 — i.e., the client waited out the full delay rather than timing out at 2 seconds as configured.

## Root Cause

`httpx.ASGITransport` calls the target ASGI application's coroutine directly, in-process. There is no real socket, no real connection, and therefore no transport-level I/O for `httpx.Timeout` to attach to — the entire request/response cycle is a single Python `await` chain with nothing resembling a network boundary in the middle. httpx's timeout machinery is implemented at the transport layer (specifically, in the real `httpx.AsyncHTTPTransport`/`httpcore` stack, which does have actual connect/read/write operations to bound); `ASGITransport` simply does not implement that machinery, because it was built for functional request/response testing, not for simulating network timing behavior.

This is a limitation of the test harness chosen, not of DART's actual timeout configuration — the real `httpx.AsyncHTTPTransport` used in production (`worker/runner.py`'s `_build_http_client`) enforces the configured timeouts correctly; only the in-process ASGI test double cannot exercise that enforcement.

## Resolution

- The original test was repurposed into `TestTimeoutBehaviorLimitation`, which verifies only that the mock receiver's own sleep-then-respond endpoint behaves as intended — explicitly *not* a test of DART's timeout handling — with a docstring recording why the original assertion was invalid.
- A correct replacement, `TestTransportLevelFailures` in `test_dispatcher_e2e.py`, was added using `respx`'s ability to raise `httpx.ReadTimeout` (and `httpx.ConnectError`) as a direct mocked side effect. This requires no real elapsed time and correctly exercises `AsyncDispatcher.dispatch`'s actual exception-handling branch, which is the thing that needed testing in the first place.

## Impact

None in production. DART's real timeout configuration (`httpx.Timeout` passed to the real `AsyncHTTPTransport`) was never actually broken; only the specific test harness used to attempt to verify it was structurally incapable of doing so. This was caught before it could create false confidence in untested behavior.

## Lessons Learned

- In-process ASGI transports (`httpx.ASGITransport`, and anything built the same way) are well-suited to functional request/response verification but categorically cannot validate anything timing- or socket-level: timeouts, TCP connection behavior, TLS handshake behavior. This is not a bug to work around within that harness — it is a hard boundary of what the harness can test at all.
- The correct fix for "this test harness can't exercise the thing I need to test" is not a more elaborate mock inside the same harness — it's choosing a different harness (here, `respx`'s exception-injection, which operates at the layer where the real behavior actually lives) for that specific class of assertion.
