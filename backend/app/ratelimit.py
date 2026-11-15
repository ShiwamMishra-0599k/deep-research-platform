"""Rate limiting and concurrency guards.

Design
------
* **Shared state in Redis** (when REDIS_URL is set) so limits hold across any number of backend
  instances. Every check is ONE Lua script, so check-and-update is atomic: concurrent requests
  cannot both slip under a limit.
* Time comes from the Redis server (`TIME`), never from the application hosts, so clock skew
  between instances cannot weaken a limit.
* **Sliding window log** (ZSET) for request limits: no fixed-window boundary burst. Memory is
  bounded by the limit (<= a few hundred members), every key has a TTL.
* **Keys never contain raw identifiers**: emails, IPs and user ids are HMAC-hashed before they
  reach Redis, and every key is prefixed and namespaced per limiter.
* **Failure policy** (Redis down, slow or hanging): a short circuit breaker stops us hammering a
  dead server, and the guarded operation is NEVER left unlimited:
    - RATE_LIMIT_FAIL_MODE=local (default): the same limits are enforced per process (degraded:
      with N replicas the effective ceiling is N x limit until Redis returns);
    - RATE_LIMIT_FAIL_MODE=closed: the guarded operation is refused with 503.
* With REDIS_URL empty (development, unit tests) only the in-process limiters are used.
"""
import hashlib
import hmac
import logging
import math
import threading
import time
import uuid
from collections import defaultdict, deque
from dataclasses import dataclass

import redis
from redis.exceptions import RedisError

from .config import settings

log = logging.getLogger(__name__)


class RateLimitUnavailable(Exception):
    """Redis is down and RATE_LIMIT_FAIL_MODE=closed: refuse rather than run unlimited."""


class _BackendDown(Exception):
    pass


@dataclass(frozen=True)
class Decision:
    allowed: bool
    retry_after: int = 0  # whole seconds, >= 1 whenever allowed is False


@dataclass(frozen=True)
class Slot:
    """A held concurrency slot. Pass it back to release()."""

    key: str
    token: str
    shared: bool  # True = held in Redis, False = held in this process


# ── Lua scripts (each runs atomically inside Redis) ─────────────────────────

_SLIDING_WINDOW = """
local t = redis.call('TIME')
local now = t[1] * 1000 + math.floor(t[2] / 1000)
local limit, window = tonumber(ARGV[1]), tonumber(ARGV[2])
redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', now - window)
if redis.call('ZCARD', KEYS[1]) >= limit then
  local oldest = redis.call('ZRANGE', KEYS[1], 0, 0, 'WITHSCORES')
  local retry = window - (now - tonumber(oldest[2]))
  if retry < 1 then retry = 1 end
  return {0, retry}
end
redis.call('ZADD', KEYS[1], now, ARGV[3])
redis.call('PEXPIRE', KEYS[1], window)
return {1, 0}
"""

_FAILURE_INCR = """
local n = redis.call('INCR', KEYS[1])
if n == 1 then redis.call('EXPIRE', KEYS[1], tonumber(ARGV[1])) end
return n
"""

_SLOT_ACQUIRE = """
local t = redis.call('TIME')
local now = t[1] * 1000 + math.floor(t[2] / 1000)
redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', now)
if redis.call('ZCARD', KEYS[1]) >= tonumber(ARGV[1]) then return 0 end
redis.call('ZADD', KEYS[1], now + tonumber(ARGV[2]), ARGV[3])
redis.call('PEXPIRE', KEYS[1], tonumber(ARGV[2]))
return 1
"""

_SLOT_COUNT = """
local t = redis.call('TIME')
local now = t[1] * 1000 + math.floor(t[2] / 1000)
return redis.call('ZCOUNT', KEYS[1], '(' .. now, '+inf')
"""


_SCRIPTS = {
    "window": _SLIDING_WINDOW,
    "fail": _FAILURE_INCR,
    "slot": _SLOT_ACQUIRE,
    "slotcount": _SLOT_COUNT,
}


# ── Redis backend with circuit breaker ──────────────────────────────────────

class RedisBackend:
    def __init__(self, url: str, timeout: float, retry_seconds: float, prefix: str) -> None:
        self.url = url
        self.timeout = timeout
        self.retry_seconds = retry_seconds
        self.prefix = prefix
        self._client: redis.Redis | None = None
        self._scripts: dict[str, object] = {}
        self._down_until = 0.0
        self._next_probe = 0.0
        self._lock = threading.Lock()

    @property
    def enabled(self) -> bool:
        return bool(self.url)

    @property
    def available(self) -> bool:
        return self.enabled and time.monotonic() >= self._down_until

    @property
    def usable(self) -> bool:
        """Would a call be attempted right now? True when closed, or when a probe is due. Does NOT consume the probe (run() does)."""
        if not self.enabled:
            return False
        now = time.monotonic()
        return now >= self._down_until or now >= self._next_probe

    def _may_call(self) -> bool:
        """Closed: yes. Open (after a failure): no, except ONE probe call per `redis_probe_seconds` (half-open).

        Without the probe, a Redis stall of under a second kept every guarded request refused (fail-closed) or on the local fallback for the whole
        `retry_seconds`, even though Redis was healthy again after a moment. Measured: a 0.9 s stall caused 4.7 s of refused research requests
        for a single user (perf/load_test.py --fault redis-stall --fail-mode closed --stall-conc 1)."""
        if not self.enabled:
            return False
        now = time.monotonic()
        if now >= self._down_until:
            return True
        if now >= self._next_probe:
            with self._lock:
                if now >= self._next_probe:
                    self._next_probe = now + settings.redis_probe_seconds
                    return True
        return False

    @property
    def status(self) -> str:
        if not self.enabled:
            return "memory"
        return "redis" if self.available else "degraded-local"

    def _get_client(self) -> redis.Redis:
        if self._client is None:
            with self._lock:
                if self._client is None:
                    # Bounded pool: a burst waits briefly for a free connection instead of opening
                    # hundreds of sockets at once (which is what made cold bursts time out).
                    pool = redis.BlockingConnectionPool.from_url(
                        self.url,
                        max_connections=settings.redis_max_connections,
                        timeout=max(self.timeout * 4, 1.0),
                        socket_connect_timeout=self.timeout,
                        socket_timeout=self.timeout,
                        decode_responses=True,
                    )
                    self._client = redis.Redis(connection_pool=pool)
        return self._client

    def _script(self, name: str, source: str):
        if name not in self._scripts:
            self._scripts[name] = self._get_client().register_script(source)
        return self._scripts[name]

    def _trip(self, exc: BaseException) -> None:
        was_up = time.monotonic() >= self._down_until
        self._down_until = time.monotonic() + self.retry_seconds
        self._next_probe = time.monotonic() + settings.redis_probe_seconds
        if was_up:  # log once per outage, never the URL (may carry a password)
            log.warning("Redis rate-limit store unavailable (%s); using the %s fallback for %.0fs",
                        type(exc).__name__, settings.rate_limit_fail_mode, self.retry_seconds)

    def run(self, fn):
        """Run fn(client). A transient connection/timeout error is retried once immediately (our
        scripts only ever make a limit stricter if they run twice). A second failure trips the
        breaker and raises _BackendDown; other Redis errors (bugs) trip it at once."""
        if not self._may_call():
            raise _BackendDown()
        last: BaseException | None = None
        for _attempt in (1, 2):
            try:
                result = fn(self._get_client())
                if self._down_until:
                    self._down_until = 0.0
                return result
            except (redis.exceptions.TimeoutError, redis.exceptions.ConnectionError, OSError) as e:
                last = e
                continue  # one immediate retry
            except RedisError as e:
                last = e
                break
        self._trip(last)
        raise _BackendDown() from last

    def warm(self) -> bool:
        """Connect and load every Lua script ahead of the first real request. Never raises."""
        try:
            def _load(c):
                c.ping()
                for name, src in _SCRIPTS.items():
                    self._script(name, src)  # create the Script object once
                    c.script_load(src)       # and have Redis cache it (EVALSHA works on the first real call)
                return True

            return bool(self.run(_load))
        except _BackendDown:
            return False

    def script(self, name: str, source: str, keys: list[str], args: list) -> object:
        return self.run(lambda c: self._script(name, source)(keys=keys, args=args, client=c))

    # Keys are namespaced and HMAC-hashed: no raw email / IP / user id is ever stored.
    def key(self, kind: str, name: str, raw: str) -> str:
        digest = hmac.new(settings.jwt_secret.encode(), f"rl|{raw}".encode(), hashlib.sha256).hexdigest()[:32]
        return f"{self.prefix}:{kind}:{name}:{digest}"

    def delete_namespace(self, kind: str, name: str) -> None:
        """Test helper: remove every key of one limiter."""
        try:
            self.run(lambda c: [c.delete(k) for k in c.scan_iter(f"{self.prefix}:{kind}:{name}:*", count=500)])
        except _BackendDown:
            pass

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None
            self._scripts.clear()


def _make_backend() -> RedisBackend:
    return RedisBackend(settings.redis_url, settings.redis_timeout_seconds, settings.redis_retry_seconds, settings.rate_limit_prefix)


_backend = _make_backend()


def get_backend() -> RedisBackend:
    return _backend


def set_backend(backend: RedisBackend) -> RedisBackend:
    """Swap the shared backend (tests; simulating another application instance)."""
    global _backend
    previous, _backend = _backend, backend
    return previous


def _fallback_or_refuse(backend: RedisBackend, strict: bool = True) -> None:
    """Called when Redis could not be used. Raises if the policy is fail-closed AND this limiter is
    strict (login / register / paid research). Non-strict limiters (generic API traffic) always degrade
    to the local limit instead of taking the whole API down with Redis."""
    if strict and backend.enabled and settings.rate_limit_fail_mode == "closed":
        raise RateLimitUnavailable()


# ── In-process implementations (fallback, and the whole story when REDIS_URL is empty) ──

class _LocalWindow:
    def __init__(self, limit: int, window: float) -> None:
        self.limit, self.window = limit, window
        self._hits: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def hit(self, key: str) -> Decision:
        now = time.monotonic()
        with self._lock:
            q = self._hits[key]
            while q and now - q[0] >= self.window:
                q.popleft()
            if len(q) >= self.limit:
                return Decision(False, max(1, math.ceil(self.window - (now - q[0]))))
            q.append(now)
            if len(self._hits) > 50_000:  # bound memory under a key-spraying attack
                for k in [k for k, v in self._hits.items() if not v or now - v[-1] >= self.window][:10_000]:
                    self._hits.pop(k, None)
            return Decision(True)

    def reset(self) -> None:
        with self._lock:
            self._hits.clear()


class _LocalLockout:
    def __init__(self, max_failures: int, lockout: float) -> None:
        self.max_failures, self.lockout = max_failures, lockout
        self._fails: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def _prune(self, q: deque[float], now: float) -> None:
        while q and now - q[0] >= self.lockout:
            q.popleft()

    def is_locked(self, key: str) -> bool:
        now = time.monotonic()
        with self._lock:
            q = self._fails.get(key)
            if not q:
                return False
            self._prune(q, now)
            return len(q) >= self.max_failures

    def record_failure(self, key: str) -> None:
        now = time.monotonic()
        with self._lock:
            q = self._fails[key]
            self._prune(q, now)
            q.append(now)

    def clear(self, key: str) -> None:
        with self._lock:
            self._fails.pop(key, None)

    def reset(self) -> None:
        with self._lock:
            self._fails.clear()


class _LocalGuard:
    def __init__(self, limit: int, ttl: float) -> None:
        self.limit, self.ttl = limit, ttl
        self._slots: dict[str, dict[str, float]] = defaultdict(dict)
        self._lock = threading.Lock()

    def _expire(self, key: str, now: float) -> dict[str, float]:
        slots = self._slots[key]
        for t in [t for t, exp in slots.items() if exp <= now]:
            del slots[t]
        return slots

    def acquire(self, key: str) -> str | None:
        now = time.monotonic()
        with self._lock:
            slots = self._expire(key, now)
            if len(slots) >= self.limit:
                return None
            token = uuid.uuid4().hex
            slots[token] = now + self.ttl
            return token

    def release(self, key: str, token: str) -> None:
        with self._lock:
            slots = self._slots.get(key)
            if slots:
                slots.pop(token, None)
                if not slots:
                    self._slots.pop(key, None)

    def active(self, key: str) -> int:
        with self._lock:
            return len(self._expire(key, time.monotonic()))

    def reset(self) -> None:
        with self._lock:
            self._slots.clear()


# ── Public façades: Redis first, bounded local fallback ─────────────────────

class SlidingWindowLimiter:
    """At most `limit` hits per `window` seconds per key, shared across instances."""

    kind = "win"

    def __init__(self, name: str, limit: int, window: float, backend: "RedisBackend | None" = None, strict: bool = True) -> None:
        self.name, self.limit, self.window = name, limit, window
        self._backend = backend
        self.strict = strict
        self._local = _LocalWindow(limit, window)

    def _b(self) -> RedisBackend:
        return self._backend or get_backend()

    def hit(self, key: str) -> Decision:
        b = self._b()
        if b.usable:
            try:
                allowed, retry_ms = b.script(
                    "window", _SLIDING_WINDOW, [b.key(self.kind, self.name, key)],
                    [self.limit, int(self.window * 1000), uuid.uuid4().hex],
                )
                return Decision(True) if int(allowed) == 1 else Decision(False, max(1, math.ceil(int(retry_ms) / 1000)))
            except _BackendDown:
                pass
        _fallback_or_refuse(b, self.strict)
        return self._local.hit(key)

    def reset(self) -> None:
        self._local.reset()
        self._b().delete_namespace(self.kind, self.name)


class FailureLockout:
    """Lock a key after `max_failures` failures; the lock lasts `lockout` seconds from the first failure."""

    kind = "lock"

    def __init__(self, name: str, max_failures: int, lockout: float, backend: "RedisBackend | None" = None, strict: bool = True) -> None:
        self.name, self.max_failures, self.lockout = name, max_failures, lockout
        self._backend = backend
        self.strict = strict
        self._local = _LocalLockout(max_failures, lockout)

    def _b(self) -> RedisBackend:
        return self._backend or get_backend()

    def is_locked(self, key: str) -> bool:
        b = self._b()
        if b.usable:
            try:
                value = b.run(lambda c: c.get(b.key(self.kind, self.name, key)))
                return value is not None and int(value) >= self.max_failures
            except _BackendDown:
                pass
        _fallback_or_refuse(b, self.strict)
        return self._local.is_locked(key)

    def record_failure(self, key: str) -> None:
        b = self._b()
        if b.usable:
            try:
                b.script("fail", _FAILURE_INCR, [b.key(self.kind, self.name, key)], [int(self.lockout)])
                return
            except _BackendDown:
                pass
        _fallback_or_refuse(b, self.strict)
        self._local.record_failure(key)

    def clear(self, key: str) -> None:
        self._local.clear(key)
        b = self._b()
        if b.usable:
            try:
                b.run(lambda c: c.delete(b.key(self.kind, self.name, key)))
            except _BackendDown:
                pass

    def reset(self) -> None:
        self._local.reset()
        self._b().delete_namespace(self.kind, self.name)


class ConcurrencyGuard:
    """Limit simultaneous in-flight operations per key. Slots expire after `ttl` so a crashed
    holder can never lock a user (or the whole service) out."""

    kind = "slot"

    def __init__(self, name: str, limit: int, ttl: float = 900.0, backend: "RedisBackend | None" = None, strict: bool = True) -> None:
        self.name, self.limit, self.ttl = name, limit, ttl
        self._backend = backend
        self.strict = strict
        self._local = _LocalGuard(limit, ttl)

    def _b(self) -> RedisBackend:
        return self._backend or get_backend()

    def acquire(self, key: str) -> Slot | None:
        b = self._b()
        if b.usable:
            try:
                token = uuid.uuid4().hex
                ok = b.script("slot", _SLOT_ACQUIRE, [b.key(self.kind, self.name, key)], [self.limit, int(self.ttl * 1000), token])
                return Slot(key, token, True) if int(ok) == 1 else None
            except _BackendDown:
                pass
        _fallback_or_refuse(b, self.strict)
        token = self._local.acquire(key)
        return Slot(key, token, False) if token else None

    def release(self, slot: Slot | None) -> None:
        if slot is None:
            return
        if not slot.shared:
            self._local.release(slot.key, slot.token)
            return
        b = self._b()
        try:
            b.run(lambda c: c.zrem(b.key(self.kind, self.name, slot.key), slot.token))
        except _BackendDown:
            pass  # the slot's TTL frees it; releasing must never raise into request handling

    def active(self, key: str) -> int:
        b = self._b()
        if b.usable:
            try:
                return int(b.script("slotcount", _SLOT_COUNT, [b.key(self.kind, self.name, key)], []))
            except _BackendDown:
                pass
        return self._local.active(key)

    def reset(self) -> None:
        self._local.reset()
        self._b().delete_namespace(self.kind, self.name)
