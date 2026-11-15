"""Counts successful Bearer-token authentications, so the move from AUTH_MODE=dual to cookie-only can be shown to be safe.

    python -m app.legacy_usage            # last 7 days, JSON; exit 0

Why: cookie-only mode rejects Bearer tokens. Before switching, an operator must see that no real client still sends one
(docs/COOKIE_AUTH_MIGRATION_PLAN.md section 13). Until now there was no way to see that.

* kind "legacy"  = a pre-migration token with no session id (it cannot be revoked and lives at most 24 h);
* kind "session" = a Bearer token bound to a server-side session (issued to clients that asked for a body token).
Cookie-authenticated requests are NOT counted.

Best effort by design: increments are batched in memory and flushed to Redis at most every `legacy_bearer_flush_seconds`; if Redis is down the
batch is dropped (counts can be slightly low, never high) and the request is never affected. Only counts and a last-seen timestamp are stored:
no user ids, tokens, IP addresses or paths.
"""
from __future__ import annotations

import json
import logging
import sys
import threading
import time
from collections import Counter
from datetime import datetime, timedelta, timezone

from .config import settings
from .ratelimit import get_backend

log = logging.getLogger("lumen.legacy_usage")

KINDS = ("legacy", "session")
KEEP_DAYS = 10


def _day(ts: datetime) -> str:
    return ts.strftime("%Y%m%d")


def _key(kind: str, day: str) -> str:
    return f"lumen:auth:bearer:{kind}:{day}"


LAST_KEY = "lumen:auth:bearer:last-seen"


class _Counter:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._pending: Counter[str] = Counter()
        self._last_flush = time.monotonic()
        self.totals: Counter[str] = Counter()     # since process start, for tests and diagnostics

    def record(self, kind: str) -> None:
        batch = None
        with self._lock:
            self._pending[kind] += 1
            self.totals[kind] += 1
            if time.monotonic() - self._last_flush >= settings.legacy_bearer_flush_seconds:
                batch, self._pending = self._pending, Counter()
                self._last_flush = time.monotonic()
        if batch:
            self._flush(batch)

    @staticmethod
    def _flush(batch: Counter[str]) -> None:
        backend = get_backend()
        if not backend.available:
            return                                   # Redis down or not configured: drop the batch
        try:
            client = backend._get_client()           # noqa: SLF001 - same pooled, short-timeout client the rate limiter uses
            now = datetime.now(timezone.utc)
            pipe = client.pipeline(transaction=False)
            for kind, n in batch.items():
                key = _key(kind, _day(now))
                pipe.incrby(key, n)
                pipe.expire(key, KEEP_DAYS * 86400)
            pipe.set(LAST_KEY, now.isoformat(timespec="seconds"), ex=KEEP_DAYS * 86400)
            pipe.execute()
        except Exception:   # noqa: BLE001 - housekeeping must never affect a request, and must not trip the rate limiter's breaker
            log.debug("legacy bearer counter flush failed", exc_info=True)

    def flush_now(self) -> None:
        with self._lock:
            batch, self._pending = self._pending, Counter()
            self._last_flush = time.monotonic()
        if batch:
            self._flush(batch)


counter = _Counter()


def record_bearer_use(has_session: bool) -> None:
    """Called after a Bearer token has been accepted."""
    try:
        counter.record("session" if has_session else "legacy")
    except Exception:   # noqa: BLE001
        log.debug("legacy bearer counter failed", exc_info=True)


def read_report(days: int = 7) -> dict:
    backend = get_backend()
    if not backend.enabled:
        return {"error": "REDIS_URL is not configured; counts are not stored"}
    client = backend._get_client()   # noqa: SLF001
    today = datetime.now(timezone.utc)
    per_day = {}
    for i in range(days):
        d = _day(today - timedelta(days=i))
        per_day[d] = {k: int(client.get(_key(k, d)) or 0) for k in KINDS}
    last = client.get(LAST_KEY)
    last_s = last.decode() if isinstance(last, bytes) else last
    total_24h = sum(per_day[_day(today)].values()) + sum(per_day[_day(today - timedelta(days=1))].values()) if days >= 2 else None
    return {
        "per_day_utc": per_day,
        "last_bearer_request_utc": last_s,
        "calendar_days_today_and_yesterday": total_24h,
        "note": "Counts are batched and best-effort. Zero for several days with the new frontend live is the signal that AUTH_MODE=cookie is safe.",
    }


def main() -> int:
    print(json.dumps(read_report(), indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
