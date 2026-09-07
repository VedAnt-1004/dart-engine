"""A configurable mock webhook receiver for exercising DART's dispatch
resilience end-to-end: timeouts, 429 rate limiting with `Retry-After`,
5xx server errors, 4xx client errors, and plain success.

Behavior is selected via query parameters on the target URL itself
(e.g. `?behavior=server_error`), rather than a custom header, so tests
can drive every chaos scenario through DART's real dispatch path —
`WebhookTask.target_url` is the only thing that needs to vary, with no
special-casing required anywhere in `AsyncDispatcher`.
"""

from __future__ import annotations

import asyncio

from fastapi import FastAPI, Response

app = FastAPI(title="DART Mock Chaos Receiver")

#: Requests received, for tests to assert delivery counts/timing.
#: Test-only in-memory state — reset between tests via
#: `received_requests.clear()`.
received_requests: list[dict[str, str]] = []


@app.post("/webhook")
async def receive_webhook(
    response: Response,
    behavior: str = "success",
    retry_after: float = 2.0,
    timeout_seconds: float = 30.0,
) -> dict[str, str]:
    """
    `behavior` values:
        success       -> 200 OK (default)
        server_error  -> 500
        client_error  -> 400 (non-retryable)
        rate_limit    -> 429 with a `Retry-After: {retry_after}` header
        timeout       -> sleeps `timeout_seconds` before responding,
                         intended to exceed the caller's read timeout
    """
    received_requests.append({"behavior": behavior})

    if behavior == "timeout":
        await asyncio.sleep(timeout_seconds)
        return {"status": "should not reach here"}

    if behavior == "rate_limit":
        response.status_code = 429
        response.headers["Retry-After"] = str(retry_after)
        return {"status": "rate_limited"}

    if behavior == "server_error":
        response.status_code = 500
        return {"status": "server_error"}

    if behavior == "client_error":
        response.status_code = 400
        return {"status": "bad_request"}

    response.status_code = 200
    return {"status": "ok"}


@app.get("/_test/received")
async def get_received_requests() -> dict[str, list[dict[str, str]]]:
    """Test-only introspection endpoint."""
    return {"received": received_requests}
