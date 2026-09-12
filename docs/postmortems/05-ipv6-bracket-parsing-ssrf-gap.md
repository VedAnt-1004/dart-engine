# Postmortem: IPv6 Bracket-Parsing Gap Bypassed Both SSRF Defense Layers Simultaneously

**Severity:** High (security control bypass, found in both defense layers at once)
**Status:** Resolved
**Detected via:** Real `pytest` execution — one parametrized case out of eight failed
**Affected component(s):** `dart.security.ssrf.is_blocked_literal_host` (ingestion-time), `dart.worker.ssrf_transport.SSRFSafeTransport` (dispatch-time)

## Summary

The ingestion-time SSRF validator correctly rejected literal blocked IP addresses in `target_url` for every tested case except one: `http://[::1]/hook` — a bracket-wrapped IPv6 loopback literal — was incorrectly **accepted** (`202`, not the expected `422`). Tracing the root cause revealed the identical defect existed, unexercised by any test, in the dispatch-time transport's own literal-IP check as well: both of the two independent SSRF defense layers shared the same flawed assumption about IP-literal string syntax.

## Timeline / Detection

The first full test run after implementing SSRF defense-in-depth (Phase 2, Target B) returned `1 failed, 248 passed`:

```
test_rejects_literal_blocked_target_urls[http://[::1]/hook] FAILED
assert response.status_code == 422
E   assert 202 == 422
```

## Root Cause

`ipaddress.ip_address("[::1]")` raises `ValueError` — Python's standard library does not accept the RFC 3986 bracket-wrapped form a URL's host component uses for IPv6 literals; only the bare `"::1"` form parses successfully. The blocklist-check function caught that `ValueError` and treated it as "not a literal IP, must be a DNS hostname" — which is the *correct* behavior for an actual hostname, but wrong here, since `[::1]` unambiguously *is* a literal IP address, merely expressed in the syntactic form a URL requires for it.

Because both the ingestion-time validator and the dispatch-time transport had independently written their own `ipaddress.ip_address(host)` / `except ValueError` check, both carried the same incorrect assumption. The dispatch-time copy had not yet been exercised by any test at the time this was found — meaning, prior to this fix, a bracket-wrapped IPv6 loopback/private/link-local `target_url` could have bypassed **both** layers of what was designed as defense-in-depth, simultaneously.

## Resolution

A single shared helper, `parse_ip_literal()`, was introduced in `dart.security.ssrf`: it strips a wrapping `[...]` if present, then parses. Both `is_blocked_literal_host` (ingestion-time) and `SSRFSafeTransport.handle_async_request` (dispatch-time) were changed to call this one function rather than each maintaining independent bracket-handling logic. This was a deliberate root-cause fix, not a per-call-site patch: the goal was specifically to make this class of bug unable to reappear in one location after being fixed in the other.

Regression coverage was added at both layers: parametrized cases in `tests/unit/test_ssrf.py` (`TestBracketedIPv6Literals`, `TestParseIPLiteral`) covering blocked and public bracketed literals, agreement between bracketed and unbracketed forms of the same address, and malformed-bracket input; and a dedicated test in `tests/unit/test_ssrf_transport.py` confirming `SSRFSafeTransport` itself now correctly rejects `http://[::1]/webhook` rather than treating it as an unresolvable hostname.

## Impact

A real security-control gap: an attacker could have used a bracket-wrapped IPv6 literal to bypass ingestion-time SSRF validation and reach a loopback, RFC 1918, or link-local address — including, notably, the IPv6-accessible cloud metadata endpoint pattern. In practice, the fact that the *same* bug independently existed in the dispatch-time layer at the same time is the more serious finding: the entire premise of defense-in-depth is that a gap in one layer is caught by the other, and here that premise did not hold, because both layers derived from the same flawed assumption rather than from genuinely independent implementations.

## Lessons Learned

- Implementing "the same conceptual check" twice, in two different layers, for defense-in-depth only provides real redundancy if the two implementations are actually independent. Two copies of the same flawed assumption produce the *appearance* of redundancy without the substance of it — this is precisely what happened here, and the fix (a single shared primitive, used by both layers) is what makes the two layers independently meaningful going forward: a future bug in the shared primitive would need to be fixed exactly once, and a future bug in either *layer's own logic* (as opposed to the shared primitive) would still be caught by the other layer, which is the property defense-in-depth is supposed to provide.
- A one-line boundary case (a single bracket character) was sufficient to defeat a validation layer that passed seven out of eight equivalent-looking test cases. Parametrized boundary testing — including syntactic variants of a value that is semantically identical (`::1` vs. `[::1]`) — is what caught this; testing only one canonical form of each address family would not have.
