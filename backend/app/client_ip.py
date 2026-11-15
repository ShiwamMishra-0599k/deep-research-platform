"""Resolve the real client IP without trusting spoofable headers.

`X-Forwarded-For` is attacker-controlled unless it was appended by a proxy we operate. Rules:

* The header is read only when the TCP peer is inside TRUSTED_PROXY_CIDRS (default: none).
* The chain is walked from the RIGHT (nearest hop first); every hop that is itself a trusted proxy
  is skipped and the first untrusted address is the client. Entries to the left of it are
  attacker-supplied and ignored, so prepending fake addresses cannot change the result.
* A malformed header is ignored entirely (the peer address is used).
* IPv6 clients are bucketed by /64, because one subscriber commonly controls an entire /64 and
  rotating addresses inside it must not reset a rate limit. IPv4-mapped IPv6 becomes plain IPv4.
"""
import ipaddress
from functools import lru_cache

from fastapi import Request

from .config import settings

_MAX_HOPS = 10


@lru_cache(maxsize=16)
def _parse_cidrs(raw: str) -> tuple:
    nets = []
    for part in raw.split(","):
        part = part.strip()
        if part:
            nets.append(ipaddress.ip_network(part, strict=False))
    return tuple(nets)


def _trusted(ip: ipaddress._BaseAddress, nets: tuple) -> bool:
    return any(ip.version == n.version and ip in n for n in nets)


def _parse_ip(text: str):
    text = text.strip()
    if text.startswith("[") and "]" in text:  # [::1]:1234
        text = text[1 : text.index("]")]
    elif text.count(":") == 1:  # 1.2.3.4:5678
        text = text.split(":")[0]
    ip = ipaddress.ip_address(text)
    if ip.version == 6 and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return ip


def normalize(ip: ipaddress._BaseAddress) -> str:
    if ip.version == 6:
        return str(ipaddress.ip_network(f"{ip}/64", strict=False))
    return str(ip)


def resolve_client_ip(peer: str | None, forwarded_for: str | None, trusted_cidrs: str | None = None) -> str:
    """Pure function (unit-testable) behind `client_ip`."""
    if not peer:
        return "unknown"
    try:
        peer_ip = _parse_ip(peer)
    except ValueError:
        return "unknown"
    nets = _parse_cidrs(settings.trusted_proxy_cidrs if trusted_cidrs is None else trusted_cidrs)
    if not nets or not forwarded_for or not _trusted(peer_ip, nets):
        return normalize(peer_ip)

    hops = [h for h in (x.strip() for x in forwarded_for.split(",")) if h]
    if not hops or len(hops) > _MAX_HOPS:
        return normalize(peer_ip)
    try:
        parsed = [_parse_ip(h) for h in hops]
    except ValueError:
        return normalize(peer_ip)  # malformed chain: do not guess
    for ip in reversed(parsed):
        if not _trusted(ip, nets):
            return normalize(ip)
    return normalize(parsed[0])  # every hop was one of our proxies


def client_ip(request: Request) -> str:
    peer = request.client.host if request.client else None
    return resolve_client_ip(peer, request.headers.get("x-forwarded-for"))
