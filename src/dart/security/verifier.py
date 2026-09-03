"""Anti-replay HMAC-SHA256 signature verification for inbound DART
webhook signatures.

Pairs with `dart.security.signer.WebhookSigner`. Verification enforces
two independent checks before a signature is accepted:

1. The recomputed HMAC-SHA256 digest matches the provided one, compared
   in constant time to avoid timing side-channel attacks.
2. The signed timestamp falls within a configurable drift tolerance of
   "now" (default: 5 minutes), which bounds the window in which a
   captured request could be successfully replayed.
"""

from __future__ import annotations

import hashlib
import hmac
import re
import time

from dart.core.exceptions import SignatureFormatError

_HEADER_PATTERN = re.compile(r"^t=(?P<timestamp>\d+),v1=(?P<digest>[0-9a-fA-F]{64})$")


class SignatureVerifier:
    """Verifies `X-DART-Signature` headers with a configurable
    replay-tolerance window."""

    def __init__(self, secret: str, tolerance_seconds: int = 300) -> None:
        if not secret:
            raise ValueError("SignatureVerifier requires a non-empty secret")
        if tolerance_seconds < 0:
            raise ValueError("tolerance_seconds must be non-negative")
        self._secret = secret.encode("utf-8")
        self._tolerance_seconds = tolerance_seconds

    @staticmethod
    def parse_header(signature_header: str) -> tuple[int, str]:
        """Parse a `"t=<ts>,v1=<hex>"` header into `(timestamp, digest)`.

        The returned digest is lowercased. This is exposed as a public,
        raising method (unlike `verify`, which never raises) so callers
        that need the raw timestamp — for logging why a verification
        failed, for instance — don't have to re-implement parsing.

        Raises:
            SignatureFormatError: if the header doesn't match the
                expected `t=<digits>,v1=<64 hex chars>` shape.
        """
        match = _HEADER_PATTERN.match(signature_header.strip())
        if not match:
            raise SignatureFormatError(
                f"Malformed X-DART-Signature header: {signature_header!r}"
            )
        return int(match.group("timestamp")), match.group("digest").lower()

    def _expected_digest(self, timestamp: int, payload: bytes) -> str:
        signed_string = f"{timestamp}.".encode("utf-8") + payload
        return hmac.new(self._secret, signed_string, hashlib.sha256).hexdigest()

    def _within_tolerance(self, timestamp: int, received_at: int) -> bool:
        return abs(received_at - timestamp) <= self._tolerance_seconds

    def verify(
        self,
        payload: bytes,
        signature_header: str,
        received_at: int | None = None,
    ) -> bool:
        """Verify a payload against a signature header.

        Returns `True` only if the header is well-formed, the digest
        matches (constant-time comparison via `hmac.compare_digest`),
        AND the signed timestamp is within `tolerance_seconds` of
        `received_at` in *either* direction — guarding against both a
        stale replayed request and a payload signed with a clock skewed
        into the future.

        This method never raises for a "bad" signature: a malformed
        header, a wrong secret, an expired timestamp, or future clock
        skew all simply produce `False`. That keeps verification
        call sites simple (`if not verifier.verify(...): reject()`)
        without needing a try/except for routine rejection paths.

        Args:
            payload: The exact raw request body bytes that were signed.
            signature_header: The received `X-DART-Signature` header value.
            received_at: Unix timestamp (seconds) representing "now" for
                tolerance purposes. Defaults to the current time.
        """
        try:
            timestamp, provided_digest = self.parse_header(signature_header)
        except SignatureFormatError:
            return False

        now = received_at if received_at is not None else int(time.time())

        if not self._within_tolerance(timestamp, now):
            return False

        expected_digest = self._expected_digest(timestamp, payload)
        return hmac.compare_digest(expected_digest, provided_digest)
