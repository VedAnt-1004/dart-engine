"""Unit tests for `dart.worker.ssrf_transport.SSRFSafeTransport`.

Uses a stub inner transport that just captures whatever request it
receives and returns a canned response, so assertions can inspect
exactly what `SSRFSafeTransport` did to the request (host rewritten,
`Host` header preserved, `sni_hostname` extension set) before handing
it off — without needing a real network call.

DNS resolution is monkeypatched at the level `SSRFSafeTransport` itself
calls it (`asyncio.get_running_loop().getaddrinfo`), so these tests
don't depend on real DNS either.
"""

from __future__ import annotations

import asyncio
import socket
from typing import Any

import httpx
import pytest

from dart.worker.ssrf_transport import SSRFBlockedError, SSRFSafeTransport


class _CapturingTransport(httpx.AsyncBaseTransport):
    """Records the request it receives and returns a fixed 200."""

    def __init__(self) -> None:
        self.received_request: httpx.Request | None = None
        self.call_count = 0

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.received_request = request
        self.call_count += 1
        return httpx.Response(200, request=request)


def _addrinfo_entry(ip: str, port: int) -> tuple:
    family = socket.AF_INET6 if ":" in ip else socket.AF_INET
    sockaddr = (ip, port, 0, 0) if family == socket.AF_INET6 else (ip, port)
    return (family, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", sockaddr)


def _patch_getaddrinfo(monkeypatch: pytest.MonkeyPatch, results: list[tuple]) -> None:
    """Monkeypatches the CURRENT running loop's `getaddrinfo` --
    `SSRFSafeTransport` calls `asyncio.get_running_loop().getaddrinfo(...)`,
    and within a single async test, that's the same loop instance the
    test itself runs on."""

    async def fake_getaddrinfo(host: str, port: int, **kwargs: Any) -> list[tuple]:
        return results

    loop = asyncio.get_running_loop()
    monkeypatch.setattr(loop, "getaddrinfo", fake_getaddrinfo)


def _patch_getaddrinfo_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    async def failing_getaddrinfo(host: str, port: int, **kwargs: Any) -> list[tuple]:
        raise socket.gaierror("simulated resolution failure")

    loop = asyncio.get_running_loop()
    monkeypatch.setattr(loop, "getaddrinfo", failing_getaddrinfo)


class TestHostnameResolvesToPublicIP:
    async def test_connection_is_pinned_to_the_resolved_ip(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _patch_getaddrinfo(monkeypatch, [_addrinfo_entry("93.184.216.34", 443)])
        inner = _CapturingTransport()
        transport = SSRFSafeTransport(inner)

        request = httpx.Request("POST", "https://example.com/webhook")
        await transport.handle_async_request(request)

        assert inner.call_count == 1
        assert inner.received_request is not None
        assert inner.received_request.url.host == "93.184.216.34"

    async def test_host_header_preserves_original_hostname(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _patch_getaddrinfo(monkeypatch, [_addrinfo_entry("93.184.216.34", 443)])
        inner = _CapturingTransport()
        transport = SSRFSafeTransport(inner)

        request = httpx.Request("POST", "https://example.com/webhook")
        await transport.handle_async_request(request)

        assert inner.received_request.headers["host"] == "example.com"

    async def test_sni_hostname_extension_preserves_original_hostname(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _patch_getaddrinfo(monkeypatch, [_addrinfo_entry("93.184.216.34", 443)])
        inner = _CapturingTransport()
        transport = SSRFSafeTransport(inner)

        request = httpx.Request("POST", "https://example.com/webhook")
        await transport.handle_async_request(request)

        assert inner.received_request.extensions["sni_hostname"] == "example.com"

    async def test_response_from_inner_transport_is_returned_unchanged(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _patch_getaddrinfo(monkeypatch, [_addrinfo_entry("93.184.216.34", 443)])
        transport = SSRFSafeTransport(_CapturingTransport())

        request = httpx.Request("POST", "https://example.com/webhook")
        response = await transport.handle_async_request(request)

        assert response.status_code == 200


class TestHostnameResolvesOnlyToPrivateIPs:
    async def test_raises_ssrf_blocked_without_calling_inner_transport(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _patch_getaddrinfo(monkeypatch, [_addrinfo_entry("10.0.0.5", 443)])
        inner = _CapturingTransport()
        transport = SSRFSafeTransport(inner)

        request = httpx.Request("POST", "https://internal.example.com/webhook")
        with pytest.raises(SSRFBlockedError):
            await transport.handle_async_request(request)

        assert inner.call_count == 0, "must not forward a blocked request"


class TestMultipleResolvedAddresses:
    async def test_picks_the_first_public_address_when_mixed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """DNS-rebinding-adjacent case: a name resolving to BOTH a
        private and a public address (e.g. round-robin/multi-A-record)
        -- the safe choice is to use a public one, not to fail closed
        just because one candidate happened to be private."""
        _patch_getaddrinfo(
            monkeypatch,
            [_addrinfo_entry("10.0.0.5", 443), _addrinfo_entry("93.184.216.34", 443)],
        )
        inner = _CapturingTransport()
        transport = SSRFSafeTransport(inner)

        request = httpx.Request("POST", "https://example.com/webhook")
        await transport.handle_async_request(request)

        assert inner.received_request.url.host == "93.184.216.34"


class TestDNSResolutionFailure:
    async def test_resolution_failure_becomes_ssrf_blocked_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _patch_getaddrinfo_failure(monkeypatch)
        inner = _CapturingTransport()
        transport = SSRFSafeTransport(inner)

        request = httpx.Request("POST", "https://nonexistent.invalid/webhook")
        with pytest.raises(SSRFBlockedError):
            await transport.handle_async_request(request)

        assert inner.call_count == 0


class TestLiteralIPTargets:
    """A request whose host is already a literal IP skips DNS entirely."""

    async def test_public_literal_ip_is_forwarded_without_dns_call(self) -> None:
        # Deliberately does NOT call _patch_getaddrinfo -- if the code
        # under test tried to resolve DNS here, it would hit the real
        # (unpatched) resolver and this test would hang/fail, which is
        # exactly what proves the "skip DNS for literal IPs" path.
        inner = _CapturingTransport()
        transport = SSRFSafeTransport(inner)

        request = httpx.Request("POST", "http://93.184.216.34/webhook")
        await transport.handle_async_request(request)

        assert inner.call_count == 1
        assert inner.received_request.url.host == "93.184.216.34"

    async def test_blocked_literal_ip_raises_without_dns_call(self) -> None:
        inner = _CapturingTransport()
        transport = SSRFSafeTransport(inner)

        request = httpx.Request("POST", "http://127.0.0.1/webhook")
        with pytest.raises(SSRFBlockedError):
            await transport.handle_async_request(request)

        assert inner.call_count == 0
