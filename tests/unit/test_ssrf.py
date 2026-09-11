"""Unit tests for `dart.security.ssrf`.

Every case here was executed directly against the real implementation
while building it (see the module's inline comments) — including one
real bug this caught: `is_global` does not exclude multicast addresses
in Python's `ipaddress` module (it's an orthogonal address-class
concept, not a subset of "not global"), so `224.0.0.1` initially sailed
through unblocked until `is_multicast` was added as an explicit check.
"""

from __future__ import annotations

import ipaddress

import pytest

from dart.security.ssrf import is_blocked_address, is_blocked_literal_host


class TestNamedBlockedRanges:
    @pytest.mark.parametrize(
        "literal",
        [
            "127.0.0.1",
            "127.255.255.254",
            "10.0.0.1",
            "10.255.255.255",
            "172.16.0.1",
            "172.31.255.255",
            "192.168.0.1",
            "192.168.255.255",
            "169.254.169.254",  # cloud metadata endpoint (AWS/GCP) -- the case that matters most
            "169.254.0.1",
        ],
    )
    def test_ipv4_named_ranges_are_blocked(self, literal: str) -> None:
        assert is_blocked_literal_host(literal) is True

    @pytest.mark.parametrize("literal", ["::1", "fe80::1", "fe80::abcd"])
    def test_ipv6_named_ranges_are_blocked(self, literal: str) -> None:
        assert is_blocked_literal_host(literal) is True


class TestRangeBoundaries:
    """One address just inside and just outside each named /N boundary."""

    @pytest.mark.parametrize(
        "literal,expected",
        [
            ("172.15.255.255", False),  # just below 172.16.0.0/12
            ("172.16.0.0", True),  # first address in range
            ("172.31.255.255", True),  # last address in range
            ("172.32.0.0", False),  # just above 172.16.0.0/12
            ("192.167.255.255", False),  # just below 192.168.0.0/16
            ("192.169.0.0", False),  # just above 192.168.0.0/16
            ("9.255.255.255", False),  # just below 10.0.0.0/8
            ("11.0.0.0", False),  # just above 10.0.0.0/8
        ],
    )
    def test_boundary_addresses(self, literal: str, expected: bool) -> None:
        assert is_blocked_literal_host(literal) is expected


class TestPublicAddressesAreAllowed:
    @pytest.mark.parametrize(
        "literal",
        ["8.8.8.8", "1.1.1.1", "93.184.216.34", "2606:4700:4700::1111"],
    )
    def test_known_public_addresses_are_not_blocked(self, literal: str) -> None:
        assert is_blocked_literal_host(literal) is False


class TestIsGlobalBackstopCoversMoreThanTheNamedList:
    """`is_global` catches categories beyond the explicitly-named
    ranges: RFC 6598 CGNAT, "this network", reserved space, TEST-NET
    documentation ranges, and broadcast."""

    @pytest.mark.parametrize(
        "literal",
        [
            "100.64.0.1",  # RFC 6598 shared address space (carrier-grade NAT)
            "0.0.0.0",  # "this network"
            "240.0.0.1",  # reserved (240.0.0.0/4)
            "255.255.255.255",  # limited broadcast
            "192.0.2.1",  # TEST-NET-1 (documentation)
            "198.51.100.1",  # TEST-NET-2 (documentation)
            "203.0.113.1",  # TEST-NET-3 (documentation)
            "fc00::1",  # IPv6 unique-local
        ],
    )
    def test_non_global_addresses_are_blocked(self, literal: str) -> None:
        assert is_blocked_literal_host(literal) is True


class TestMulticastIsBlockedExplicitly:
    """Regression test for the real bug found via execution: `is_global`
    does NOT exclude multicast on its own."""

    @pytest.mark.parametrize("literal", ["224.0.0.1", "239.255.255.255", "ff02::1"])
    def test_multicast_addresses_are_blocked(self, literal: str) -> None:
        assert is_blocked_literal_host(literal) is True

    def test_is_global_alone_would_have_missed_this(self) -> None:
        """Documents WHY the explicit is_multicast check exists, not
        just that it works."""
        ip = ipaddress.ip_address("224.0.0.1")
        assert ip.is_global is True, (
            "if this ever becomes False in a future Python version, the "
            "explicit is_multicast check in is_blocked_address becomes "
            "redundant but still harmless -- this test documents the "
            "current stdlib behavior that made the check necessary"
        )
        assert ip.is_multicast is True


class TestIPv4MappedIPv6Unwrapping:
    def test_mapped_blocked_ipv4_is_caught(self) -> None:
        mapped_loopback = ipaddress.ip_address("::ffff:127.0.0.1")
        assert is_blocked_address(mapped_loopback) is True

    def test_mapped_public_ipv4_is_allowed(self) -> None:
        mapped_public = ipaddress.ip_address("::ffff:8.8.8.8")
        assert is_blocked_address(mapped_public) is False

    def test_mapped_private_ipv4_is_caught(self) -> None:
        mapped_private = ipaddress.ip_address("::ffff:10.0.0.1")
        assert is_blocked_address(mapped_private) is True


class TestNonLiteralHostsAreOutOfScope:
    """`is_blocked_literal_host` only judges literal IP strings; DNS
    hostname resolution is deliberately a dispatch-time concern (see
    the module docstring)."""

    @pytest.mark.parametrize(
        "hostname",
        [
            "example.com",
            "internal-service.corp.example.com",
            "localhost",  # a NAME, not the 127.0.0.1 literal -- out of scope here by design
            "not a valid host at all!!",
        ],
    )
    def test_hostnames_return_false_not_an_error(self, hostname: str) -> None:
        assert is_blocked_literal_host(hostname) is False
