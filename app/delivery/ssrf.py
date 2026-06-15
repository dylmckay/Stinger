"""SSRF guard for outbound webhook delivery.

Endpoint URLs are attacker-controlled, so before we POST to one we must prove
it doesn't resolve to infrastructure we can reach but the tenant shouldn't:
loopback, RFC-1918 private space, link-local (which includes the cloud
metadata address 169.254.169.254), multicast, reserved, and IPv4-mapped IPv6.

The guard resolves the host, validates EVERY resolved address (a hostname can
return both an allowed and a blocked record), and returns a target PINNED to a
validated IP. Pinning is what closes the DNS-rebinding TOCTOU window: the
caller connects to the exact IP we checked, not whatever a second resolution
might return. `allow_private` exists because "internal" is network-specific —
self-hosters and the test suite need to opt into local targets.
"""
from __future__ import annotations

import asyncio
import ipaddress
import socket
from dataclasses import dataclass
from urllib.parse import urlsplit, urlunsplit

ALLOWED_SCHEMES = frozenset({"http", "https"})
_DEFAULT_PORT = {"http": 80, "https": 443}


class SSRFError(Exception):
    """The target URL is malformed or resolves to a blocked address."""


@dataclass(frozen=True)
class ValidatedTarget:
    host: str           # original hostname — preserved for Host header + TLS SNI
    port: int
    scheme: str
    ip: str             # validated, pinned address to actually connect to
    connect_url: str    # scheme://<ip>:<port>/path?query
    host_header: str    # host[:port], omitting the default port


def _normalize(ip: ipaddress._BaseAddress) -> ipaddress._BaseAddress:
    # Unwrap ::ffff:x.x.x.x so an IPv4-mapped IPv6 literal can't bypass checks.
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        return ip.ipv4_mapped
    return ip


def _is_blocked(ip: ipaddress._BaseAddress) -> bool:
    ip = _normalize(ip)
    return (
        ip.is_loopback
        or ip.is_private
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    )


async def resolve_and_validate(url: str, *, allow_private: bool = False) -> ValidatedTarget:
    parts = urlsplit(url)
    if parts.scheme not in ALLOWED_SCHEMES:
        raise SSRFError(f"scheme {parts.scheme!r} not allowed")
    host = parts.hostname
    if not host:
        raise SSRFError("no host in URL")
    port = parts.port or _DEFAULT_PORT[parts.scheme]

    loop = asyncio.get_running_loop()
    try:
        infos = await loop.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror as e:
        raise SSRFError(f"DNS resolution failed: {e}") from e

    resolved = [info[4][0] for info in infos]
    if not resolved:
        raise SSRFError("host did not resolve")

    # Validate ALL resolved addresses; a single blocked one fails the URL.
    for ip_str in resolved:
        ip = ipaddress.ip_address(ip_str)
        if not allow_private and _is_blocked(ip):
            raise SSRFError(f"blocked address {ip_str}")

    pinned = resolved[0]
    is_v6 = ipaddress.ip_address(pinned).version == 6
    netloc_ip = f"[{pinned}]" if is_v6 else pinned
    connect_url = urlunsplit(
        (parts.scheme, f"{netloc_ip}:{port}", parts.path or "/", parts.query, "")
    )
    show_port = port != _DEFAULT_PORT[parts.scheme]
    host_header = f"{host}:{port}" if show_port else host

    return ValidatedTarget(
        host=host, port=port, scheme=parts.scheme,
        ip=pinned, connect_url=connect_url, host_header=host_header,
    )