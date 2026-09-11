"""Dispatch-time SSRF defense: a custom `httpx.AsyncBaseTransport` that
resolves the target hostname itself, rejects addresses that resolve
only to non-public IPs, and pins the TCP connection to the validated
address — closing the DNS-rebinding TOCTOU gap between "this hostname
looked public when ingestion validated it" and "this hostname now
resolves somewhere private by the time we actually connect."

This is layer 2 of 2 (not a replacement for layer 1, the literal-IP
check in `dart.models.events.EventIngestRequest`) — see
`dart.security.ssrf`'s module docstring for the full rationale. Layer 1
alone is a checkbox; this is where the actual protection lives, because
it validates the IP DART is about to connect to, at the moment it's
about to connect to it, not a hostname string minutes or days earlier.
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket

import httpx

from dart.core.exceptions import DartError
from dart.security.ssrf import is_blocked_address


class SSRFBlockedError(DartError):
    """Raised when a dispatch target resolves only to blocked
    (loopback / RFC 1918 / link-local / multicast / otherwise
    non-public) addresses, or when DNS resolution fails outright."""


class SSRFSafeTransport(httpx.AsyncBaseTransport):
    """Wraps a real `httpx.AsyncBaseTransport`, resolving DNS itself and
    pinning each request's connection to a validated public IP.

    Preserves the original hostname as both the `Host` header (so the
    origin's virtual-hosting/routing still sees the right value) and
    the TLS SNI hostname (via httpcore's `sni_hostname` request
    extension, the documented mechanism for exactly this "connect via
    IP, present a different hostname via SNI" case) — only the actual
    TCP connection target changes, to the resolved, pre-validated IP.

    Requires an explicitly-constructed inner transport (no default) so
    that connection-pool `limits` configured by the caller are honored
    — `httpx.AsyncClient(limits=...)` only applies that config to its
    OWN default transport; once a custom `transport=` is supplied, the
    client's `limits` kwarg is silently ignored, so the inner transport
    must be built with limits baked in and passed in explicitly.
    """

    def __init__(self, inner_transport: httpx.AsyncBaseTransport) -> None:
        self._inner_transport = inner_transport

    async def _resolve_public_ip(self, hostname: str, port: int) -> str:
        """Resolve `hostname`, returning the first PUBLIC address found.

        Raises:
            SSRFBlockedError: if every resolved address is blocked, or
                resolution fails outright.
        """
        loop = asyncio.get_running_loop()
        try:
            addrinfo = await loop.getaddrinfo(hostname, port, proto=socket.IPPROTO_TCP)
        except socket.gaierror as exc:
            raise SSRFBlockedError(
                f"DNS resolution failed for host {hostname!r}: {exc}"
            ) from exc

        candidates: list[str] = []
        for _family, _type, _proto, _canonname, sockaddr in addrinfo:
            raw_ip = sockaddr[0]
            candidates.append(raw_ip)
            if not is_blocked_address(ipaddress.ip_address(raw_ip)):
                return raw_ip

        raise SSRFBlockedError(
            f"host {hostname!r} resolved only to non-public addresses "
            f"{candidates}; refusing to dispatch."
        )

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        original_host = request.url.host
        port = request.url.port or (443 if request.url.scheme == "https" else 80)

        # A literal IP to begin with (e.g. a retried task whose URL was
        # already validated, or ingestion validation somehow bypassed)
        # -- check it directly, no DNS round trip needed.
        try:
            literal_ip = ipaddress.ip_address(original_host)
        except ValueError:
            literal_ip = None

        if literal_ip is not None:
            if is_blocked_address(literal_ip):
                raise SSRFBlockedError(
                    f"target host {original_host!r} is a non-public literal "
                    "address; refusing to dispatch."
                )
            resolved_ip = original_host
        else:
            resolved_ip = await self._resolve_public_ip(original_host, port)

        # Mutate in place rather than reconstruct a new httpx.Request:
        # a request carrying a body has stateful stream handling that a
        # naive reconstruction risks mishandling (e.g. a body that's
        # already been partially consumed).
        request.url = request.url.copy_with(host=resolved_ip)
        request.headers["host"] = original_host
        request.extensions["sni_hostname"] = original_host

        return await self._inner_transport.handle_async_request(request)

    async def aclose(self) -> None:
        await self._inner_transport.aclose()
