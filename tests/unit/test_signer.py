"""Unit tests for `dart.security.signer.WebhookSigner`."""

from __future__ import annotations

import hashlib
import hmac
import re
import time

import pytest

from dart.security.signer import WebhookSigner

SECRET = "whsec_test_secret_1234567890"
PAYLOAD = b'{"event_type": "invoice.paid", "id": "evt_123"}'
HEADER_PATTERN = re.compile(r"^t=(\d+),v1=([0-9a-f]{64})$")


class TestWebhookSignerInit:
    def test_rejects_empty_secret(self) -> None:
        with pytest.raises(ValueError):
            WebhookSigner("")

    def test_accepts_valid_secret(self) -> None:
        signer = WebhookSigner(SECRET)
        assert signer is not None


class TestBuildSignedString:
    def test_canonical_string_format(self) -> None:
        result = WebhookSigner.build_signed_string(1735689600, PAYLOAD)
        assert result == b"1735689600." + PAYLOAD

    def test_canonical_string_is_bytes(self) -> None:
        result = WebhookSigner.build_signed_string(1735689600, PAYLOAD)
        assert isinstance(result, bytes)

    def test_different_timestamps_produce_different_strings(self) -> None:
        s1 = WebhookSigner.build_signed_string(100, PAYLOAD)
        s2 = WebhookSigner.build_signed_string(200, PAYLOAD)
        assert s1 != s2

    def test_empty_payload(self) -> None:
        result = WebhookSigner.build_signed_string(100, b"")
        assert result == b"100."


class TestComputeDigest:
    def test_digest_matches_reference_hmac(self) -> None:
        signer = WebhookSigner(SECRET)
        ts = 1735689600
        expected = hmac.new(
            SECRET.encode("utf-8"),
            f"{ts}.".encode("utf-8") + PAYLOAD,
            hashlib.sha256,
        ).hexdigest()
        assert signer.compute_digest(ts, PAYLOAD) == expected

    def test_digest_is_hex_sha256_length(self) -> None:
        signer = WebhookSigner(SECRET)
        digest = signer.compute_digest(1735689600, PAYLOAD)
        assert len(digest) == 64
        assert re.fullmatch(r"[0-9a-f]{64}", digest)

    def test_digest_changes_with_different_secret(self) -> None:
        signer_a = WebhookSigner(SECRET)
        signer_b = WebhookSigner("a-completely-different-secret")
        ts = 1735689600
        assert signer_a.compute_digest(ts, PAYLOAD) != signer_b.compute_digest(ts, PAYLOAD)

    def test_digest_changes_with_different_payload(self) -> None:
        signer = WebhookSigner(SECRET)
        ts = 1735689600
        d1 = signer.compute_digest(ts, PAYLOAD)
        d2 = signer.compute_digest(ts, b'{"event_type": "invoice.voided"}')
        assert d1 != d2

    def test_digest_changes_with_different_timestamp(self) -> None:
        signer = WebhookSigner(SECRET)
        d1 = signer.compute_digest(1735689600, PAYLOAD)
        d2 = signer.compute_digest(1735689601, PAYLOAD)
        assert d1 != d2

    def test_digest_is_deterministic(self) -> None:
        signer = WebhookSigner(SECRET)
        ts = 1735689600
        assert signer.compute_digest(ts, PAYLOAD) == signer.compute_digest(ts, PAYLOAD)


class TestSign:
    def test_header_format(self) -> None:
        signer = WebhookSigner(SECRET)
        header = signer.sign(PAYLOAD, timestamp=1735689600)
        assert HEADER_PATTERN.match(header)

    def test_header_contains_expected_timestamp(self) -> None:
        signer = WebhookSigner(SECRET)
        header = signer.sign(PAYLOAD, timestamp=1735689600)
        assert header.startswith("t=1735689600,")

    def test_header_digest_matches_compute_digest(self) -> None:
        signer = WebhookSigner(SECRET)
        ts = 1735689600
        header = signer.sign(PAYLOAD, timestamp=ts)
        expected_digest = signer.compute_digest(ts, PAYLOAD)
        assert header == f"t={ts},v1={expected_digest}"

    def test_defaults_to_current_time_when_timestamp_omitted(self) -> None:
        signer = WebhookSigner(SECRET)
        before = int(time.time())
        header = signer.sign(PAYLOAD)
        after = int(time.time())

        match = HEADER_PATTERN.match(header)
        assert match is not None
        ts_in_header = int(match.group(1))
        assert before <= ts_in_header <= after

    def test_sign_is_self_consistent_with_recomputation(self) -> None:
        signer = WebhookSigner(SECRET)
        ts = 1735689600
        header = signer.sign(PAYLOAD, timestamp=ts)

        match = HEADER_PATTERN.match(header)
        assert match is not None
        recomputed = signer.compute_digest(int(match.group(1)), PAYLOAD)
        assert match.group(2) == recomputed

    def test_scheme_version_is_v1(self) -> None:
        assert WebhookSigner.SCHEME_VERSION == "v1"
