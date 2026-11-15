"""Small in-process rate limiting and concurrency guards (no extra dependencies).

Limits are per process. With several workers/replicas each keeps its own counters, so
the effective limit is multiplied; move to Redis-backed limits before scaling out
(documented in SECURITY_REVIEW.md).
"""
import threading
import time
from collections import defaultdict, deque


class SlidingWindowLimiter:
    """Allow at most `limit` events per `window` seconds per key."""

    def __init__(self, limit: int, window: float) -> None:
        self.limit = limit
        self.window = window
        self._hits: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def allow(self, key: str) -> bool:
        now = time.monotonic()
        with self._lock:
            q = self._hits[key]
            while q and now - q[0] >= self.window:
                q.popleft()
            if len(q) >= self.limit:
                return False
            q.append(now)
            return True

    def retry_after(self, key: str) -> int:
        now = time.monotonic()
        with self._lock:
            q = self._hits.get(key)
            if not q:
                return 1
            return max(1, int(self.window - (now - q[0])) + 1)

    def reset(self) -> None:
        with self._lock:
            self._hits.clear()


class FailureLockout:
    """Lock a key after `max_failures` failures within `lockout` seconds."""

    def __init__(self, max_failures: int, lockout: float) -> None:
        self.max_failures = max_failures
        self.lockout = lockout
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


class ConcurrencyGuard:
    """Limit simultaneous in-flight operations per key (e.g. open research streams per user).

    Slots expire after `ttl` seconds so a stream that never started (or crashed before
    releasing) cannot lock a user out forever.
    """

    def __init__(self, limit: int, ttl: float = 900.0) -> None:
        self.limit = limit
        self.ttl = ttl
        self._active: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def acquire(self, key: str) -> bool:
        now = time.monotonic()
        with self._lock:
            q = self._active[key]
            while q and now - q[0] >= self.ttl:
                q.popleft()
            if len(q) >= self.limit:
                return False
            q.append(now)
            return True

    def release(self, key: str) -> None:
        with self._lock:
            q = self._active.get(key)
            if q:
                q.popleft()
                if not q:
                    self._active.pop(key, None)

    def active(self, key: str) -> int:
        with self._lock:
            return len(self._active.get(key, ()))

    def reset(self) -> None:
        with self._lock:
            self._active.clear()
