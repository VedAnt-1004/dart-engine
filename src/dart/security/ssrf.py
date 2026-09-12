"""SSRF (Server-Side Request Forgery) defense: shared IP-range
classification used by both protection layers, so they can never
silently drift out of sync with each other.

Two layers exist, deliberately different in what they check and why:

1. **Ingestion-time** (`dart.models.events.EventIngestRequest`'s
   `target_url` validator): a fast, synchronous, zero-I/O check that
   only catches a LITERAL IP address in the URL (e.g.
   `http://127.0.0.1/hook`). It deliberately does NOT resolve DNS
   names — Pydantic validators are synchronous, and a blocking DNS
   lookup on the hot ingestion path would stall the event loop for
   every other concurrent request, directly undermining the
   high-throughput design this project is built around. A DNS name
   that currently resolves to a public IP is NOT rejected at this
   layer.

2. **Dispatch-time** (`dart.worker.ssrf_transport.SSRFSafeTransport`):
   resolves the hostname asynchronously immediately before connecting,
   checks the RESOLVED IP (not the hostname string) against this same
   blocklist, and pins the TCP connection to that exact IP. This is
   what actually closes DNS-rebinding: a hostname that resolved to a
   public IP at ingestion time but has since been repointed at a
   private one gets caught here, at the moment it matters.

Layer 1 alone is a checkbox, not a security boundary — a reviewer who
only sees ingestion-time validation and calls the system
"SSRF-protected" is overclaiming. Layer 2 is where the real protection
lives; layer 1 just gives fast-fail UX for the common case.
"""

from __future__ import annotations

import ipaddress

IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address

# Explicitly named per the required ranges, kept visible/testable in
# their own right even though `is_global` (see `is_blocked_address`
# below) already subsumes all of them plus more (multicast, "this
# network" 0.0.0.0/8, carrier-grade NAT, IPv6 unique-local, etc.).
# Belt-and-suspenders: `is_global` is the actual enforcement mechanism;
# this list exists for direct traceability to the specific requirement
# and as a redundant check in case any Python version's `is_global`
# ever behaves surprisingly for one of these.
_BLOCKED_NETWORKS: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...] = (
    ipaddress.ip_network("127.0.0.0/8"),  # IPv4 loopback
    ipaddress.ip_network("::1/128"),  # IPv6 loopback
    ipaddress.ip_network("10.0.0.0/8"),  # RFC 1918
    ipaddress.ip_network("172.16.0.0/12"),  # RFC 1918
    ipaddress.ip_network("192.168.0.0/16"),  # RFC 1918
    ipaddress.ip_network("169.254.0.0/16"),  # IPv4 link-local
    ipaddress.ip_network("fe80::/10"),  # IPv6 link-local
)


def is_blocked_address(ip: IPAddress) -> bool:
    """True if `ip` falls in a loopback, RFC 1918 private, link-local,
    multicast, or otherwise non-public-routable range DART refuses to
    dispatch to.

    Unwraps IPv4-mapped IPv6 addresses (`::ffff:a.b.c.d`) to the
    embedded IPv4 address first — otherwise a blocked IPv4 address
    disguised in mapped-IPv6 form would sail through a naive
    IPv6-only membership check.

    `is_multicast` is checked explicitly alongside `is_global` because
    it is NOT a subset of "not global" in Python's `ipaddress` module —
    a real gap found via execution while building this: `224.0.0.1`
    (IPv4 multicast) reports `is_global=True`, since multicast is a
    separate address-class concept in the stdlib's classification, not
    something `is_global` excludes on its own.
    """
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped

    in_named_blocklist = any(ip in network for network in _BLOCKED_NETWORKS)
    return in_named_blocklist or not ip.is_global or ip.is_multicast


def parse_ip_literal(host: str) -> IPAddress | None:
    """Parse `host` as a literal IP address, returning `None` if it
    isn't one (i.e. it's a DNS hostname).

    Strips a wrapping `[...]` first if present — IPv6 literals in a
    URL's host component are conventionally bracket-wrapped per RFC
    3986 (`http://[::1]/path`), but `ipaddress.ip_address()` rejects
    the brackets outright (raises `ValueError` for the literal string
    `"[::1]"`, while `"::1"` parses fine). This was a real bug found
    via execution: `http://[::1]/hook` sailed through ingestion
    validation unblocked, because the un-stripped `"[::1]"` failed to
    parse, was treated as "must be a hostname," and hostnames are
    (correctly, for DNS names) not rejected at this layer. Centralized
    here — rather than duplicated in both `is_blocked_literal_host` and
    `dart.worker.ssrf_transport`'s own literal-IP check — specifically
    so this exact class of bug can't reappear in one call site after
    being fixed in the other.
    """
    if host.startswith("[") and host.endswith("]"):
        host = host[1:-1]
    try:
        return ipaddress.ip_address(host)
    except ValueError:
        return None


def is_blocked_literal_host(host: str) -> bool:
    """True if `host` is itself a literal IP address string (optionally
    bracket-wrapped, for IPv6) that falls in a blocked range.

    Returns `False` for anything that isn't a valid literal IP — i.e.
    a DNS hostname. Resolving DNS names is deliberately not this
    function's job; see the module docstring for why that check lives
    at dispatch time instead.
    """
    ip = parse_ip_literal(host)
    if ip is None:
        return False  # not a literal IP -- a hostname, out of scope here
    return is_blocked_address(ip)
