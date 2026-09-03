"""Unit tests for `dart.security.verifier.SignatureVerifier`."""

from __future__ import annotations

import hmac
from unittest.mock import patch

import pytest

from dart.core.exceptions import SignatureFormatError
from dart.security.signer import WebhookSigner
from dart.security.verifier import SignatureVerifier

SECRET = "whsec_test_secret_1234567890"
PAYLOAD = b'{"event_type": "invoice.paid", "id": "evt_123"}'
TOLERANCE = 300  # 5-minute default drift tolerance, per spec


def _make_header(secret: str, timestamp: int, payload: bytes = PAYLOAD) -> str:
    return WebhookSigner(secret).sign(payload, timestamp=timestamp)


class TestInit:
    def test_rejects_empty_secret(self) -> None:
        with pytest.raises(ValueError):
            SignatureVerifier("")

    def test_rejects_negative_tolerance(self) -> None:
        with pytest.raises(ValueError):
            SignatureVerifier(SECRET, tolerance_seconds=-1)

    def test_accepts_zero_tolerance(self) -> None:
        verifier = SignatureVerifier(SECRET, tolerance_seconds=0)
        assert verifier is not None


class TestParseHeader:
    def test_parses_well_formed_header(self) -> None:
        header = _make_header(SECRET, 1735689600)
        ts, digest = SignatureVerifier.parse_header(header)
        assert ts == 1735689600
        assert len(digest) == 64

    def test_rejects_missing_v1_component(self) -> None:
        with pytest.raises(SignatureFormatError):
            SignatureVerifier.parse_header("t=1735689600")

    def test_rejects_non_numeric_timestamp(self) -> None:
        with pytest.raises(SignatureFormatError):
            SignatureVerifier.parse_header("t=notanumber,v1=" + "a" * 64)

    def test_rejects_short_digest(self) -> None:
        with pytest.raises(SignatureFormatError):
            SignatureVerifier.parse_header("t=1735689600,v1=abcd")

    def test_rejects_completely_malformed_string(self) -> None:
        with pytest.raises(SignatureFormatError):
            SignatureVerifier.parse_header("not-a-signature-header-at-all")

    def test_rejects_empty_string(self) -> None:
        with pytest.raises(SignatureFormatError):
            SignatureVerifier.parse_header("")

    def test_digest_is_lowercased(self) -> None:
        header = f"t=1735689600,v1={'A' * 64}"
        _, digest = SignatureVerifier.parse_header(header)
        assert digest == "a" * 64


class TestVerifyValidSignatures:
    def test_valid_signature_at_current_time_passes(self) -> None:
        verifier = SignatureVerifier(SECRET, tolerance_seconds=TOLERANCE)
        now = 1735689600
        header = _make_header(SECRET, now)
        assert verifier.verify(PAYLOAD, header, received_at=now) is True

    def test_valid_signature_defaults_received_at_to_now(self) -> None:
        verifier = SignatureVerifier(SECRET, tolerance_seconds=TOLERANCE)
        with patch("dart.security.verifier.time.time", return_value=1735689600.0):
            header = _make_header(SECRET, 1735689600)
            assert verifier.verify(PAYLOAD, header) is True


class TestVerifyInvalidSignatures:
    def test_wrong_secret_fails(self) -> None:
        verifier = SignatureVerifier(SECRET, tolerance_seconds=TOLERANCE)
        now = 1735689600
        header = _make_header("a-completely-different-secret", now)
        assert verifier.verify(PAYLOAD, header, received_at=now) is False

    def test_tampered_payload_fails(self) -> None:
        verifier = SignatureVerifier(SECRET, tolerance_seconds=TOLERANCE)
        now = 1735689600
        header = _make_header(SECRET, now)
        tampered_payload = PAYLOAD + b"tampered"
        assert verifier.verify(tampered_payload, header, received_at=now) is False

    def test_tampered_digest_fails(self) -> None:
        verifier = SignatureVerifier(SECRET, tolerance_seconds=TOLERANCE)
        now = 1735689600
        header = _make_header(SECRET, now)
        ts_part, _, digest_part = header.partition(",v1=")
        flipped_char = "0" if digest_part[0] != "0" else "1"
        tampered_header = f"{ts_part},v1={flipped_char}{digest_part[1:]}"
        assert verifier.verify(PAYLOAD, tampered_header, received_at=now) is False

    def test_malformed_header_returns_false_not_raise(self) -> None:
        verifier = SignatureVerifier(SECRET, tolerance_seconds=TOLERANCE)
        assert verifier.verify(PAYLOAD, "garbage-header", received_at=1735689600) is False

    def test_empty_header_returns_false_not_raise(self) -> None:
        verifier = SignatureVerifier(SECRET, tolerance_seconds=TOLERANCE)
        assert verifier.verify(PAYLOAD, "", received_at=1735689600) is False


class TestToleranceBoundaries:
    """`signed_at = 1735689600`; `TOLERANCE = 300` seconds in either direction."""

    def test_exactly_at_tolerance_boundary_past_is_valid(self) -> None:
        verifier = SignatureVerifier(SECRET, tolerance_seconds=TOLERANCE)
        signed_at = 1735689600
        header = _make_header(SECRET, signed_at)
        received_at = signed_at + TOLERANCE  # t - drift == tolerance
        assert verifier.verify(PAYLOAD, header, received_at=received_at) is True

    def test_one_second_past_tolerance_boundary_is_expired(self) -> None:
        verifier = SignatureVerifier(SECRET, tolerance_seconds=TOLERANCE)
        signed_at = 1735689600
        header = _make_header(SECRET, signed_at)
        received_at = signed_at + TOLERANCE + 1
        assert verifier.verify(PAYLOAD, header, received_at=received_at) is False

    def test_exactly_at_future_skew_boundary_is_valid(self) -> None:
        verifier = SignatureVerifier(SECRET, tolerance_seconds=TOLERANCE)
        signed_at = 1735689600
        header = _make_header(SECRET, signed_at)
        # header's timestamp is *ahead* of received_at by exactly tolerance
        received_at = signed_at - TOLERANCE
        assert verifier.verify(PAYLOAD, header, received_at=received_at) is True

    def test_one_second_beyond_future_skew_boundary_fails(self) -> None:
        verifier = SignatureVerifier(SECRET, tolerance_seconds=TOLERANCE)
        signed_at = 1735689600
        header = _make_header(SECRET, signed_at)
        received_at = signed_at - TOLERANCE - 1
        assert verifier.verify(PAYLOAD, header, received_at=received_at) is False

    def test_zero_tolerance_requires_exact_match(self) -> None:
        verifier = SignatureVerifier(SECRET, tolerance_seconds=0)
        signed_at = 1735689600
        header = _make_header(SECRET, signed_at)
        assert verifier.verify(PAYLOAD, header, received_at=signed_at) is True
        assert verifier.verify(PAYLOAD, header, received_at=signed_at + 1) is False
        assert verifier.verify(PAYLOAD, header, received_at=signed_at - 1) is False


class TestConstantTimeComparison:
    def test_verify_uses_hmac_compare_digest(self) -> None:
        """`verify` must compare digests via `hmac.compare_digest` rather
        than plain `==`, which is the standard mitigation against timing
        side-channel attacks on signature comparison."""
        verifier = SignatureVerifier(SECRET, tolerance_seconds=TOLERANCE)
        now = 1735689600
        header = _make_header(SECRET, now)

        with patch(
            "dart.security.verifier.hmac.compare_digest", wraps=hmac.compare_digest
        ) as spy:
            result = verifier.verify(PAYLOAD, header, received_at=now)

        spy.assert_called_once()
        assert result is True

    def test_compare_digest_not_called_when_header_malformed(self) -> None:
        """No point comparing digests for a structurally invalid header —
        confirms the short-circuit happens before the HMAC comparison."""
        verifier = SignatureVerifier(SECRET, tolerance_seconds=TOLERANCE)

        with patch(
            "dart.security.verifier.hmac.compare_digest", wraps=hmac.compare_digest
        ) as spy:
            verifier.verify(PAYLOAD, "garbage-header", received_at=1735689600)

        spy.assert_not_called()
