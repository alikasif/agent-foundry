"""SSRF policy and enforcement for article fetching (FR-031).

Role: URL policy checks, public-address test, blocked networks, the guarded
network backend and transport builder.

Allowed imports: stdlib, `anyio`, `httpcore` and `httpx`; no project modules.
"""

from __future__ import annotations

import ipaddress
import re
import socket
from collections.abc import Awaitable, Callable, Iterable
from ipaddress import IPv4Address, IPv4Network, IPv6Address, IPv6Network

import anyio
import httpcore
import httpx

MAX_URL_CHARS = 2048

ALLOWED_SCHEMES = frozenset({"http", "https"})
BLOCKED_HOST_SUFFIXES = (".localhost", ".local", ".internal", ".home", ".lan", ".arpa")

BLOCKED_NETWORKS: tuple[IPv4Network | IPv6Network, ...] = (
    IPv4Network("0.0.0.0/8"),
    IPv4Network("10.0.0.0/8"),
    IPv4Network("100.64.0.0/10"),
    IPv4Network("127.0.0.0/8"),
    IPv4Network("169.254.0.0/16"),  # cloud metadata 169.254.169.254
    IPv4Network("172.16.0.0/12"),
    IPv4Network("192.0.0.0/24"),
    IPv4Network("192.0.2.0/24"),
    IPv4Network("192.88.99.0/24"),
    IPv4Network("192.168.0.0/16"),
    IPv4Network("198.18.0.0/15"),
    IPv4Network("198.51.100.0/24"),
    IPv4Network("203.0.113.0/24"),
    IPv4Network("224.0.0.0/4"),
    IPv4Network("240.0.0.0/4"),
    IPv4Network("255.255.255.255/32"),
    IPv6Network("::/128"),
    IPv6Network("::1/128"),
    IPv6Network("::ffff:0:0/96"),
    IPv6Network("64:ff9b:1::/48"),
    IPv6Network("100::/64"),
    IPv6Network("2001::/23"),
    IPv6Network("2001:db8::/32"),
    IPv6Network("fc00::/7"),  # unique local; covers AWS IMDSv6 fd00:ec2::254
    IPv6Network("fe80::/10"),
    IPv6Network("fec0::/10"),
    IPv6Network("ff00::/8"),
)

_NAT64_NETWORK = IPv6Network("64:ff9b::/96")
_IPV4_COMPATIBLE_NETWORK = IPv6Network("::/96")

# A label made only of digits or a 0x hex prefix: getaddrinfo (inet_aton) reads
# such hosts as IPv4 numbers, e.g. 2130706433, 0x7f.1, 017700000001, 127.1.
_NUMERIC_LABEL_RE = re.compile(r"0x[0-9a-f]*|[0-9]+")

Resolver = Callable[[str, int], Awaitable[list[str]]]


class UrlPolicyError(ValueError):
    """A URL failed the AF-3.1 policy; `reason` is short and never holds the URL."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class BlockedAddressError(OSError):
    """A connection target is not a public address (AF-3.2).

    It is not an httpcore exception, so httpx does not remap it and it reaches
    the caller unchanged.
    """


async def default_resolver(host: str, port: int) -> list[str]:
    """Resolve `host` to IP strings, deduplicated with order kept.

    An IP literal resolves to itself (getaddrinfo treats it as numeric).
    """
    infos = await anyio.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return list(dict.fromkeys(sockaddr[0] for *_, sockaddr in infos))


def check_url_policy(url: httpx.URL | str) -> None:
    """Raise `UrlPolicyError` unless `url` passes the AF-3.1 URL policy.

    Checked: length, scheme (http/https), userinfo, explicit non-default port
    and host (empty, localhost, private suffixes, non-canonical numeric forms).
    """
    if len(str(url)) > MAX_URL_CHARS:
        raise UrlPolicyError("url too long")
    try:
        parsed = httpx.URL(url)
    except httpx.InvalidURL:
        raise UrlPolicyError("invalid url") from None
    if parsed.scheme not in ALLOWED_SCHEMES:
        raise UrlPolicyError("scheme not allowed")
    if parsed.userinfo:
        raise UrlPolicyError("userinfo not allowed")
    # httpx drops an explicit default port (http :80, https :443) to None.
    if parsed.port is not None:
        raise UrlPolicyError("port not allowed")
    _check_host(parsed.raw_host.decode("ascii").lower().rstrip("."))


def _check_host(host: str) -> None:
    """Raise `UrlPolicyError` for an empty, local or non-canonical numeric host.

    `host` is the lower-cased ASCII (IDNA) host without a trailing dot.
    """
    if not host:
        raise UrlPolicyError("empty host")
    if "%" in host:
        raise UrlPolicyError("host not allowed")  # percent-encoding or zone id
    if host == "localhost" or host.endswith(BLOCKED_HOST_SUFFIXES):
        raise UrlPolicyError("host not allowed")
    if ":" in host:
        return  # IPv6 literal: the address policy decides
    last_label = host.rsplit(".", 1)[-1]
    if _NUMERIC_LABEL_RE.fullmatch(last_label) and not _is_canonical_ipv4(host):
        raise UrlPolicyError("non-canonical numeric host")


def _is_canonical_ipv4(host: str) -> bool:
    """True when `host` is a plain dotted-quad IPv4 address with no leading zeros."""
    try:
        return str(IPv4Address(host)) == host
    except ValueError:
        return False


def is_public_address(ip: str) -> bool:
    """True only when `ip` is a globally routable unicast address (AF-3.2).

    A scope id is stripped first. Every IPv4 address embedded in an IPv6 one
    (mapped, compatible, 6to4, Teredo server and client, NAT64) must pass, and
    then the address itself must pass the flag checks and `BLOCKED_NETWORKS`.
    Unparseable input is not public.
    """
    try:
        addr = _parse_ip(ip)
    except ValueError:
        return False
    if isinstance(addr, IPv6Address) and not all(
        is_public_address(str(embedded)) for embedded in _embedded_ipv4(addr)
    ):
        return False
    return (
        addr.is_global
        and not addr.is_multicast
        and not addr.is_reserved
        and not addr.is_unspecified
        and not addr.is_loopback
        and not addr.is_link_local
        and not any(addr in network for network in BLOCKED_NETWORKS)
    )


def _parse_ip(ip: str) -> IPv4Address | IPv6Address:
    """Parse an IP string, dropping any `%scope` suffix; raise ValueError if invalid."""
    return ipaddress.ip_address(ip.split("%", 1)[0])


def _embedded_ipv4(addr: IPv6Address) -> list[IPv4Address]:
    """Return the IPv4 addresses embedded in `addr` (empty when there are none)."""
    if addr.ipv4_mapped is not None:
        return [addr.ipv4_mapped]
    if addr.sixtofour is not None:
        return [addr.sixtofour]
    if addr.teredo is not None:
        return list(addr.teredo)
    if addr in _NAT64_NETWORK or addr in _IPV4_COMPATIBLE_NETWORK:
        return [IPv4Address(int(addr) & 0xFFFFFFFF)]
    return []


class GuardedNetworkBackend(httpcore.AsyncNetworkBackend):
    """httpcore backend that only ever connects to validated public IPs (AF-3.2)."""

    def __init__(self, inner: httpcore.AsyncNetworkBackend, resolver: Resolver) -> None:
        self._inner = inner
        self._resolver = resolver

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Iterable[httpcore.SOCKET_OPTION] | None = None,
    ) -> httpcore.AsyncNetworkStream:
        """Resolve, reject the whole answer if any IP is not public, dial an IP.

        The validated IP literal is dialled, never the hostname, so a second
        DNS lookup cannot be steered. On connect failure the next IP is tried.
        """
        ips = await self._resolver(host, port)
        if not ips:
            raise httpcore.ConnectError("no addresses resolved")
        if not all(is_public_address(ip) for ip in ips):
            raise BlockedAddressError("address not allowed")
        *fallbacks, last = [_parse_ip(ip) for ip in ips]
        for address in fallbacks:
            try:
                return await self._dial(
                    address, port, timeout, local_address, socket_options
                )
            except (httpcore.ConnectError, httpcore.ConnectTimeout):
                continue
        return await self._dial(last, port, timeout, local_address, socket_options)

    async def _dial(
        self,
        address: IPv4Address | IPv6Address,
        port: int,
        timeout: float | None,
        local_address: str | None,
        socket_options: Iterable[httpcore.SOCKET_OPTION] | None,
    ) -> httpcore.AsyncNetworkStream:
        stream = await self._inner.connect_tcp(
            str(address),
            port,
            timeout=timeout,
            local_address=local_address,
            socket_options=socket_options,
        )
        server_addr = stream.get_extra_info("server_addr")
        if server_addr is None:
            return stream
        connected = server_addr[0]
        if not is_public_address(connected) or _parse_ip(connected) != address:
            await stream.aclose()
            raise BlockedAddressError("connected address not allowed")
        return stream

    async def connect_unix_socket(
        self,
        path: str,
        timeout: float | None = None,
        socket_options: Iterable[httpcore.SOCKET_OPTION] | None = None,
    ) -> httpcore.AsyncNetworkStream:
        """Always refuse: article fetching never uses unix sockets."""
        raise BlockedAddressError("unix sockets not allowed")

    async def sleep(self, seconds: float) -> None:
        """Delegate to the inner backend."""
        await self._inner.sleep(seconds)


class GuardedTransport(httpx.AsyncHTTPTransport):
    """httpx transport whose connection pool dials through `GuardedNetworkBackend`.

    Keeps httpx's request/response mapping and `map_httpcore_exceptions`.
    """

    def __init__(
        self,
        *,
        resolver: Resolver,
        inner_backend: httpcore.AsyncNetworkBackend | None = None,
        max_connections: int = 4,
    ) -> None:
        super().__init__(trust_env=False, http1=True, http2=False)
        # httpcore defines a non-backend stub AnyIOBackend when anyio is missing;
        # anyio is a direct dependency, so this is always the real backend.
        # pyrefly: ignore[bad-assignment]
        default_backend: httpcore.AsyncNetworkBackend = httpcore.AnyIOBackend()
        # R-F1: `_pool` is a private httpx attribute (httpx pinned <0.29). If a
        # future httpx stops routing requests through it, the guard is bypassed;
        # the canary test (recording resolver must be called) catches that.
        self._pool = httpcore.AsyncConnectionPool(
            ssl_context=httpx.create_ssl_context(trust_env=False),
            network_backend=GuardedNetworkBackend(
                inner_backend or default_backend, resolver
            ),
            max_connections=max_connections,
            http1=True,
            http2=False,
            retries=0,
        )


def build_guarded_transport(
    *,
    resolver: Resolver = default_resolver,
    inner_backend: httpcore.AsyncNetworkBackend | None = None,
    max_connections: int = 4,
) -> GuardedTransport:
    """Build the SSRF-guarded transport used by `PageFetcher` (AF-3.3)."""
    return GuardedTransport(
        resolver=resolver,
        inner_backend=inner_backend,
        max_connections=max_connections,
    )
