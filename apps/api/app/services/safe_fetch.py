"""One place that fetches a URL on a tenant's behalf (the website crawler today;
tools and webhooks can move onto it) without letting that URL reach our own network.

What it guards against (SSRF): a host that resolves to a private, loopback, link-local
or cloud-metadata address; a public host that redirects to one; DNS that answers
differently between the check and the connection (rebinding); odd IP spellings
(decimal, hex, IPv4-mapped IPv6); credentials in the URL; ports other than 80/443;
and bodies of unbounded size.

How: resolve the name ourselves, require every address to be public, then connect to
that exact address (the check and the connection use the same answer) while keeping the
original Host and TLS name. Redirects are followed by hand and each hop is checked again.
Only GET is ever sent."""

import asyncio
import ipaddress
import socket
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from urllib.parse import urljoin, urlsplit

import httpx

USER_AGENT = "CallPlusBot/1.0 (+https://callplus.autobot360.com)"
ALLOWED_PORTS = {80, 443}
MAX_REDIRECTS = 5
Resolver = Callable[[str, int], Awaitable[list[str]]]


class UnsafeURL(ValueError):
    """The URL (or a redirect target) is not one we will fetch."""


class FetchError(Exception):
    """The fetch failed for a reason worth showing: unreachable, too big, wrong type."""


@dataclass
class FetchResult:
    url: str  # final URL after redirects
    status: int
    content_type: str
    body: bytes


def is_public_address(address: str) -> bool:
    ip = ipaddress.ip_address(address)
    if isinstance(ip, ipaddress.IPv6Address):
        if ip.ipv4_mapped is not None:
            return is_public_address(str(ip.ipv4_mapped))
        # NAT64 (64:ff9b::/96) wraps an IPv4 address: judge that one.
        if ip in ipaddress.ip_network("64:ff9b::/96"):
            return is_public_address(str(ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF)))
        if ip in ipaddress.ip_network("2002::/16"):  # 6to4 wraps one too
            return is_public_address(str(ipaddress.IPv4Address((int(ip) >> 80) & 0xFFFFFFFF)))
    return ip.is_global and not ip.is_multicast


async def _system_resolver(host: str, port: int) -> list[str]:
    infos = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return list(dict.fromkeys(info[4][0] for info in infos))


def check_url(url: str) -> tuple[str, str, int]:
    """Returns (scheme, host, port) for a URL we are willing to consider, before any lookup."""
    if len(url) > 2048:
        raise UnsafeURL("The address is too long")
    parts = urlsplit(url.strip())
    if parts.scheme not in ("http", "https"):
        raise UnsafeURL("Only http:// and https:// addresses can be fetched")
    if parts.username or parts.password:
        raise UnsafeURL("The address must not contain a username or password")
    host = parts.hostname
    if not host:
        raise UnsafeURL("The address has no host name")
    try:
        port = parts.port or (443 if parts.scheme == "https" else 80)
    except ValueError as exc:
        raise UnsafeURL("The address has an invalid port") from exc
    if port not in ALLOWED_PORTS:
        raise UnsafeURL("Only ports 80 and 443 can be fetched")
    return parts.scheme, host, port


async def resolve_public(host: str, port: int, resolver: Resolver | None = None) -> list[str]:
    """Every address the host resolves to, provided all of them are public."""
    try:
        addresses = await (resolver or _system_resolver)(host, port)
    except (socket.gaierror, OSError) as exc:
        raise FetchError(f"Could not find {host}") from exc
    if not addresses:
        raise FetchError(f"Could not find {host}")
    if not all(is_public_address(a) for a in addresses):
        raise UnsafeURL("That address points to a private network and cannot be fetched")
    return addresses


async def assert_fetchable(url: str, resolver: Resolver | None = None) -> None:
    scheme, host, port = check_url(url)
    await resolve_public(host, port, resolver)


def _media_type(value: str | None) -> str:
    return (value or "").split(";")[0].strip().lower()


async def safe_get(
    url: str,
    *,
    max_bytes: int = 2_000_000,
    timeout: float = 15.0,
    allowed_types: tuple[str, ...] | None = None,
    resolver: Resolver | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
    user_agent: str = USER_AGENT,
) -> FetchResult:
    """GET `url`, following up to MAX_REDIRECTS redirects, each re-checked.

    Returns the final response whatever its status; raises UnsafeURL for a forbidden
    address and FetchError for a network failure, an oversized body, or (on a 200) a
    content type outside `allowed_types`."""
    current = url
    try:
        async with asyncio.timeout(timeout * 2):
            async with httpx.AsyncClient(
                transport=transport,
                follow_redirects=False,
                timeout=timeout,
                trust_env=False,  # never route through a proxy from the environment
            ) as client:
                for _hop in range(MAX_REDIRECTS + 1):
                    scheme, host, port = check_url(current)
                    address = (await resolve_public(host, port, resolver))[0]
                    literal = f"[{address}]" if ":" in address else address
                    default_port = 443 if scheme == "https" else 80
                    pinned = urlsplit(current)._replace(
                        netloc=f"{literal}:{port}" if port != default_port else literal
                    ).geturl()
                    headers = {
                        "Host": host if port == default_port else f"{host}:{port}",
                        "User-Agent": user_agent,
                        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,text/plain;q=0.8",
                        "Accept-Encoding": "gzip",
                    }
                    async with client.stream(
                        "GET", pinned, headers=headers, extensions={"sni_hostname": host}
                    ) as response:
                        if response.status_code in (301, 302, 303, 307, 308):
                            location = response.headers.get("location")
                            if not location:
                                raise FetchError("The site redirected without saying where")
                            current = urljoin(current, location)
                            continue
                        kind = _media_type(response.headers.get("content-type"))
                        if response.status_code == 200 and allowed_types and kind not in allowed_types:
                            raise FetchError(f"Unsupported content type: {kind or 'unknown'}")
                        declared = response.headers.get("content-length")
                        if declared and declared.isdigit() and int(declared) > max_bytes:
                            raise FetchError("The page is too large")
                        body = bytearray()
                        async for chunk in response.aiter_bytes():
                            body.extend(chunk)
                            if len(body) > max_bytes:
                                raise FetchError("The page is too large")
                        return FetchResult(current, response.status_code, kind, bytes(body))
                raise FetchError("The site redirected too many times")
    except TimeoutError as exc:
        raise FetchError("The site took too long to answer") from exc
    except httpx.HTTPError as exc:
        raise FetchError(f"Could not fetch the page ({type(exc).__name__})") from exc
