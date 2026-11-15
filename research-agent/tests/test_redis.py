"""Redis behaviour: real hits/misses, never caching failures, and survival when Redis is down."""
import asyncio
import time

import httpx
import pytest

import research_agent.infrastructure.redis_client as rc
from research_agent.agent import tools

# ── with real Redis (isolated test instance) ─────────────────


async def test_roundtrip_ttl_and_miss(real_redis):
    assert await rc.cache_get("k") is None
    await rc.cache_set("k", "v", 60)
    assert await rc.cache_get("k") == "v"
    assert 0 < await rc.get_redis().ttl("k") <= 60


async def test_non_string_and_empty_values_are_not_stored_or_served(real_redis):
    await rc.cache_set("a", "", 60)
    await rc.cache_set("b", None, 60)      # type: ignore[arg-type]
    await rc.cache_set("c", {"x": 1}, 60)  # type: ignore[arg-type]
    assert await rc.get_redis().dbsize() == 0
    await rc.get_redis().set("e", "")
    assert await rc.cache_get("e") is None


async def test_tool_cache_hit_and_errors_not_cached_against_real_redis(real_redis, install_http, no_cache_patch):
    calls = []

    def ok(request):
        calls.append(1)
        return httpx.Response(200, json={"bitcoin": {"inr": 1.5, "usd": 2.5, "usd_24h_change": 1.0}})

    install_http(ok)
    a = await tools.get_crypto_price.ainvoke({"coin_id": "bitcoin"})
    b = await tools.get_crypto_price.ainvoke({"coin_id": "bitcoin"})
    assert a == b and len(calls) == 1
    assert await real_redis.get_redis().exists("research:crypto:bitcoin") == 1

    await real_redis.get_redis().flushdb()
    install_http(lambda r: httpx.Response(500))
    out = await tools.get_crypto_price.ainvoke({"coin_id": "bitcoin"})
    assert out.startswith("Error:")
    assert await real_redis.get_redis().dbsize() == 0          # failure left no cache entry


async def test_cached_untrusted_content_is_raw_and_wrapped_on_read(real_redis, monkeypatch, no_cache_patch):
    async def ddg(q):
        return [("t", "s", "https://e.com/p")]

    monkeypatch.setattr(tools, "_search_ddg", ddg)
    first = await tools.web_search.ainvoke({"query": "real redis query"})
    keys = await real_redis.get_redis().keys("research:search:*")
    raw = await real_redis.get_redis().get(keys[0])
    assert "<untrusted_content>" not in raw and first.count("<untrusted_content>") == 1
    second = await tools.web_search.ainvoke({"query": "real redis query"})
    assert second == first and second.count("<untrusted_content>") == 1   # wrapped once, not double-wrapped


async def test_poisoned_non_text_cache_entry_is_just_a_miss(real_redis, install_http, no_cache_patch):
    await real_redis.get_redis().set("research:crypto:bitcoin", "")          # empty poison -> miss
    install_http(lambda r: httpx.Response(200, json={"bitcoin": {"inr": 1, "usd": 2, "usd_24h_change": 0}}))
    assert "CRYPTO|bitcoin" in await tools.get_crypto_price.ainvoke({"coin_id": "bitcoin"})


# ── Redis down ───────────────────────────────────────────────


@pytest.fixture
def dead_redis(settings, monkeypatch):
    monkeypatch.setattr(settings, "REDIS_URL", "redis://127.0.0.1:1")    # nothing listens here
    monkeypatch.setattr(settings, "REDIS_SOCKET_TIMEOUT", 0.3)
    monkeypatch.setattr(rc, "_redis", None)
    monkeypatch.setattr(rc, "_last_attempt", 0.0)


async def test_startup_connect_failure_does_not_raise(dead_redis):
    t0 = time.monotonic()
    assert await rc.connect_redis() is False
    assert time.monotonic() - t0 < 5
    assert await rc.redis_ping() is False


async def test_cache_helpers_degrade_to_miss_when_down(dead_redis):
    t0 = time.monotonic()
    assert await rc.cache_get("k") is None
    await rc.cache_set("k", "v", 10)                  # must not raise
    assert time.monotonic() - t0 < 5


async def test_tool_still_works_uncached_when_redis_down(dead_redis, install_http, no_cache_patch):
    install_http(lambda r: httpx.Response(200, json={"bitcoin": {"inr": 1, "usd": 2, "usd_24h_change": 0}}))
    out = await tools.get_crypto_price.ainvoke({"coin_id": "bitcoin"})
    assert out.startswith("CRYPTO|bitcoin")


async def test_lazy_reconnect_is_rate_limited(dead_redis, monkeypatch):
    attempts = []
    real = rc.connect_redis

    async def counting():
        attempts.append(1)
        return await real()

    monkeypatch.setattr(rc, "connect_redis", counting)
    for _ in range(5):
        await rc.cache_get("k")
    assert len(attempts) == 1            # not one reconnect storm per request


async def test_lazy_reconnect_recovers_when_redis_returns(real_redis, monkeypatch):
    await rc.disconnect_redis()
    monkeypatch.setattr(rc, "_last_attempt", 0.0)     # retry window elapsed
    await rc.cache_set("back", "yes", 30)             # triggers the lazy reconnect
    assert await rc.cache_get("back") == "yes"


class _Broken:
    async def get(self, *a, **k):
        raise ConnectionError("lost mid-request")

    async def set(self, *a, **k):
        raise ConnectionError("lost mid-request")

    async def ping(self):
        raise ConnectionError("lost")


class _Hanging:
    async def get(self, *a, **k):
        await asyncio.sleep(60)

    async def set(self, *a, **k):
        await asyncio.sleep(60)


async def test_redis_failing_mid_request_is_a_miss(monkeypatch):
    monkeypatch.setattr(rc, "_redis", _Broken())
    assert await rc.cache_get("k") is None
    await rc.cache_set("k", "v", 10)
    assert await rc.redis_ping() is False


async def test_redis_hanging_mid_request_times_out(settings, monkeypatch):
    monkeypatch.setattr(settings, "REDIS_SOCKET_TIMEOUT", 0.1)
    monkeypatch.setattr(rc, "_redis", _Hanging())
    t0 = time.monotonic()
    assert await rc.cache_get("k") is None
    await rc.cache_set("k", "v", 10)
    assert time.monotonic() - t0 < 3
