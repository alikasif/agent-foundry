"""SSRF tests for netguard: URL policy, address policy and the guarded transport.

All offline. The network tests drive the REAL `GuardedNetworkBackend` and
`build_guarded_transport` through a plain `httpx.AsyncClient`; only DNS (a
dict-backed resolver) and the socket layer (a scripted inner httpcore backend
returning `httpcore.AsyncMockStream` subclasses) are faked.
"""

from __future__ import annotations

import ipaddress
import socket
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from ipaddress import IPv4Network, IPv6Network
from pathlib import Path
from typing import Any

import anyio
import httpcore
import httpx
import pytest

import newsletter_digest.netguard as netguard
from newsletter_digest.netguard import (
    BLOCKED_NETWORKS,
    MAX_URL_CHARS,
    BlockedAddressError,
    GuardedNetworkBackend,
    GuardedTransport,
    UrlPolicyError,
    build_guarded_transport,
    check_url_policy,
    default_resolver,
    is_public_address,
)

pytestmark = pytest.mark.anyio

TIMEOUT_S = 5.0
PUBLIC_IP = "93.184.215.14"
PUBLIC_IP_2 = "1.1.1.1"
PUBLIC_HOST = "news.example"
OK_RESPONSE = (
    b"HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\nContent-Length: 2\r\n\r\nok"
)


def redirect_response(location: str) -> bytes:
    """Return canned HTTP/1.1 302 bytes pointing at `location`."""
    return (
        b"HTTP/1.1 302 Found\r\nLocation: "
        + location.encode("ascii")
        + b"\r\nContent-Length: 0\r\n\r\n"
    )


# --------------------------------------------------------------------------- fakes


@dataclass
class FakeResolver:
    """Dict-backed resolver that records every (host, port) lookup.

    IP literals resolve to themselves (like getaddrinfo). Unknown hosts and
    hosts listed in `failing` raise `socket.gaierror`.
    """

    answers: dict[str, list[str]] = field(default_factory=dict)
    failing: frozenset[str] = frozenset()
    calls: list[tuple[str, int]] = field(default_factory=list)

    async def __call__(self, host: str, port: int) -> list[str]:
        """Resolve `host` from the dict, recording the call."""
        self.calls.append((host, port))
        if host in self.failing:
            raise socket.gaierror(socket.EAI_NONAME, "Name or service not known")
        if host in self.answers:
            return list(self.answers[host])
        try:
            ipaddress.ip_address(host)
        except ValueError:
            raise socket.gaierror(socket.EAI_NONAME, "Name or service not known")
        return [host]


class ScriptedStream(httpcore.AsyncMockStream):
    """Mock stream with a chosen `server_addr` that records `start_tls` and close."""

    def __init__(
        self, buffer: list[bytes], server_addr: tuple[str, int] | None = None
    ) -> None:
        super().__init__(buffer)
        self.server_addr = server_addr
        self.tls_server_hostnames: list[str | None] = []
        self.closed = False

    async def start_tls(
        self,
        ssl_context: Any,
        server_hostname: str | None = None,
        timeout: float | None = None,
    ) -> httpcore.AsyncNetworkStream:
        """Record the SNI/verification hostname and stay on this stream."""
        self.tls_server_hostnames.append(server_hostname)
        return self

    async def aclose(self) -> None:
        """Mark the stream closed."""
        self.closed = True
        await super().aclose()

    def get_extra_info(self, info: str) -> Any:
        """Return the scripted `server_addr`; defer everything else."""
        if info == "server_addr":
            return self.server_addr
        return super().get_extra_info(info)


Step = ScriptedStream | Exception


class ScriptedBackend(httpcore.AsyncNetworkBackend):
    """Inner backend: records every dial and hands out one scripted step per connect.

    `script` maps the dialled host (an IP literal, in practice) to a queue of
    steps; a step is a stream to return or an exception to raise.
    """

    def __init__(self, script: dict[str, Sequence[Step]] | None = None) -> None:
        self.script: dict[str, list[Step]] = {
            host: list(steps) for host, steps in (script or {}).items()
        }
        self.dials: list[tuple[str, int]] = []
        self.unix_dials: list[str] = []
        self.sleeps: list[float] = []
        self.streams: list[ScriptedStream] = []

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Iterable[httpcore.SOCKET_OPTION] | None = None,
    ) -> httpcore.AsyncNetworkStream:
        """Record the dial and return (or raise) the next scripted step."""
        self.dials.append((host, port))
        steps = self.script.get(host)
        if not steps:
            raise httpcore.ConnectError(f"no scripted stream for {host}")
        step = steps.pop(0)
        if isinstance(step, Exception):
            raise step
        self.streams.append(step)
        return step

    async def connect_unix_socket(
        self,
        path: str,
        timeout: float | None = None,
        socket_options: Iterable[httpcore.SOCKET_OPTION] | None = None,
    ) -> httpcore.AsyncNetworkStream:
        """Record the attempt; the guard must never get here."""
        self.unix_dials.append(path)
        return ScriptedStream([OK_RESPONSE])

    async def sleep(self, seconds: float) -> None:
        """Record the sleep without waiting."""
        self.sleeps.append(seconds)


def ok_stream(server_addr: tuple[str, int] | None = None) -> ScriptedStream:
    """Return a stream that answers one 200 OK."""
    return ScriptedStream([OK_RESPONSE], server_addr=server_addr)


def make_client(resolver: FakeResolver, backend: ScriptedBackend) -> httpx.AsyncClient:
    """Return a plain AsyncClient over the real guarded transport."""
    transport = build_guarded_transport(resolver=resolver, inner_backend=backend)
    return httpx.AsyncClient(
        transport=transport, follow_redirects=True, trust_env=False
    )


# ------------------------------------------------------------- is_public_address

BLOCKED_ADDRESSES = [
    # IPv4 BLOCKED_NETWORKS representatives
    "0.0.0.0",
    "0.1.2.3",
    "10.0.0.5",
    "10.255.255.255",
    "100.64.0.1",
    "100.127.255.254",
    "127.0.0.1",
    "127.255.255.254",
    "169.254.169.254",
    "169.254.0.1",
    "172.16.0.1",
    "172.31.255.254",
    "192.0.0.9",
    "192.0.0.170",
    "192.0.2.1",
    "192.88.99.1",
    "192.168.1.1",
    "198.18.0.1",
    "198.19.255.254",
    "198.51.100.7",
    "203.0.113.9",
    "224.0.0.1",
    "239.255.255.250",
    "240.0.0.1",
    "255.255.255.255",
    # IPv6 BLOCKED_NETWORKS representatives and flag classes
    "::",
    "::1",
    "::ffff:127.0.0.1",
    "::ffff:10.0.0.5",
    "::ffff:169.254.169.254",
    "::127.0.0.1",
    "::10.0.0.5",
    "2002:7f00:1::",
    "2002:a9fe:a9fe::1",
    "2001:0:4136:e378:8000:63bf:f5ff:fffe",  # Teredo, client 10.0.0.1
    "2001:0:a00:1:8000:63bf:f7f7:f7f7",  # Teredo, server 10.0.0.1
    "64:ff9b::a00:1",  # NAT64 of 10.0.0.1
    "64:ff9b::7f00:1",  # NAT64 of 127.0.0.1
    "64:ff9b:1::1",
    "100::1",
    "2001::1",
    "2001:3::1",
    "2001:20::1",
    "2001:db8::1",
    "fc00::1",
    "fd00:ec2::254",
    "fe80::1",
    "fe80::1%eth0",
    "fec0::1",
    "ff02::1",
    "ff05::1:3",
]


@pytest.mark.parametrize("ip", BLOCKED_ADDRESSES)
def test_is_public_address_blocks_non_public(ip: str) -> None:
    assert is_public_address(ip) is False


PUBLIC_ADDRESSES = [
    "8.8.8.8",
    "1.1.1.1",
    PUBLIC_IP,
    # just outside blocked ranges (guards against over-blocking)
    "9.255.255.255",
    "11.0.0.1",
    "100.63.255.255",
    "100.128.0.0",
    "126.255.255.255",
    "128.0.0.1",
    "169.253.255.255",
    "172.15.255.255",
    "172.32.0.1",
    "192.169.0.1",
    "198.20.0.1",
    "223.255.255.254",
    "2606:4700:4700::1111",
    "2001:4860:4860::8888",
]


@pytest.mark.parametrize("ip", PUBLIC_ADDRESSES)
def test_is_public_address_allows_public(ip: str) -> None:
    assert is_public_address(ip) is True


@pytest.mark.parametrize(
    "network", BLOCKED_NETWORKS, ids=[str(n) for n in BLOCKED_NETWORKS]
)
def test_every_blocked_network_edge_is_not_public(
    network: IPv4Network | IPv6Network,
) -> None:
    assert is_public_address(str(network.network_address)) is False
    assert is_public_address(str(network.broadcast_address)) is False
    if network.num_addresses > 1:
        assert is_public_address(str(network.network_address + 1)) is False


def test_blocked_networks_cover_the_design_list() -> None:
    expected = {
        "0.0.0.0/8",
        "10.0.0.0/8",
        "100.64.0.0/10",
        "127.0.0.0/8",
        "169.254.0.0/16",
        "172.16.0.0/12",
        "192.0.0.0/24",
        "192.0.2.0/24",
        "192.88.99.0/24",
        "192.168.0.0/16",
        "198.18.0.0/15",
        "198.51.100.0/24",
        "203.0.113.0/24",
        "224.0.0.0/4",
        "240.0.0.0/4",
        "255.255.255.255/32",
        "::/128",
        "::1/128",
        "::ffff:0:0/96",
        "64:ff9b:1::/48",
        "100::/64",
        "2001::/23",
        "2001:db8::/32",
        "fc00::/7",
        "fe80::/10",
        "fec0::/10",
        "ff00::/8",
    }
    assert {ipaddress.ip_network(n) for n in expected} <= set(BLOCKED_NETWORKS)


@pytest.mark.parametrize(
    "ip",
    [
        "::ffff:8.8.8.8",  # mapped public: outer range is blocked anyway
        "2002:808:808::1",  # 6to4 of 8.8.8.8
        "64:ff9b::808:808",  # NAT64 of 8.8.8.8 (see T028 note)
    ],
)
def test_embedded_public_ipv4_forms_are_still_blocked(ip: str) -> None:
    assert is_public_address(ip) is False


@pytest.mark.parametrize("value", ["", "not-an-ip", "999.1.1.1", "::g", "%eth0"])
def test_is_public_address_rejects_unparseable(value: str) -> None:
    assert is_public_address(value) is False


# --------------------------------------------------------------- check_url_policy


@pytest.mark.parametrize(
    "url",
    [
        "http://news.example/a",
        "https://news.example/a?b=1#c",
        "HTTPS://NEWS.EXAMPLE/",
        "http://news.example:80/",
        "https://news.example:443/",
        f"http://{PUBLIC_IP}/",
        "http://[2606:4700:4700::1111]/",
        "http://[::1]/",  # IPv6 literal: the address policy decides
        "http://bücher.example/",
        "https://www.example.co.uk/path",
    ],
)
def test_check_url_policy_allows(url: str) -> None:
    check_url_policy(url)


def test_check_url_policy_accepts_httpx_url() -> None:
    check_url_policy(httpx.URL("https://news.example/a"))


@pytest.mark.parametrize(
    ("url", "reason"),
    [
        ("ftp://news.example/", "scheme not allowed"),
        ("file:///etc/passwd", "scheme not allowed"),
        ("gopher://news.example/", "scheme not allowed"),
        ("javascript:alert(1)", "scheme not allowed"),
        ("http://user@news.example/", "userinfo not allowed"),
        ("http://user:secret@news.example/", "userinfo not allowed"),
        ("http://:secret@news.example/", "userinfo not allowed"),
        ("http://news.example:8080/", "port not allowed"),
        ("http://news.example:443/", "port not allowed"),
        ("https://news.example:80/", "port not allowed"),
        ("http://news.example:0/", "port not allowed"),
        ("http:///path", "empty host"),
        ("http://localhost/", "host not allowed"),
        ("http://LOCALHOST/", "host not allowed"),
        ("http://localhost./", "host not allowed"),
        ("http://app.localhost/", "host not allowed"),
        ("http://printer.local/", "host not allowed"),
        ("http://metadata.google.internal/", "host not allowed"),
        ("http://router.home/", "host not allowed"),
        ("http://nas.lan/", "host not allowed"),
        ("http://1.0.0.127.in-addr.arpa/", "host not allowed"),
        ("http://[fe80::1%25eth0]/", "host not allowed"),
        ("http://2130706433/", "non-canonical numeric host"),
        ("http://017700000001/", "non-canonical numeric host"),
        ("http://0x7f000001/", "non-canonical numeric host"),
        ("http://0x7f.1/", "non-canonical numeric host"),
        ("http://127.1/", "non-canonical numeric host"),
        ("http://10.1/", "non-canonical numeric host"),
        ("http://foo.123/", "non-canonical numeric host"),
        ("http://0177.0.0.1/", "invalid url"),
    ],
)
def test_check_url_policy_rejects(url: str, reason: str) -> None:
    with pytest.raises(UrlPolicyError) as excinfo:
        check_url_policy(url)
    assert excinfo.value.reason == reason
    assert isinstance(excinfo.value, ValueError)


def test_check_url_policy_length_limit() -> None:
    base = "https://news.example/"
    at_limit = base + "a" * (MAX_URL_CHARS - len(base))
    check_url_policy(at_limit)
    with pytest.raises(UrlPolicyError) as excinfo:
        check_url_policy(at_limit + "a")
    assert excinfo.value.reason == "url too long"


def test_url_policy_error_never_carries_the_url() -> None:
    url = "http://user:topsecret@news.example/private-token-xyz"
    with pytest.raises(UrlPolicyError) as excinfo:
        check_url_policy(url)
    rendered = f"{excinfo.value} {excinfo.value!r} {excinfo.value.args}"
    assert "topsecret" not in rendered
    assert "private-token-xyz" not in rendered
    assert "news.example" not in rendered


# ------------------------------------------------------------- default_resolver


async def test_default_resolver_dedups_in_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[tuple[str, int, dict[str, Any]]] = []

    async def fake_getaddrinfo(host: str, port: int, **kwargs: Any) -> list[Any]:
        seen.append((host, port, kwargs))
        return [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", port)),
            (socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("2001:db8::1", port, 0, 0)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", port)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("1.1.1.1", port)),
        ]

    monkeypatch.setattr(netguard.anyio, "getaddrinfo", fake_getaddrinfo)
    assert await default_resolver("news.example", 443) == [
        "8.8.8.8",
        "2001:db8::1",
        "1.1.1.1",
    ]
    assert seen == [("news.example", 443, {"type": socket.SOCK_STREAM})]


async def test_default_resolver_ip_literal_resolves_to_itself() -> None:
    # A numeric host never triggers DNS (AI_NUMERICHOST behaviour of getaddrinfo).
    with anyio.fail_after(TIMEOUT_S):
        assert await default_resolver(PUBLIC_IP, 80) == [PUBLIC_IP]


# ------------------------------------------- GuardedNetworkBackend (direct calls)


async def test_backend_blocks_private_answer_without_dialling() -> None:
    resolver = FakeResolver({"intranet.example": ["10.0.0.5"]})
    inner = ScriptedBackend({"10.0.0.5": [ok_stream()]})
    guard = GuardedNetworkBackend(inner, resolver)
    with anyio.fail_after(TIMEOUT_S), pytest.raises(BlockedAddressError):
        await guard.connect_tcp("intranet.example", 80)
    assert resolver.calls == [("intranet.example", 80)]
    assert inner.dials == []


@pytest.mark.parametrize(
    "answer",
    [
        [PUBLIC_IP, "10.0.0.5"],
        ["10.0.0.5", PUBLIC_IP],
        [PUBLIC_IP, "::ffff:127.0.0.1"],
        [PUBLIC_IP, "2606:4700:4700::1111", "fd00:ec2::254"],
    ],
)
async def test_backend_blocks_mixed_answer_without_dialling(answer: list[str]) -> None:
    resolver = FakeResolver({"rebind.example": answer})
    inner = ScriptedBackend({ip: [ok_stream()] for ip in answer})
    guard = GuardedNetworkBackend(inner, resolver)
    with anyio.fail_after(TIMEOUT_S), pytest.raises(BlockedAddressError):
        await guard.connect_tcp("rebind.example", 80)
    assert inner.dials == []


async def test_backend_dials_ip_literal_not_hostname() -> None:
    resolver = FakeResolver({PUBLIC_HOST: [PUBLIC_IP]})
    stream = ok_stream()
    inner = ScriptedBackend({PUBLIC_IP: [stream]})
    guard = GuardedNetworkBackend(inner, resolver)
    with anyio.fail_after(TIMEOUT_S):
        got = await guard.connect_tcp(PUBLIC_HOST, 80, timeout=1.0)
    assert got is stream
    assert inner.dials == [(PUBLIC_IP, 80)]


async def test_backend_normalises_ipv6_answer_before_dialling() -> None:
    resolver = FakeResolver({PUBLIC_HOST: ["2606:4700:4700:0:0:0:0:1111"]})
    inner = ScriptedBackend({"2606:4700:4700::1111": [ok_stream()]})
    guard = GuardedNetworkBackend(inner, resolver)
    with anyio.fail_after(TIMEOUT_S):
        await guard.connect_tcp(PUBLIC_HOST, 443)
    assert inner.dials == [("2606:4700:4700::1111", 443)]


async def test_backend_tries_next_ip_after_connect_failure() -> None:
    resolver = FakeResolver({PUBLIC_HOST: [PUBLIC_IP, PUBLIC_IP_2]})
    stream = ok_stream()
    inner = ScriptedBackend(
        {
            PUBLIC_IP: [httpcore.ConnectError("refused")],
            PUBLIC_IP_2: [stream],
        }
    )
    guard = GuardedNetworkBackend(inner, resolver)
    with anyio.fail_after(TIMEOUT_S):
        got = await guard.connect_tcp(PUBLIC_HOST, 80)
    assert got is stream
    assert inner.dials == [(PUBLIC_IP, 80), (PUBLIC_IP_2, 80)]


async def test_backend_tries_next_ip_after_connect_timeout() -> None:
    resolver = FakeResolver({PUBLIC_HOST: [PUBLIC_IP, PUBLIC_IP_2]})
    inner = ScriptedBackend(
        {
            PUBLIC_IP: [httpcore.ConnectTimeout("slow")],
            PUBLIC_IP_2: [ok_stream()],
        }
    )
    guard = GuardedNetworkBackend(inner, resolver)
    with anyio.fail_after(TIMEOUT_S):
        await guard.connect_tcp(PUBLIC_HOST, 80)
    assert inner.dials == [(PUBLIC_IP, 80), (PUBLIC_IP_2, 80)]


async def test_backend_raises_last_error_when_every_ip_fails() -> None:
    resolver = FakeResolver({PUBLIC_HOST: [PUBLIC_IP, PUBLIC_IP_2]})
    inner = ScriptedBackend(
        {
            PUBLIC_IP: [httpcore.ConnectError("first")],
            PUBLIC_IP_2: [httpcore.ConnectError("last")],
        }
    )
    guard = GuardedNetworkBackend(inner, resolver)
    with (
        anyio.fail_after(TIMEOUT_S),
        pytest.raises(httpcore.ConnectError, match="last"),
    ):
        await guard.connect_tcp(PUBLIC_HOST, 80)


async def test_backend_empty_answer_is_connect_error() -> None:
    resolver = FakeResolver({PUBLIC_HOST: []})
    inner = ScriptedBackend()
    guard = GuardedNetworkBackend(inner, resolver)
    with anyio.fail_after(TIMEOUT_S), pytest.raises(httpcore.ConnectError):
        await guard.connect_tcp(PUBLIC_HOST, 80)
    assert inner.dials == []


@pytest.mark.parametrize(
    "server_addr",
    [
        ("10.0.0.5", 80),  # private
        ("169.254.169.254", 80),  # metadata
        (PUBLIC_IP_2, 80),  # public but not the dialled IP
        ("::ffff:93.184.215.14", 80),  # same IPv4, mapped form: not public
    ],
)
async def test_backend_server_addr_mismatch_closes_and_blocks(
    server_addr: tuple[str, int],
) -> None:
    resolver = FakeResolver({PUBLIC_HOST: [PUBLIC_IP, PUBLIC_IP_2]})
    bad = ok_stream(server_addr=server_addr)
    inner = ScriptedBackend({PUBLIC_IP: [bad], PUBLIC_IP_2: [ok_stream()]})
    guard = GuardedNetworkBackend(inner, resolver)
    with anyio.fail_after(TIMEOUT_S), pytest.raises(BlockedAddressError):
        await guard.connect_tcp(PUBLIC_HOST, 80)
    assert bad.closed is True
    # A mismatch never fails over to the next address.
    assert inner.dials == [(PUBLIC_IP, 80)]


@pytest.mark.parametrize(
    ("answer", "server_addr"),
    [
        (PUBLIC_IP, (PUBLIC_IP, 80)),
        ("2606:4700:4700::1111", ("2606:4700:4700:0::1111", 443)),
    ],
)
async def test_backend_matching_server_addr_is_accepted(
    answer: str, server_addr: tuple[str, int]
) -> None:
    resolver = FakeResolver({PUBLIC_HOST: [answer]})
    stream = ok_stream(server_addr=server_addr)
    dialled = str(ipaddress.ip_address(answer))
    inner = ScriptedBackend({dialled: [stream]})
    guard = GuardedNetworkBackend(inner, resolver)
    with anyio.fail_after(TIMEOUT_S):
        assert await guard.connect_tcp(PUBLIC_HOST, server_addr[1]) is stream
    assert stream.closed is False


async def test_backend_refuses_unix_socket() -> None:
    resolver = FakeResolver()
    inner = ScriptedBackend()
    guard = GuardedNetworkBackend(inner, resolver)
    with anyio.fail_after(TIMEOUT_S), pytest.raises(BlockedAddressError):
        await guard.connect_unix_socket("/var/run/docker.sock")
    assert inner.unix_dials == []
    assert resolver.calls == []


async def test_backend_sleep_delegates() -> None:
    inner = ScriptedBackend()
    guard = GuardedNetworkBackend(inner, FakeResolver())
    await guard.sleep(0.25)
    assert inner.sleeps == [0.25]


async def test_backend_resolver_gaierror_propagates() -> None:
    resolver = FakeResolver(failing=frozenset({"gone.example"}))
    inner = ScriptedBackend()
    guard = GuardedNetworkBackend(inner, resolver)
    with anyio.fail_after(TIMEOUT_S), pytest.raises(socket.gaierror):
        await guard.connect_tcp("gone.example", 80)
    assert inner.dials == []


# ------------------------------------------- through httpx + the real transport


async def test_public_request_dials_ip_literal_only() -> None:
    resolver = FakeResolver({PUBLIC_HOST: [PUBLIC_IP]})
    backend = ScriptedBackend({PUBLIC_IP: [ok_stream()]})
    with anyio.fail_after(TIMEOUT_S):
        async with make_client(resolver, backend) as client:
            response = await client.get(f"http://{PUBLIC_HOST}/article")
    assert response.status_code == 200
    assert response.text == "ok"
    assert resolver.calls == [(PUBLIC_HOST, 80)]
    assert backend.dials == [(PUBLIC_IP, 80)]
    assert all(host != PUBLIC_HOST for host, _ in backend.dials)


async def test_https_tls_uses_original_hostname() -> None:
    # V13: the IP literal is dialled, but SNI/cert verification use the hostname.
    resolver = FakeResolver({PUBLIC_HOST: [PUBLIC_IP]})
    stream = ok_stream()
    backend = ScriptedBackend({PUBLIC_IP: [stream]})
    with anyio.fail_after(TIMEOUT_S):
        async with make_client(resolver, backend) as client:
            response = await client.get(f"https://{PUBLIC_HOST}/article")
    assert response.status_code == 200
    assert backend.dials == [(PUBLIC_IP, 443)]
    assert stream.tls_server_hostnames == [PUBLIC_HOST]


@pytest.mark.parametrize(
    "url",
    [
        "http://169.254.169.254/latest/meta-data/",
        "http://10.0.0.5/",
        "http://[::1]/",
        "http://[::ffff:127.0.0.1]/",
        "http://[fd00:ec2::254]/",
        "http://[64:ff9b::a9fe:a9fe]/",
    ],
)
async def test_private_ip_literal_url_is_blocked(url: str) -> None:
    resolver = FakeResolver()
    backend = ScriptedBackend()
    with anyio.fail_after(TIMEOUT_S):
        async with make_client(resolver, backend) as client:
            with pytest.raises(BlockedAddressError):
                await client.get(url)
    assert backend.dials == []


async def test_hostname_resolving_private_is_blocked_through_httpx() -> None:
    resolver = FakeResolver({"intranet.example": ["192.168.1.10"]})
    backend = ScriptedBackend({"192.168.1.10": [ok_stream()]})
    with anyio.fail_after(TIMEOUT_S):
        async with make_client(resolver, backend) as client:
            with pytest.raises(BlockedAddressError) as excinfo:
                await client.get("http://intranet.example/")
    # Propagates unchanged: not wrapped into an httpx transport error.
    assert type(excinfo.value) is BlockedAddressError
    assert not isinstance(excinfo.value, httpx.HTTPError)
    assert backend.dials == []


async def test_mixed_answer_is_blocked_through_httpx() -> None:
    resolver = FakeResolver({"rebind.example": ["8.8.8.8", "10.0.0.5"]})
    backend = ScriptedBackend({"8.8.8.8": [ok_stream()], "10.0.0.5": [ok_stream()]})
    with anyio.fail_after(TIMEOUT_S):
        async with make_client(resolver, backend) as client:
            with pytest.raises(BlockedAddressError):
                await client.get("http://rebind.example/")
    assert backend.dials == []


@pytest.mark.parametrize(
    ("location", "answers", "private_ip"),
    [
        ("http://169.254.169.254/latest/meta-data/", {}, "169.254.169.254"),
        ("http://evil.example/", {"evil.example": ["10.0.0.5"]}, "10.0.0.5"),
        (
            "https://evil.example/x",
            {"evil.example": ["2606:4700:4700::1111", "fd00:ec2::254"]},
            "fd00:ec2::254",
        ),
    ],
)
async def test_redirect_to_private_is_blocked(
    location: str, answers: dict[str, list[str]], private_ip: str
) -> None:
    resolver = FakeResolver({PUBLIC_HOST: [PUBLIC_IP], **answers})
    backend = ScriptedBackend(
        {
            PUBLIC_IP: [ScriptedStream([redirect_response(location)])],
            private_ip: [ok_stream()],
        }
    )
    with anyio.fail_after(TIMEOUT_S):
        async with make_client(resolver, backend) as client:
            with pytest.raises(BlockedAddressError) as excinfo:
                await client.get(f"http://{PUBLIC_HOST}/start")
    assert type(excinfo.value) is BlockedAddressError
    assert backend.dials == [(PUBLIC_IP, 80)]
    assert all(host != private_ip for host, _ in backend.dials)
    assert len(resolver.calls) == 2


async def test_redirect_to_public_host_is_followed() -> None:
    resolver = FakeResolver({PUBLIC_HOST: [PUBLIC_IP], "cdn.example": [PUBLIC_IP_2]})
    backend = ScriptedBackend(
        {
            PUBLIC_IP: [ScriptedStream([redirect_response("http://cdn.example/a")])],
            PUBLIC_IP_2: [ok_stream()],
        }
    )
    with anyio.fail_after(TIMEOUT_S):
        async with make_client(resolver, backend) as client:
            response = await client.get(f"http://{PUBLIC_HOST}/start")
    assert response.status_code == 200
    assert backend.dials == [(PUBLIC_IP, 80), (PUBLIC_IP_2, 80)]


async def test_server_addr_mismatch_through_httpx_closes_stream() -> None:
    resolver = FakeResolver({PUBLIC_HOST: [PUBLIC_IP]})
    bad = ok_stream(server_addr=("10.0.0.5", 80))
    backend = ScriptedBackend({PUBLIC_IP: [bad]})
    with anyio.fail_after(TIMEOUT_S):
        async with make_client(resolver, backend) as client:
            with pytest.raises(BlockedAddressError):
                await client.get(f"http://{PUBLIC_HOST}/")
    assert bad.closed is True


async def test_next_ip_tried_after_connect_failure_through_httpx() -> None:
    resolver = FakeResolver({PUBLIC_HOST: [PUBLIC_IP, PUBLIC_IP_2]})
    backend = ScriptedBackend(
        {PUBLIC_IP: [httpcore.ConnectError("refused")], PUBLIC_IP_2: [ok_stream()]}
    )
    with anyio.fail_after(TIMEOUT_S):
        async with make_client(resolver, backend) as client:
            response = await client.get(f"http://{PUBLIC_HOST}/")
    assert response.status_code == 200
    assert backend.dials == [(PUBLIC_IP, 80), (PUBLIC_IP_2, 80)]


async def test_connect_failure_is_not_retried_by_pool() -> None:
    # retries=0: one failing address means exactly one dial and no backoff sleep.
    resolver = FakeResolver({PUBLIC_HOST: [PUBLIC_IP]})
    backend = ScriptedBackend({PUBLIC_IP: [httpcore.ConnectError("refused")]})
    with anyio.fail_after(TIMEOUT_S):
        async with make_client(resolver, backend) as client:
            with pytest.raises(httpx.ConnectError):
                await client.get(f"http://{PUBLIC_HOST}/")
    assert backend.dials == [(PUBLIC_IP, 80)]
    assert backend.sleeps == []
    assert resolver.calls == [(PUBLIC_HOST, 80)]


async def test_empty_answer_surfaces_as_httpx_connect_error() -> None:
    resolver = FakeResolver({PUBLIC_HOST: []})
    backend = ScriptedBackend()
    with anyio.fail_after(TIMEOUT_S):
        async with make_client(resolver, backend) as client:
            with pytest.raises(httpx.ConnectError):
                await client.get(f"http://{PUBLIC_HOST}/")
    assert backend.dials == []


async def test_resolver_gaierror_surfaces_as_network_oserror() -> None:
    # AF-6 maps a resolver gaierror/OSError to NETWORK; it must not look UNSAFE.
    resolver = FakeResolver(failing=frozenset({"gone.example"}))
    backend = ScriptedBackend()
    with anyio.fail_after(TIMEOUT_S):
        async with make_client(resolver, backend) as client:
            with pytest.raises(OSError) as excinfo:
                await client.get("http://gone.example/")
    assert isinstance(excinfo.value, socket.gaierror)
    assert not isinstance(excinfo.value, BlockedAddressError)
    assert backend.dials == []


async def test_pooled_connection_reused_only_for_same_origin() -> None:
    resolver = FakeResolver({PUBLIC_HOST: [PUBLIC_IP]})
    stream = ScriptedStream([OK_RESPONSE, OK_RESPONSE])
    backend = ScriptedBackend({PUBLIC_IP: [stream]})
    with anyio.fail_after(TIMEOUT_S):
        async with make_client(resolver, backend) as client:
            await client.get(f"http://{PUBLIC_HOST}/one")
            await client.get(f"http://{PUBLIC_HOST}/two")
            with pytest.raises(BlockedAddressError):
                await client.get("http://10.0.0.5/")
    assert backend.dials == [(PUBLIC_IP, 80)]
    assert resolver.calls == [(PUBLIC_HOST, 80), ("10.0.0.5", 80)]


# ------------------------------------------------------- R-F1 canary and env


async def test_canary_guarded_transport_calls_resolver() -> None:
    # R-F1: if httpx stops routing requests through `_pool`, the guard is bypassed
    # and the recording resolver is never called.
    resolver = FakeResolver({PUBLIC_HOST: [PUBLIC_IP]})
    backend = ScriptedBackend({PUBLIC_IP: [ok_stream()]})
    transport = build_guarded_transport(resolver=resolver, inner_backend=backend)
    assert isinstance(transport, GuardedTransport)
    assert isinstance(transport, httpx.AsyncHTTPTransport)
    with anyio.fail_after(TIMEOUT_S):
        async with httpx.AsyncClient(transport=transport, trust_env=False) as client:
            response = await client.get(f"http://{PUBLIC_HOST}/")
    assert response.status_code == 200
    assert resolver.calls == [(PUBLIC_HOST, 80)]
    assert backend.dials == [(PUBLIC_IP, 80)]


async def test_canary_direct_transport_request_calls_resolver() -> None:
    resolver = FakeResolver({PUBLIC_HOST: [PUBLIC_IP]})
    backend = ScriptedBackend({PUBLIC_IP: [ok_stream()]})
    transport = build_guarded_transport(resolver=resolver, inner_backend=backend)
    request = httpx.Request("GET", f"http://{PUBLIC_HOST}/")
    with anyio.fail_after(TIMEOUT_S):
        async with transport:
            response = await transport.handle_async_request(request)
            await response.aread()
    assert response.status_code == 200
    assert resolver.calls == [(PUBLIC_HOST, 80)]


@pytest.mark.parametrize("client_trust_env", [False, True])
async def test_proxy_env_is_ignored(
    monkeypatch: pytest.MonkeyPatch, client_trust_env: bool
) -> None:
    proxy = "http://203.0.113.50:3128"
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy"):
        monkeypatch.setenv(name, proxy)
    monkeypatch.delenv("NO_PROXY", raising=False)
    monkeypatch.delenv("no_proxy", raising=False)
    resolver = FakeResolver({PUBLIC_HOST: [PUBLIC_IP], "203.0.113.50": ["8.8.4.4"]})
    backend = ScriptedBackend({PUBLIC_IP: [ok_stream()], "8.8.4.4": [ok_stream()]})
    transport = build_guarded_transport(resolver=resolver, inner_backend=backend)
    with anyio.fail_after(TIMEOUT_S):
        async with httpx.AsyncClient(
            transport=transport, trust_env=client_trust_env
        ) as client:
            response = await client.get(f"http://{PUBLIC_HOST}/")
    assert response.status_code == 200
    assert resolver.calls == [(PUBLIC_HOST, 80)]
    assert backend.dials == [(PUBLIC_IP, 80)]


def test_ssl_cert_env_is_ignored(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # With trust_env the missing CA file would make SSL context creation fail.
    missing = tmp_path / "missing.pem"
    monkeypatch.setenv("SSL_CERT_FILE", str(missing))
    with pytest.raises(FileNotFoundError):
        httpx.create_ssl_context(trust_env=True)
    transport = build_guarded_transport(resolver=FakeResolver())
    assert isinstance(transport, GuardedTransport)
