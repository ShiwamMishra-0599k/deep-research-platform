"""Shared fixtures. No test touches the real internet or a paid LLM.

Environment is pinned BEFORE the package is imported so a developer's real .env keys
can never leak into tests.
"""
import os

for _k in ("OPENROUTER_API_KEY", "OPENAI_API_KEY", "EXA_API_KEY", "TAVILY_API_KEY", "JINA_API_KEY", "AGENT_API_KEY"):
    os.environ[_k] = ""
os.environ["REDIS_URL"] = "redis://127.0.0.1:56379"  # isolated ephemeral test Redis
os.environ["LOG_LEVEL"] = "WARNING"

import httpx  # noqa: E402
import pytest  # noqa: E402

from research_agent.config import get_settings  # noqa: E402


@pytest.fixture(autouse=True)
def block_external_network(monkeypatch):
    """Fail loudly if any test tries to reach a non-loopback address (no real internet)."""
    import ipaddress
    import socket

    real_connect = socket.socket.connect

    def guarded(self, address):
        host = address[0] if isinstance(address, tuple) else address
        try:
            ok = ipaddress.ip_address(host).is_loopback
        except ValueError:
            ok = False
        if not ok:
            raise RuntimeError(f"real network access blocked in tests: {address!r}")
        return real_connect(self, address)

    monkeypatch.setattr(socket.socket, "connect", guarded)


class MemCache:
    """In-memory stand-in for the Redis cache, so tool tests are hermetic."""

    def __init__(self):
        self.store: dict[str, str] = {}
        self.sets: list[tuple[str, str, int]] = []

    async def get(self, key):
        return self.store.get(key)

    async def set(self, key, value, ttl):
        self.sets.append((key, value, ttl))
        self.store[key] = value


@pytest.fixture(autouse=True)
def mem_cache(request, monkeypatch):
    """Patch the tools' cache with a dict unless the test asks for real Redis."""
    if "real_redis" in request.fixturenames or "no_cache_patch" in request.fixturenames:
        yield None
        return
    from research_agent.agent import tools

    cache = MemCache()
    monkeypatch.setattr(tools, "cache_get", cache.get)
    monkeypatch.setattr(tools, "cache_set", cache.set)
    yield cache


@pytest.fixture
def no_cache_patch():
    return True


@pytest.fixture
def settings():
    return get_settings()


@pytest.fixture
def install_http(monkeypatch):
    """Route the tools' fixed-API HTTP client through a MockTransport handler."""

    def _install(handler):
        from research_agent.agent import tools

        def _client(timeout: float = 15.0):
            return httpx.AsyncClient(
                transport=httpx.MockTransport(handler), timeout=timeout, follow_redirects=True
            )

        monkeypatch.setattr(tools, "_client", _client)

    return _install


@pytest.fixture
async def real_redis(monkeypatch):
    import research_agent.infrastructure.redis_client as rc

    monkeypatch.setattr(get_settings(), "REDIS_URL", "redis://127.0.0.1:56379")
    ok = await rc.connect_redis()
    if not ok:
        pytest.skip("test Redis (127.0.0.1:56379) not reachable")
    await rc.get_redis().flushdb()  # isolated ephemeral test instance only
    yield rc
    try:
        await rc.get_redis().flushdb()
    finally:
        await rc.disconnect_redis()
