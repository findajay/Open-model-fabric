"""Egress (SSRF) policy for operator-configurable endpoints.

Every URL that Open Model Fabric connects to on behalf of a configuration — inference
endpoints, Colab endpoints — passes through :meth:`EgressPolicy.check_url` when the
configuration is saved *and* immediately before the adapter connects (to narrow the
DNS-rebinding window).

Rules, in order:

1. Only ``http``/``https`` URLs without embedded credentials.
2. Cloud metadata endpoints (169.254.169.254, fd00:ec2::254, metadata.google.internal …)
   are always blocked, whatever the configuration says.
3. Link-local, multicast, reserved and unspecified addresses are always blocked.
4. Loopback is allowed only with ``allow_loopback`` (needed for local Ollama).
5. Private networks (RFC 1918, ULA, CGNAT) only with ``allow_private_networks``.
6. If ``allowed_hosts`` is non-empty, the hostname must match one of its entries
   (exact, or ``*.example.com`` suffix match).

Known limitation: resolution and connection are separate steps, so a hostile DNS server
could still rebind between them. Pin providers to IP literals or use an egress proxy in
high-assurance deployments (documented in docs/security).
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from collections.abc import Iterable
from urllib.parse import urlsplit

from pydantic import BaseModel, Field

from omf.core.domain.errors import EgressDenied

_ALWAYS_BLOCKED_HOSTS = frozenset(
    {
        "metadata.google.internal",
        "metadata.goog",
        "metadata",
        "instance-data",
        "instance-data.ec2.internal",
    }
)
_ALWAYS_BLOCKED_IPS = frozenset(
    {
        ipaddress.ip_address("169.254.169.254"),  # AWS/GCP/Azure/OpenStack metadata
        ipaddress.ip_address("169.254.170.2"),  # ECS task metadata
        ipaddress.ip_address("100.100.100.200"),  # Alibaba Cloud metadata
        ipaddress.ip_address("fd00:ec2::254"),  # AWS IMDS IPv6
    }
)
_CGNAT = ipaddress.ip_network("100.64.0.0/10")

IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address


class EgressPolicy(BaseModel):
    allow_loopback: bool = False
    allow_private_networks: bool = False
    allowed_hosts: list[str] = Field(default_factory=list)

    # -- public API --------------------------------------------------------------------

    def check_url_syntax(self, url: str) -> tuple[str, int]:
        """Validate scheme/host/allowlist without DNS. Returns ``(host, port)``."""
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https"):
            raise EgressDenied("endpoint must use http or https", details={"url": url})
        if parts.username or parts.password:
            raise EgressDenied(
                "credentials must not be embedded in endpoint URLs; use a secret reference",
                details={"url": _redact(url)},
            )
        host = (parts.hostname or "").rstrip(".").lower()
        if not host:
            raise EgressDenied("endpoint URL has no host", details={"url": url})
        try:
            port = parts.port or (443 if parts.scheme == "https" else 80)
        except ValueError as exc:
            raise EgressDenied("endpoint URL has an invalid port", details={"url": url}) from exc
        if host in _ALWAYS_BLOCKED_HOSTS:
            raise EgressDenied("cloud metadata endpoints are never allowed", details={"host": host})
        if self.allowed_hosts and not _host_allowed(host, self.allowed_hosts):
            raise EgressDenied(
                f"host {host!r} is not in security.egress.allowed_hosts", details={"host": host}
            )
        literal = _as_ip(host)
        if literal is not None:
            self._check_ip(literal, host)
        return host, port

    def check_ips(self, host: str, ips: Iterable[IPAddress]) -> None:
        for ip in ips:
            self._check_ip(ip, host)

    async def check_url(self, url: str) -> None:
        """Full check including DNS resolution of the host."""
        host, port = self.check_url_syntax(url)
        if _as_ip(host) is not None:
            return
        loop = asyncio.get_running_loop()
        try:
            infos = await loop.getaddrinfo(host, port, type=socket.SOCK_STREAM)
        except socket.gaierror as exc:
            # Unresolvable hosts are not an SSRF risk; the adapter reports them as
            # unavailable when it tries to connect.
            raise EgressDenied(
                f"cannot resolve host {host!r}", details={"host": host}, retryable=True
            ) from exc
        self.check_ips(host, {ipaddress.ip_address(info[4][0]) for info in infos})

    # -- internals ---------------------------------------------------------------------

    def _check_ip(self, ip: IPAddress, host: str) -> None:
        if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
            ip = ip.ipv4_mapped
        details = {"host": host, "address": str(ip)}
        if ip in _ALWAYS_BLOCKED_IPS:
            raise EgressDenied("cloud metadata endpoints are never allowed", details=details)
        if ip.is_link_local or ip.is_multicast or ip.is_unspecified or ip.is_reserved:
            raise EgressDenied(
                "link-local/multicast/reserved addresses are blocked", details=details
            )
        if ip.is_loopback:
            if not self.allow_loopback:
                raise EgressDenied(
                    "loopback endpoints are disabled (security.egress.allow_loopback)",
                    details=details,
                )
            return
        is_private = ip.is_private or (isinstance(ip, ipaddress.IPv4Address) and ip in _CGNAT)
        if is_private and not self.allow_private_networks:
            raise EgressDenied(
                "private-network endpoints are disabled (security.egress.allow_private_networks)",
                details=details,
            )


def _as_ip(host: str) -> IPAddress | None:
    try:
        return ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        return None


def _host_allowed(host: str, patterns: Iterable[str]) -> bool:
    for pattern in patterns:
        p = pattern.lower().rstrip(".")
        if p.startswith("*."):
            if host.endswith(p[1:]):
                return True
        elif host == p:
            return True
    return False


def _redact(url: str) -> str:
    parts = urlsplit(url)
    netloc = parts.hostname or ""
    if parts.port:
        netloc += f":{parts.port}"
    return parts._replace(netloc=netloc).geturl()
