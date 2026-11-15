"""Rate limiting as users and attackers experience it: HTTP status codes, Retry-After, proxies, outages."""
import asyncio
import json
import uuid

import httpx
import pytest
import redis

from app import deps, ratelimit
from app.config import settings
from app.main import app
from app.ratelimit import ConcurrencyGuard, RedisBackend, SlidingWindowLimiter
from app.research_client import AgentEvent
from app.routers import auth as auth_router
from app.routers import research as research_router


def _n() -> str:
    """Unique limiter name per use: Redis state is shared and persists across tests and runs (by design)."""
    return "h-" + uuid.uuid4().hex[:10]


DEAD = "redis://127.0.0.1:1/0"
PW = "correct-horse-battery"


@pytest.fixture()
def dead_redis():
    """Every limiter now sees an unreachable Redis (breaker already open)."""
    be = RedisBackend(DEAD, 0.2, 60, settings.rate_limit_prefix)
    previous = ratelimit.set_backend(be)
    yield be
    ratelimit.set_backend(previous)


def _login(c, email="nobody@example.com", pw="wrong-wrong-1"):
    return c.post("/api/auth/login", json={"email": email, "password": pw})


# ── 429 + Retry-After on every guarded endpoint ─────────────────────────────

def test_login_429_has_retry_after_and_is_shared_state(client, monkeypatch):
    monkeypatch.setattr(auth_router, "login_ip_limiter", SlidingWindowLimiter(_n(), 3, 60))
    codes = [_login(client, f"u{i}@example.com").status_code for i in range(5)]
    assert codes == [401, 401, 401, 429, 429]
    r = _login(client, "x@example.com")
    assert r.status_code == 429 and 1 <= int(r.headers["Retry-After"]) <= 60


def test_register_429_has_retry_after(client, monkeypatch):
    monkeypatch.setattr(auth_router, "register_ip_limiter", SlidingWindowLimiter(_n(), 1, 3600))
    body = {"password": PW, "name": "R"}
    assert client.post("/api/auth/register", json={"email": "r1@example.com", **body}).status_code == 200
    r = client.post("/api/auth/register", json={"email": "r2@example.com", **body})
    assert r.status_code == 429 and int(r.headers["Retry-After"]) >= 1


def test_per_user_api_limit_covers_every_authenticated_route(client, alice, bob, monkeypatch):
    monkeypatch.setattr(deps, "api_user_limiter", SlidingWindowLimiter(_n(), 3, 60, strict=False))
    assert [client.get("/api/conversations", headers=alice["headers"]).status_code for _ in range(3)] == [200] * 3
    r = client.get("/api/auth/me", headers=alice["headers"])  # a different route, same user budget
    assert r.status_code == 429 and int(r.headers["Retry-After"]) >= 1
    assert client.get("/api/conversations", headers=bob["headers"]).status_code == 200  # per user, not global


def test_api_limit_is_not_consumed_by_unauthenticated_or_invalid_requests(client, alice, monkeypatch):
    monkeypatch.setattr(deps, "api_user_limiter", SlidingWindowLimiter(_n(), 2, 60, strict=False))
    for _ in range(5):
        assert client.get("/api/conversations").status_code == 401
        assert client.get("/api/conversations", headers={"Authorization": "Bearer junk"}).status_code == 401
    assert client.get("/api/conversations", headers=alice["headers"]).status_code == 200


def test_global_stream_capacity_returns_429_and_cleans_up(client, alice, monkeypatch, engine_rows):
    cap = ConcurrencyGuard(_n(), 1, ttl=60)
    monkeypatch.setattr(research_router, "global_stream_guard", cap)
    held = cap.acquire("all")  # another user's stream is occupying the only global slot
    cid = client.post("/api/conversations", json={}, headers=alice["headers"]).json()["id"]
    r = client.post(f"/api/conversations/{cid}/query", json={"query": "hi"}, headers=alice["headers"])
    assert r.status_code == 429 and "capacity" in r.json()["detail"] and int(r.headers["Retry-After"]) >= 1
    assert research_router.stream_guard.active(alice["id"]) == 0  # the per-user slot taken first was returned
    assert engine_rows("SELECT count(*) FROM messages")[0][0] == 0  # rejected before any write
    cap.release(held)


# ── concurrent HTTP bursts (the limit holds, exactly) ───────────────────────

async def test_concurrent_login_burst_is_limited_exactly(client, monkeypatch):
    monkeypatch.setattr(auth_router, "login_ip_limiter", SlidingWindowLimiter(_n(), 5, 60))
    ratelimit.get_backend().warm()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as ac:
        rs = await asyncio.gather(*[
            ac.post("/api/auth/login", json={"email": f"b{i}@example.com", "password": "wrong-wrong-1"}) for i in range(40)
        ])
    codes = [r.status_code for r in rs]
    assert codes.count(429) == 35 and codes.count(401) == 5, codes


async def test_concurrent_research_requests_admit_exactly_the_per_user_limit(client, alice, monkeypatch):
    monkeypatch.setattr(research_router, "query_limiter", SlidingWindowLimiter(_n(), 6, 60))
    monkeypatch.setattr(research_router, "stream_guard", ConcurrencyGuard(_n(), 100, ttl=60))

    def fake(query, history):
        async def gen():
            yield AgentEvent("blocks", json.dumps({"status": "completed", "data": {"blocks": [{"template_id": "markdown", "data": {"content": "ok"}}]}}))
            yield AgentEvent("done", "{}")
        return gen()

    monkeypatch.setattr(research_router, "stream_research", fake)
    ratelimit.get_backend().warm()
    cid = client.post("/api/conversations", json={}, headers=alice["headers"]).json()["id"]
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as ac:
        rs = await asyncio.gather(*[
            ac.post(f"/api/conversations/{cid}/query", json={"query": f"q{i}"}, headers=alice["headers"]) for i in range(30)
        ])
    codes = [r.status_code for r in rs]
    assert codes.count(200) == 6 and codes.count(429) == 24, codes  # paid operations: never one over the limit


# ── Redis outage behaviour over HTTP ────────────────────────────────────────

def test_redis_down_default_mode_login_and_research_stay_limited(client, alice, dead_redis, monkeypatch):
    assert settings.rate_limit_fail_mode == "local"
    monkeypatch.setattr(auth_router, "login_ip_limiter", SlidingWindowLimiter(_n(), 3, 60))
    assert [_login(client, f"d{i}@example.com").status_code for i in range(5)] == [401, 401, 401, 429, 429]

    monkeypatch.setattr(research_router, "query_limiter", SlidingWindowLimiter(_n(), 2, 60))
    monkeypatch.setattr(research_router, "stream_research", lambda q, h: _empty_stream())
    cid = client.post("/api/conversations", json={}, headers=alice["headers"]).json()["id"]
    codes = [client.post(f"/api/conversations/{cid}/query", json={"query": "x"}, headers=alice["headers"]).status_code for _ in range(4)]
    assert codes[:2] == [200, 200] and codes[2:] == [429, 429]  # NOT unrestricted while Redis is offline


def _empty_stream():
    async def gen():
        if False:
            yield None
    return gen()


def test_redis_down_fail_closed_refuses_login_register_and_research(client, alice, dead_redis, monkeypatch, engine_rows):
    monkeypatch.setattr(settings, "rate_limit_fail_mode", "closed")
    for r in (
        _login(client),
        client.post("/api/auth/register", json={"email": "c@example.com", "password": PW, "name": "C"}),
    ):
        assert r.status_code == 503 and r.json() == {"detail": "Service temporarily unavailable"} and r.headers["Retry-After"] == "5"
    cid_resp = client.post("/api/conversations", json={}, headers=alice["headers"])
    assert cid_resp.status_code == 200  # ordinary API traffic keeps working (generic limiter is non-strict)
    r = client.post(f"/api/conversations/{cid_resp.json()['id']}/query", json={"query": "x"}, headers=alice["headers"])
    assert r.status_code == 503
    assert engine_rows("SELECT count(*) FROM messages")[0][0] == 0  # the paid operation never started


def test_redis_down_never_takes_ordinary_api_traffic_down(client, alice, dead_redis, monkeypatch):
    monkeypatch.setattr(settings, "rate_limit_fail_mode", "closed")
    assert client.get("/api/conversations", headers=alice["headers"]).status_code == 200
    assert client.get("/api/auth/me", headers=alice["headers"]).status_code == 200


def test_readiness_reports_rate_limit_state_without_failing_readiness(client, dead_redis):
    r = client.get("/health/ready")
    dead_redis._down_until = 0
    _login(client)  # discovers the outage
    r = client.get("/health/ready")
    assert r.status_code == 200 and r.json()["rate_limit"] == "degraded-local"


def test_readiness_reports_redis_when_healthy(client):
    assert client.get("/health/ready").json()["rate_limit"] == "redis"


def test_service_recovers_to_redis_when_it_comes_back(client, monkeypatch):
    be = ratelimit.get_backend()
    real = be._get_client
    state = {"down": True}

    def flaky():
        if state["down"]:
            raise redis.exceptions.ConnectionError("simulated")
        return real()

    monkeypatch.setattr(be, "_get_client", flaky)
    monkeypatch.setattr(be, "retry_seconds", 0.2)
    _login(client)
    assert client.get("/health/ready").json()["rate_limit"] == "degraded-local"
    state["down"] = False
    import time

    time.sleep(0.3)
    _login(client)
    assert client.get("/health/ready").json()["rate_limit"] == "redis"


# ── proxies and X-Forwarded-For ─────────────────────────────────────────────

def _via(peer_ip: str):
    """An ASGI client whose TCP peer address is `peer_ip` (what a proxy or attacker would present)."""
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app, client=(peer_ip, 4000)), base_url="http://t")


async def _bad_login(ac, n, xff=None):
    headers = {"X-Forwarded-For": xff} if xff else {}
    r = await ac.post("/api/auth/login", json={"email": f"x{n}@example.com", "password": "wrong-wrong-1"}, headers=headers)
    return r.status_code


async def test_spoofing_x_forwarded_for_cannot_evade_the_login_limit(monkeypatch):
    monkeypatch.setattr(settings, "trusted_proxy_cidrs", "")
    monkeypatch.setattr(auth_router, "login_ip_limiter", SlidingWindowLimiter(_n(), 3, 60))
    async with _via("203.0.113.9") as ac:
        codes = [await _bad_login(ac, i, f"198.51.100.{i}") for i in range(6)]
    assert codes == [401, 401, 401, 429, 429, 429]  # rotating the header changed nothing


async def test_behind_a_trusted_proxy_each_real_client_gets_its_own_bucket(monkeypatch):
    monkeypatch.setattr(settings, "trusted_proxy_cidrs", "10.0.0.0/8")
    monkeypatch.setattr(auth_router, "login_ip_limiter", SlidingWindowLimiter(_n(), 2, 60))
    async with _via("10.0.0.5") as proxy:
        assert [await _bad_login(proxy, f"a{i}", "198.51.100.1") for i in range(3)] == [401, 401, 429]
        assert [await _bad_login(proxy, f"b{i}", "198.51.100.2") for i in range(3)] == [401, 401, 429]  # unaffected
        # attacker prepends a fake hop; the right-most untrusted address is still 198.51.100.1
        assert await _bad_login(proxy, "z", "9.9.9.9, 198.51.100.1") == 429


async def test_a_peer_outside_the_trusted_range_cannot_use_the_header_even_if_proxies_exist(monkeypatch):
    monkeypatch.setattr(settings, "trusted_proxy_cidrs", "10.0.0.0/8")
    monkeypatch.setattr(auth_router, "login_ip_limiter", SlidingWindowLimiter(_n(), 2, 60))
    async with _via("203.0.113.9") as attacker:  # not one of our proxies
        codes = [await _bad_login(attacker, i, f"198.51.100.{i}") for i in range(4)]
    assert codes == [401, 401, 429, 429]


async def test_behind_an_untrusted_proxy_all_users_share_one_bucket(monkeypatch):
    """Safe default: without TRUSTED_PROXY_CIDRS the proxy's own address is the key (strict, never spoofable)."""
    monkeypatch.setattr(settings, "trusted_proxy_cidrs", "")
    monkeypatch.setattr(auth_router, "login_ip_limiter", SlidingWindowLimiter(_n(), 2, 60))
    async with _via("10.0.0.5") as proxy:
        codes = [await _bad_login(proxy, i, f"198.51.100.{i}") for i in range(4)]
    assert codes == [401, 401, 429, 429]
