"""Redis cache client.

Redis is an optimisation, never a dependency: a failed connection at startup or a
failure mid-request must not crash or hang the service. All helpers degrade to
"cache miss" / "not stored".
"""
import asyncio
import logging
import time

import redis.asyncio as aioredis

from research_agent.config import get_settings

logger = logging.getLogger(__name__)

_redis: aioredis.Redis | None = None
_last_attempt: float = 0.0
_RETRY_INTERVAL = 10.0  # seconds between lazy reconnect attempts


def _build() -> aioredis.Redis:
    settings = get_settings()
    t = settings.REDIS_SOCKET_TIMEOUT
    return aioredis.from_url(
        settings.REDIS_URL,
        encoding="utf-8",
        decode_responses=True,
        socket_connect_timeout=t,
        socket_timeout=t,
        health_check_interval=30,
    )


async def connect_redis() -> bool:
    """Try to connect. Never raises; returns True when Redis answered a ping."""
    global _redis, _last_attempt
    _last_attempt = time.monotonic()
    try:
        client = _build()
        await asyncio.wait_for(client.ping(), timeout=get_settings().REDIS_SOCKET_TIMEOUT + 1)
        _redis = client
        logger.info("Redis connected")
        return True
    except Exception as e:
        _redis = None
        logger.warning("Redis unavailable (%s); running without cache", type(e).__name__)
        return False


async def disconnect_redis() -> None:
    global _redis
    client, _redis = _redis, None
    if client is not None:
        try:
            await client.aclose()
        except Exception:
            logger.debug("Redis close failed", exc_info=True)


def get_redis() -> aioredis.Redis:
    if _redis is None:
        raise RuntimeError("Redis client not connected")
    return _redis


async def redis_ping() -> bool:
    """True when Redis currently answers. Never raises."""
    try:
        await asyncio.wait_for(get_redis().ping(), timeout=get_settings().REDIS_SOCKET_TIMEOUT + 1)
        return True
    except Exception:
        return False


async def _ensure() -> aioredis.Redis | None:
    """Return a client, lazily retrying the connection at most every few seconds."""
    if _redis is not None:
        return _redis
    if time.monotonic() - _last_attempt < _RETRY_INTERVAL:
        return None
    await connect_redis()
    return _redis


# ── Tool result cache helpers ─────────────────────────────────

async def cache_get(key: str) -> str | None:
    """Return a cached string, or None on miss / failure / non-string value."""
    client = await _ensure()
    if client is None:
        return None
    try:
        value = await asyncio.wait_for(client.get(key), timeout=get_settings().REDIS_SOCKET_TIMEOUT + 1)
    except Exception as e:
        logger.warning("Redis get failed (%s)", type(e).__name__)
        return None
    return value if isinstance(value, str) and value else None


async def cache_set(key: str, value: str, ttl: int) -> None:
    if not isinstance(value, str) or not value:
        return
    client = await _ensure()
    if client is None:
        return
    try:
        await asyncio.wait_for(client.set(key, value, ex=ttl), timeout=get_settings().REDIS_SOCKET_TIMEOUT + 1)
    except Exception as e:
        logger.warning("Redis set failed (%s)", type(e).__name__)
