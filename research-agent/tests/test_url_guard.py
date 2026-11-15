import asyncio
import ipaddress
import socket

import httpx
import pytest

from research_agent.security import url_guard
from research_agent.security.url_guard import (
    FetchError,
    UnsafeURLError,
    assert_safe_url,
    is_public_ip,
    resolve_public_ips,
    safe_fetch,
    validate_url_syntax,
)

PUBLIC_IP = "93.184.216.34"


def _fake_dns(mapping):
    def fake(host, port, *args, **kwargs):
        ips = mapping.get(host)
        if ips is None:
            raise socket.gaierror("not found")
        return [(socket.AF_INET6 if ":" in ip else socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port)) for ip in ips]

    return fake


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/",
        "http://127.0.0.1:8080/admin",
        "http://localhost/",
        "http://LOCALHOST./",
        "http://foo.localhost/",
        "http://[::1]/",
        "http://[::]/",
        "http://0.0.0.0/",
        "http://2130706433/",          # decimal 127.0.0.1
        "http://0x7f000001/",          # hex 127.0.0.1
        "http://0x7f.0.0.1/",          # mixed hex
        "http://0177.0.0.1/",          # octal 127.0.0.1
        "http://127.1/",               # short form
        "http://[::ffff:127.0.0.1]/",  # IPv4-mapped IPv6 loopback
        "http://[::ffff:10.0.0.1]/",
        "http://169.254.169.254/latest/meta-data/",   # AWS/GCP/Azure metadata
        "http://[fd00:ec2::254]/",                    # AWS IPv6 metadata
        "http://metadata.google.internal/computeMetadata/v1/",
        "http://100.100.100.200/",     # Alibaba metadata (also CGNAT)
        "http://100.64.0.1/",          # CGNAT
        "http://10.0.0.1/",
        "http://172.16.5.5/",
        "http://192.168.1.1/",
        "http://[fe80::1]/",
        "http://[fc00::1]/",
        "http://224.0.0.1/",
        "http://service.internal/",
        "http://printer.local/",
        "http://evil@127.0.0.1/",       # userinfo trick
        "http://user:pw@example.com/",
        "http://example.com@127.0.0.1/",
        "ftp://example.com/",
        "file:///etc/passwd",
        "gopher://example.com/",
        "javascript:alert(1)",
        "data:text/html,<script>alert(1)</script>",
        "http://example.com:22/",
        "http://example.com:6379/",
        "http://example.com:5432/",
        "http://",
        "",
        "http://exa mple.com/",
        "http://example.com/\r\nHost: evil",
    ],
)
def test_unsafe_urls_rejected(url):
    with pytest.raises(UnsafeURLError):
        validate_url_syntax(url)


@pytest.mark.parametrize(
    "url,expected",
    [
        ("https://example.com/a?b=1", ("https", "example.com", 443)),
        ("http://example.com", ("http", "example.com", 80)),
        ("http://example.com:8080/x", ("http", "example.com", 8080)),
        (f"http://{PUBLIC_IP}/", ("http", PUBLIC_IP, 80)),
        ("https://Sub.Example.COM./p", ("https", "sub.example.com", 443)),
    ],
)
def test_safe_urls_accepted(url, expected):
    assert validate_url_syntax(url) == expected


def test_non_string_rejected():
    for bad in (None, 123, b"http://x.com", ["http://x.com"]):
        with pytest.raises(UnsafeURLError):
            validate_url_syntax(bad)  # type: ignore[arg-type]


def test_overlong_url_rejected():
    with pytest.raises(UnsafeURLError):
        validate_url_syntax("http://example.com/" + "a" * 3000)


@pytest.mark.parametrize(
    "ip,public",
    [
        ("8.8.8.8", True), ("93.184.216.34", True), ("2606:4700:4700::1111", True),
        ("127.0.0.1", False), ("10.1.2.3", False), ("169.254.169.254", False),
        ("::1", False), ("::ffff:127.0.0.1", False), ("::ffff:8.8.8.8", True),
        ("100.64.0.1", False), ("240.0.0.1", False), ("192.0.2.1", False),
        ("fd00:ec2::254", False), ("2002:7f00:1::", False),  # 6to4 wrapping 127.0.0.1
    ],
)
def test_is_public_ip(ip, public):
    assert is_public_ip(ipaddress.ip_address(ip)) is public


async def test_dns_all_private_blocked(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", _fake_dns({"intranet.example": ["10.0.0.5"]}))
    with pytest.raises(UnsafeURLError):
        await resolve_public_ips("intranet.example", 443)


async def test_dns_mixed_public_and_private_blocked(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", _fake_dns({"mixed.example": [PUBLIC_IP, "127.0.0.1"]}))
    with pytest.raises(UnsafeURLError):
        await resolve_public_ips("mixed.example", 443)


async def test_dns_public_ok_and_failure_is_fetcherror(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", _fake_dns({"ok.example": [PUBLIC_IP]}))
    assert await resolve_public_ips("ok.example", 443) == [PUBLIC_IP]
    with pytest.raises(FetchError):
        await resolve_public_ips("nope.example", 443)


async def test_assert_safe_url_blocks_resolved_private(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", _fake_dns({"evil.example": ["192.168.0.10"]}))
    with pytest.raises(UnsafeURLError):
        await assert_safe_url("https://evil.example/page")


# ── safe_fetch ───────────────────────────────────────────────


async def test_fetch_connects_to_validated_ip_with_host_and_sni(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", _fake_dns({"example.com": [PUBLIC_IP]}))
    seen = {}

    def handler(request: httpx.Request):
        seen["host"] = request.url.host
        seen["host_header"] = request.headers["host"]
        seen["sni"] = request.extensions.get("sni_hostname")
        seen["path"] = request.url.raw_path
        return httpx.Response(200, headers={"content-type": "text/html; charset=utf-8"}, content=b"<p>hi</p>")

    res = await safe_fetch("https://example.com/a/b?q=1", transport=httpx.MockTransport(handler))
    assert res.status == 200 and res.body == b"<p>hi</p>" and res.content_type == "text/html"
    assert seen["host"] == PUBLIC_IP            # connected to the IP we validated
    assert seen["host_header"] == "example.com"  # but the site still sees its own name
    assert seen["sni"] == "example.com"
    assert seen["path"] == b"/a/b?q=1"


async def test_dns_rebinding_is_defeated(monkeypatch):
    """The name resolves to a public IP at check time and a private IP afterwards.

    We resolve once and connect to that exact IP, so the second answer is never used.
    """
    answers = iter([[PUBLIC_IP], ["127.0.0.1"], ["127.0.0.1"]])
    calls = {"n": 0}

    def rebinding(host, port, *a, **k):
        calls["n"] += 1
        ip = next(answers)[0]
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port))]

    monkeypatch.setattr(socket, "getaddrinfo", rebinding)
    hosts = []

    def handler(request):
        hosts.append(request.url.host)
        return httpx.Response(200, content=b"ok")

    await safe_fetch("http://rebind.example/", transport=httpx.MockTransport(handler))
    assert hosts == [PUBLIC_IP]
    assert calls["n"] == 1


async def test_redirect_to_internal_ip_blocked(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", _fake_dns({"example.com": [PUBLIC_IP]}))

    def handler(request):
        return httpx.Response(302, headers={"location": "http://169.254.169.254/latest/meta-data/"})

    with pytest.raises(UnsafeURLError):
        await safe_fetch("http://example.com/", transport=httpx.MockTransport(handler))


async def test_redirect_to_hostname_resolving_private_blocked(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", _fake_dns({"example.com": [PUBLIC_IP], "sneaky.example": ["10.9.9.9"]}))
    hits = []

    def handler(request):
        hits.append(request.headers["host"])
        return httpx.Response(301, headers={"location": "https://sneaky.example/x"})

    with pytest.raises(UnsafeURLError):
        await safe_fetch("https://example.com/", transport=httpx.MockTransport(handler))
    assert hits == ["example.com"]  # the internal hop was never requested


async def test_relative_redirect_followed_and_revalidated(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", _fake_dns({"example.com": [PUBLIC_IP]}))

    def handler(request):
        if request.url.path == "/start":
            return httpx.Response(302, headers={"location": "/final"})
        return httpx.Response(200, content=b"done")

    res = await safe_fetch("http://example.com/start", transport=httpx.MockTransport(handler))
    assert res.body == b"done"


async def test_redirect_to_non_http_scheme_blocked(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", _fake_dns({"example.com": [PUBLIC_IP]}))

    def handler(request):
        return httpx.Response(302, headers={"location": "file:///etc/passwd"})

    with pytest.raises(UnsafeURLError):
        await safe_fetch("http://example.com/", transport=httpx.MockTransport(handler))


async def test_too_many_redirects(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", _fake_dns({"example.com": [PUBLIC_IP]}))

    def handler(request):
        return httpx.Response(302, headers={"location": "/again"})

    with pytest.raises(FetchError, match="redirect"):
        await safe_fetch("http://example.com/", transport=httpx.MockTransport(handler), max_redirects=3)


async def test_oversized_declared_length_rejected(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", _fake_dns({"example.com": [PUBLIC_IP]}))

    def handler(request):
        return httpx.Response(200, content=b"x" * 5000)

    with pytest.raises(FetchError, match="too large"):
        await safe_fetch("http://example.com/", transport=httpx.MockTransport(handler), max_bytes=1000)


async def test_oversized_streamed_body_without_content_length_rejected(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", _fake_dns({"example.com": [PUBLIC_IP]}))

    async def gen():
        for _ in range(20):
            yield b"x" * 100

    def handler(request):
        return httpx.Response(200, content=gen())

    with pytest.raises(FetchError, match="too large"):
        await safe_fetch("http://example.com/", transport=httpx.MockTransport(handler), max_bytes=1000)


async def test_upstream_error_status(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", _fake_dns({"example.com": [PUBLIC_IP]}))
    with pytest.raises(FetchError, match="500"):
        await safe_fetch("http://example.com/", transport=httpx.MockTransport(lambda r: httpx.Response(500)))


async def test_total_timeout(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", _fake_dns({"example.com": [PUBLIC_IP]}))

    async def slow(request):
        await asyncio.sleep(2)
        return httpx.Response(200, content=b"late")

    with pytest.raises(FetchError, match="timeout"):
        await safe_fetch("http://example.com/", transport=httpx.MockTransport(slow), timeout=0.1)


async def test_private_literal_never_reaches_transport():
    called = []

    def handler(request):
        called.append(1)
        return httpx.Response(200)

    with pytest.raises(UnsafeURLError):
        await safe_fetch("http://127.0.0.1:8080/", transport=httpx.MockTransport(handler))
    assert not called


def test_module_constants_are_sane():
    assert url_guard.MAX_REDIRECTS <= 3
    assert url_guard.MAX_BYTES <= 10 * 1024 * 1024
    assert 443 in url_guard.ALLOWED_PORTS and 22 not in url_guard.ALLOWED_PORTS
