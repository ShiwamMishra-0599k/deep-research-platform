"""Client-IP resolution: X-Forwarded-For is trusted only from configured proxies, and only from the right."""
import pytest

from app.client_ip import resolve_client_ip

PROXIES = "10.0.0.0/8, 192.168.0.0/16, fd00::/8"


@pytest.mark.parametrize(
    "peer,xff,trusted,expected",
    [
        # no trusted proxies configured: the header is pure attacker input and is ignored
        ("203.0.113.5", "1.1.1.1", "", "203.0.113.5"),
        ("203.0.113.5", "1.1.1.1, 2.2.2.2", "", "203.0.113.5"),
        # peer is not a trusted proxy: header ignored even though proxies exist
        ("203.0.113.5", "1.1.1.1", PROXIES, "203.0.113.5"),
        # trusted proxy, single hop
        ("10.0.0.7", "198.51.100.9", PROXIES, "198.51.100.9"),
        # chain of our own proxies is skipped; first untrusted hop from the right is the client
        ("10.0.0.7", "198.51.100.9, 10.1.1.1, 192.168.5.5", PROXIES, "198.51.100.9"),
        # spoofed prefix: attacker prepends fake addresses; only the right-most untrusted counts
        ("10.0.0.7", "9.9.9.9, 198.51.100.9", PROXIES, "198.51.100.9"),
        ("10.0.0.7", "127.0.0.1, 8.8.8.8, 198.51.100.9", PROXIES, "198.51.100.9"),
        # every hop is one of ours: fall back to the left-most
        ("10.0.0.7", "10.2.2.2, 10.3.3.3", PROXIES, "10.2.2.2"),
        # missing / empty / malformed / oversized header: use the peer
        ("10.0.0.7", None, PROXIES, "10.0.0.7"),
        ("10.0.0.7", "", PROXIES, "10.0.0.7"),
        ("10.0.0.7", "not-an-ip", PROXIES, "10.0.0.7"),
        ("10.0.0.7", "1.1.1.1, <script>", PROXIES, "10.0.0.7"),
        ("10.0.0.7", ", ".join(f"1.1.1.{i}" for i in range(1, 12)), PROXIES, "10.0.0.7"),
        # ports and brackets are tolerated
        ("10.0.0.7", "198.51.100.9:54321", PROXIES, "198.51.100.9"),
        ("10.0.0.7", "[2001:db8:1:2:3:4:5:6]:443", PROXIES, "2001:db8:1:2::/64"),
        # IPv6 clients share a /64 bucket; different /64s differ; IPv4-mapped becomes IPv4
        ("2001:db8:aaaa:bbbb:1:2:3:4", None, "", "2001:db8:aaaa:bbbb::/64"),
        ("2001:db8:aaaa:bbbb:ffff:ffff:ffff:ffff", None, "", "2001:db8:aaaa:bbbb::/64"),
        ("::ffff:203.0.113.5", None, "", "203.0.113.5"),
        # unusable peer
        (None, "1.1.1.1", PROXIES, "unknown"),
        ("testclient", None, "", "unknown"),
    ],
)
def test_resolve_client_ip(peer, xff, trusted, expected):
    assert resolve_client_ip(peer, xff, trusted) == expected


def test_ipv6_addresses_in_one_slash64_cannot_rotate_to_dodge_a_limit():
    a = resolve_client_ip("2001:db8:1:1::1", None, "")
    b = resolve_client_ip("2001:db8:1:1:dead:beef:0:7", None, "")
    c = resolve_client_ip("2001:db8:1:2::1", None, "")
    assert a == b and a != c


def test_ipv4_and_ipv6_trusted_ranges_do_not_cross_match():
    # an IPv6 peer must not be "trusted" by an IPv4 CIDR (no cross-family surprises)
    assert resolve_client_ip("::1", "1.1.1.1", "10.0.0.0/8") == "::1/128" or resolve_client_ip("::1", "1.1.1.1", "10.0.0.0/8").startswith("::")
