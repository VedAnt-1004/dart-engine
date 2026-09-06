"""Exponential backoff with full jitter, and retry/DLQ classification.

Implements `sleep = uniform(0, min(max_backoff, base * 2**attempt))` per
the approved architecture. Per approved decision #6, an upstream
`Retry-After` header always overrides the calculated backoff — that
precedence lives in `resolve_backoff`, kept separate from
`compute_backoff` so the pure exponential-jitter formula stays
independently testable.
"""

from __future__ import annotations

import random


class RetryPolicy:
    """Governs both *whether* a failed dispatch should be retried and
    *how long* to wait before the next attempt."""

    def __init__(
        self,
        base_seconds: float = 1.0,
        max_backoff_seconds: float = 300.0,
        max_attempts: int = 8,
    ) -> None:
        if base_seconds <= 0:
            raise ValueError("base_seconds must be positive")
        if max_backoff_seconds < base_seconds:
            raise ValueError("max_backoff_seconds must be >= base_seconds")
        if max_attempts < 1:
            raise ValueError("max_attempts must be >= 1")

        self._base_seconds = base_seconds
        self._max_backoff_seconds = max_backoff_seconds
        self._max_attempts = max_attempts

    @property
    def max_attempts(self) -> int:
        return self._max_attempts

    def compute_backoff(self, attempt: int) -> float:
        """Full-jitter exponential backoff for a given attempt number.

        Args:
            attempt: The (zero-indexed) number of attempts already made.
                `attempt=0` yields a delay in `[0, base_seconds]`,
                `attempt=1` in `[0, min(max_backoff, base_seconds*2)]`,
                and so on, capped at `max_backoff_seconds`.

        Returns:
            A random delay in seconds, uniformly sampled from
            `[0, min(max_backoff_seconds, base_seconds * 2**attempt)]`.
        """
        if attempt < 0:
            raise ValueError("attempt must be non-negative")

        exponential = self._base_seconds * (2**attempt)
        cap = min(self._max_backoff_seconds, exponential)
        return random.uniform(0, cap)

    def resolve_backoff(
        self, attempt: int, retry_after_seconds: float | None = None
    ) -> float:
        """Resolve the actual delay to sleep before the next attempt.

        Per approved architecture decision #6, an explicit
        `retry_after_seconds` (parsed from an upstream `Retry-After`
        header) always overrides the calculated exponential-jitter
        backoff, taking precedence regardless of `attempt`.
        """
        if retry_after_seconds is not None:
            if retry_after_seconds < 0:
                raise ValueError("retry_after_seconds must be non-negative")
            return retry_after_seconds
        return self.compute_backoff(attempt)

    def should_retry(
        self,
        attempt: int,
        status_code: int | None,
        exception: Exception | None = None,
    ) -> bool:
        """Decide whether a failed dispatch should be retried.

        Args:
            attempt: The number of attempts already made (including the
                one that just failed).
            status_code: The HTTP status code received, or `None` if the
                attempt failed before a response was received (timeout,
                connection error, etc.).
            exception: The exception raised during the attempt, if any.

        Returns:
            `False` once `attempt` reaches `max_attempts` (route to
            DLQ), regardless of the failure reason. Otherwise: `True` for
            network-level exceptions and for HTTP 429 or any 5xx status;
            `False` for any other 4xx status (client error — retrying
            won't help) and for a missing status with no exception
            (a malformed/unclassifiable failure).
        """
        if attempt >= self._max_attempts:
            return False

        if exception is not None:
            return True

        if status_code is None:
            return False

        if status_code == 429:
            return True

        if 500 <= status_code < 600:
            return True

        return False
