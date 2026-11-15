"""SSRF-safe URL validation and fetching.

Defences:
- http/https only, no userinfo, port allowlist
- DNS is resolved by us; every resolved address must be public
- the connection is made to the validated IP (Host header + SNI preserved), which
  defeats DNS rebinding between check and use
- redirects are followed manually and re-validated per hop
- the body is streamed with a hard byte cap and an overall deadline
"""
from __future__ import annotations

import asyncio
import ipaddress
import logging
import socket
import time
from dataclasses import dataclass
from urllib.parse import urljoin, urlsplit

import httpx

logger = logging.getLogger(__name__)

ALLOWED_PORTS = {80, 443, 8080, 8443}
MAX_REDIRECTS = 3
MAX_BYTES = 5 * 1024 * 1024
TOTAL_TIMEOUT = 20.0

_BLOCKED_HOSTNAMES = {
    "localhost",
    "metadata.google.internal",
    "metadata",
    "instance-data",
}
_BLOCKED_SUFFIXES = (".localhost", ".local", ".internal", ".localdomain", ".home.arpa")
_METADATA_IPS = {
    ipaddress.ip_address("169.254.169.254"),
    ipaddress.ip_address("169.254.170.2"),
    ipaddress.ip_address("fd00:ec2::254"),
    ipaddress.ip_address("100.100.100.200"),
}
_CGNAT = ipaddress.ip_network("100.64.0.0/10")


class UnsafeURLError(ValueError):
    """The URL (or a redirect target) is not allowed to be fetched."""


class FetchError(RuntimeError):
    """A safe URL could not be fetched (network, size, status, type)."""


def _normalize_ip(ip):
    if isinstance(ip, ipaddress.IPv6Address):
        if ip.ipv4_mapped is not None:
            return ip.ipv4_mapped
        if ip.sixtofour is not None:
            return ip.sixtofour
        teredo = ip.teredo
        if teredo is not None:
            return teredo[1]
    return ip


def is_public_ip(ip) -> bool:
    ip = _normalize_ip(ip)
    if ip in _METADATA_IPS:
        return False
    if isinstance(ip, ipaddress.IPv4Address) and ip in _CGNAT:
        return False
    return not (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
        or not ip.is_global
    )


def _parse_ip_literal(host: str):
    """Parse IP literals incl. decimal/octal/hex/short forms that inet_aton accepts."""
    h = host.strip("[]")
    try:
        return ipaddress.ip_address(h)
    except ValueError:
        pass
    try:
        # inet_aton accepts 2130706433, 0x7f.1, 0177.0.0.1, 127.1 ...
        return ipaddress.IPv4Address(socket.inet_aton(h))
    except OSError:
        return None


def validate_url_syntax(url: str) -> tuple[str, str, int]:
    """Return (scheme, hostname, port) or raise UnsafeURLError. No network."""
    if not isinstance(url, str) or not url or len(url) > 2048:
        raise UnsafeURLError("invalid url")
    if any(ord(c) < 33 or ord(c) == 127 for c in url):
        raise UnsafeURLError("invalid characters in url")
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError as e:
        raise UnsafeURLError("invalid url") from e
    if parts.scheme not in ("http", "https"):
        raise UnsafeURLError("only http and https are allowed")
    if parts.username is not None or parts.password is not None or "@" in parts.netloc:
        raise UnsafeURLError("credentials in url are not allowed")
    host = (parts.hostname or "").rstrip(".").lower()
    if not host:
        raise UnsafeURLError("missing host")
    port = port or (443 if parts.scheme == "https" else 80)
    if port not in ALLOWED_PORTS:
        raise UnsafeURLError("port not allowed")
    if host in _BLOCKED_HOSTNAMES or host.endswith(_BLOCKED_SUFFIXES):
        raise UnsafeURLError("host not allowed")
    ip = _parse_ip_literal(host)
    if ip is not None and not is_public_ip(ip):
        raise UnsafeURLError("address not allowed")
    return parts.scheme, host, port


async def resolve_public_ips(host: str, port: int) -> list[str]:
    """Resolve host and require EVERY address to be public. Returns the IP strings."""
    literal = _parse_ip_literal(host)
    if literal is not None:
        if not is_public_ip(literal):
            raise UnsafeURLError("address not allowed")
        return [str(literal)]
    loop = asyncio.get_running_loop()
    try:
        infos = await loop.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except OSError as e:
        raise FetchError("dns resolution failed") from e
    ips: list[str] = []
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if not is_public_ip(ip):
            raise UnsafeURLError("address not allowed")
        if str(ip) not in ips:
            ips.append(str(ip))
    if not ips:
        raise FetchError("dns resolution failed")
    return ips


async def assert_safe_url(url: str) -> None:
    """Validate syntax and DNS (used before handing a URL to a third-party reader)."""
    _, host, port = validate_url_syntax(url)
    await resolve_public_ips(host, port)


@dataclass
class FetchResult:
    url: str
    status: int
    content_type: str
    body: bytes


async def safe_fetch(
    url: str,
    *,
    max_bytes: int = MAX_BYTES,
    timeout: float = TOTAL_TIMEOUT,
    max_redirects: int = MAX_REDIRECTS,
    transport: httpx.AsyncBaseTransport | None = None,
    headers: dict[str, str] | None = None,
) -> FetchResult:
    """GET url with SSRF protections. Raises UnsafeURLError / FetchError."""
    deadline = time.monotonic() + timeout
    current = url
    for _hop in range(max_redirects + 1):
        scheme, host, port = validate_url_syntax(current)
        ips = await resolve_public_ips(host, port)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise FetchError("timeout")
        result = await _single_request(
            current, scheme, host, port, ips[0], remaining, max_bytes, transport, headers
        )
        if isinstance(result, str):  # redirect target
            current = urljoin(current, result)
            continue
        return result
    raise FetchError("too many redirects")


async def _single_request(
    url: str,
    scheme: str,
    host: str,
    port: int,
    ip: str,
    timeout: float,
    max_bytes: int,
    transport: httpx.AsyncBaseTransport | None,
    extra_headers: dict[str, str] | None,
) -> FetchResult | str:
    parts = urlsplit(url)
    ip_host = f"[{ip}]" if ":" in ip else ip
    default_port = 443 if scheme == "https" else 80
    netloc = ip_host if port == default_port else f"{ip_host}:{port}"
    target = f"{scheme}://{netloc}{parts.path or '/'}"
    if parts.query:
        target += f"?{parts.query}"
    host_header = host if port == default_port else f"{host}:{port}"
    headers = {"Host": host_header, "User-Agent": "LumenResearchAgent/1.0", **(extra_headers or {})}
    extensions = {"sni_hostname": host} if scheme == "https" else {}

    async def _run() -> FetchResult | str:
        async with httpx.AsyncClient(
            follow_redirects=False,
            timeout=httpx.Timeout(timeout, connect=min(10.0, timeout)),
            transport=transport,
            verify=True,
            trust_env=False,
        ) as client:
            async with client.stream("GET", target, headers=headers, extensions=extensions) as resp:
                if resp.status_code in (301, 302, 303, 307, 308):
                    loc = resp.headers.get("location")
                    if not loc:
                        raise FetchError("redirect without location")
                    return loc
                if resp.status_code >= 400:
                    raise FetchError(f"upstream status {resp.status_code}")
                declared = resp.headers.get("content-length")
                if declared and declared.isdigit() and int(declared) > max_bytes:
                    raise FetchError("response too large")
                buf = bytearray()
                async for chunk in resp.aiter_bytes():
                    buf.extend(chunk)
                    if len(buf) > max_bytes:
                        raise FetchError("response too large")
                return FetchResult(
                    url=url,
                    status=resp.status_code,
                    content_type=resp.headers.get("content-type", "").split(";")[0].strip().lower(),
                    body=bytes(buf),
                )

    try:
        return await asyncio.wait_for(_run(), timeout=timeout)
    except TimeoutError as e:
        raise FetchError("timeout") from e
    except httpx.HTTPError as e:
        logger.warning("fetch failed for %s: %s", host, type(e).__name__)
        raise FetchError("network error") from e
