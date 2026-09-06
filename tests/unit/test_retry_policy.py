"""Unit tests for `dart.resilience.retry_policy.RetryPolicy`."""

from __future__ import annotations

import pytest

from dart.resilience.retry_policy import RetryPolicy

SAMPLE_SIZE = 2000


class TestInit:
    def test_rejects_non_positive_base_seconds(self) -> None:
        with pytest.raises(ValueError):
            RetryPolicy(base_seconds=0)
        with pytest.raises(ValueError):
            RetryPolicy(base_seconds=-1)

    def test_rejects_max_backoff_below_base(self) -> None:
        with pytest.raises(ValueError):
            RetryPolicy(base_seconds=10, max_backoff_seconds=5)

    def test_accepts_max_backoff_equal_to_base(self) -> None:
        policy = RetryPolicy(base_seconds=5, max_backoff_seconds=5)
        assert policy is not None

    def test_rejects_max_attempts_below_one(self) -> None:
        with pytest.raises(ValueError):
            RetryPolicy(max_attempts=0)


class TestComputeBackoffBounds:
    """Statistical bound checks over many samples — jitter is random, so
    we assert the invariant (0 <= sample <= cap) holds for every sample,
    plus a weak spread check to catch a degenerate ("always returns 0")
    implementation."""

    def test_attempt_zero_bounded_by_base_seconds(self) -> None:
        policy = RetryPolicy(base_seconds=1.0, max_backoff_seconds=300.0)
        samples = [policy.compute_backoff(0) for _ in range(SAMPLE_SIZE)]
        assert all(0.0 <= s <= 1.0 for s in samples)
        assert max(samples) > 0.5  # extremely unlikely to fail if uniform

    def test_attempt_one_bounded_by_double_base(self) -> None:
        policy = RetryPolicy(base_seconds=1.0, max_backoff_seconds=300.0)
        samples = [policy.compute_backoff(1) for _ in range(SAMPLE_SIZE)]
        assert all(0.0 <= s <= 2.0 for s in samples)
        assert max(samples) > 1.0

    def test_attempt_bounded_by_exponential_growth(self) -> None:
        policy = RetryPolicy(base_seconds=2.0, max_backoff_seconds=300.0)
        for attempt, expected_cap in [(0, 2.0), (1, 4.0), (2, 8.0), (3, 16.0)]:
            samples = [policy.compute_backoff(attempt) for _ in range(200)]
            assert all(0.0 <= s <= expected_cap for s in samples), attempt

    def test_large_attempt_is_capped_at_max_backoff(self) -> None:
        policy = RetryPolicy(base_seconds=1.0, max_backoff_seconds=60.0)
        # 2**20 * 1.0 would be enormous uncapped; must be clamped to 60.
        samples = [policy.compute_backoff(20) for _ in range(SAMPLE_SIZE)]
        assert all(0.0 <= s <= 60.0 for s in samples)
        assert max(samples) > 30.0  # spread should use most of the cap

    def test_rejects_negative_attempt(self) -> None:
        policy = RetryPolicy()
        with pytest.raises(ValueError):
            policy.compute_backoff(-1)

    def test_samples_are_not_all_identical(self) -> None:
        """Guards against a broken implementation that returns a constant
        instead of sampling — e.g. always returning the cap itself."""
        policy = RetryPolicy(base_seconds=5.0, max_backoff_seconds=300.0)
        samples = {policy.compute_backoff(3) for _ in range(50)}
        assert len(samples) > 1


class TestResolveBackoff:
    def test_retry_after_overrides_calculated_backoff(self) -> None:
        policy = RetryPolicy(base_seconds=1.0, max_backoff_seconds=300.0)
        # Even at attempt=0 (cap=1.0), an explicit Retry-After of 120s wins.
        assert policy.resolve_backoff(0, retry_after_seconds=120.0) == 120.0

    def test_retry_after_of_zero_is_honored(self) -> None:
        policy = RetryPolicy()
        assert policy.resolve_backoff(0, retry_after_seconds=0.0) == 0.0

    def test_falls_back_to_compute_backoff_when_no_retry_after(self) -> None:
        policy = RetryPolicy(base_seconds=1.0, max_backoff_seconds=300.0)
        result = policy.resolve_backoff(0, retry_after_seconds=None)
        assert 0.0 <= result <= 1.0

    def test_falls_back_when_retry_after_omitted_entirely(self) -> None:
        policy = RetryPolicy(base_seconds=1.0, max_backoff_seconds=300.0)
        result = policy.resolve_backoff(0)
        assert 0.0 <= result <= 1.0

    def test_rejects_negative_retry_after(self) -> None:
        policy = RetryPolicy()
        with pytest.raises(ValueError):
            policy.resolve_backoff(0, retry_after_seconds=-5.0)


class TestShouldRetry:
    def test_5xx_is_retryable(self) -> None:
        policy = RetryPolicy(max_attempts=8)
        for status in (500, 502, 503, 504, 599):
            assert policy.should_retry(1, status_code=status) is True

    def test_429_is_retryable(self) -> None:
        policy = RetryPolicy(max_attempts=8)
        assert policy.should_retry(1, status_code=429) is True

    def test_other_4xx_is_not_retryable(self) -> None:
        policy = RetryPolicy(max_attempts=8)
        for status in (400, 401, 403, 404, 409, 422):
            assert policy.should_retry(1, status_code=status) is False

    def test_2xx_and_3xx_are_not_retryable(self) -> None:
        """Defensive: should_retry is meant to be called only on failure,
        but a 2xx/3xx passed in should never trigger a retry."""
        policy = RetryPolicy(max_attempts=8)
        for status in (200, 201, 301, 304):
            assert policy.should_retry(1, status_code=status) is False

    def test_exception_is_retryable_regardless_of_missing_status(self) -> None:
        policy = RetryPolicy(max_attempts=8)
        assert policy.should_retry(1, status_code=None, exception=TimeoutError()) is True

    def test_exception_takes_precedence_even_with_a_4xx_status(self) -> None:
        """An exception means no reliable status was actually received;
        transport-level failure classification wins."""
        policy = RetryPolicy(max_attempts=8)
        assert policy.should_retry(1, status_code=400, exception=OSError()) is True

    def test_no_status_and_no_exception_is_not_retryable(self) -> None:
        policy = RetryPolicy(max_attempts=8)
        assert policy.should_retry(1, status_code=None, exception=None) is False

    def test_exhausted_attempts_stops_retrying_regardless_of_reason(self) -> None:
        policy = RetryPolicy(max_attempts=3)
        assert policy.should_retry(3, status_code=503) is False
        assert policy.should_retry(3, status_code=429) is False
        assert policy.should_retry(3, status_code=None, exception=TimeoutError()) is False

    def test_attempt_below_max_attempts_still_retries(self) -> None:
        policy = RetryPolicy(max_attempts=3)
        assert policy.should_retry(2, status_code=503) is True

    def test_attempt_beyond_max_attempts_stops_retrying(self) -> None:
        policy = RetryPolicy(max_attempts=3)
        assert policy.should_retry(5, status_code=503) is False
