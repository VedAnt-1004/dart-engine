"""HMAC-SHA256 webhook signing with timestamp anchoring.

DART signs every outbound delivery so recipients can verify authenticity
and, combined with the timestamp, reject replayed requests. The header
scheme mirrors the pattern used by Stripe/Svix::

    X-DART-Signature: t=<unix_timestamp>,v1=<hex_hmac_sha256_digest>
"""

from __future__ import annotations

import hashlib
import hmac
import time


class WebhookSigner:
    """Signs outgoing webhook payloads using HMAC-SHA256.

    The signed string is `"<timestamp>." + payload` — binding the
    timestamp into the digest itself (rather than sending it unsigned
    alongside) means an attacker cannot replay an old payload with a
    freshly forged timestamp without also knowing the secret.
    """

    #: The signature scheme version tag used in the header, e.g. "v1".
    SCHEME_VERSION = "v1"

    def __init__(self, secret: str) -> None:
        if not secret:
            raise ValueError("WebhookSigner requires a non-empty secret")
        self._secret = secret.encode("utf-8")

    @staticmethod
    def build_signed_string(timestamp: int, payload: bytes) -> bytes:
        """Canonical string-to-sign: `b"<timestamp>." + payload`."""
        return f"{timestamp}.".encode("utf-8") + payload

    def compute_digest(self, timestamp: int, payload: bytes) -> str:
        """Return the lowercase hex HMAC-SHA256 digest for
        `(timestamp, payload)` under this signer's secret."""
        signed_string = self.build_signed_string(timestamp, payload)
        return hmac.new(self._secret, signed_string, hashlib.sha256).hexdigest()

    def sign(self, payload: bytes, timestamp: int | None = None) -> str:
        """Build the full `X-DART-Signature` header value for a payload.

        Args:
            payload: Raw request body bytes to sign.
            timestamp: Unix timestamp (seconds) to anchor the signature
                to. Defaults to the current time when omitted.

        Returns:
            Header value formatted as `"t=<ts>,v1=<hex_digest>"`.
        """
        ts = timestamp if timestamp is not None else int(time.time())
        digest = self.compute_digest(ts, payload)
        return f"t={ts},{self.SCHEME_VERSION}={digest}"
