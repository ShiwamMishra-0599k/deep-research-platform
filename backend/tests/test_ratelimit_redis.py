"""Distributed rate limiting against REAL Redis (isolated test instance, db 3).

Covers: atomicity under concurrency, several application instances sharing one limit, expiry,
key hygiene, and every Redis failure mode (down, hanging, flapping, fail-closed).
"""
import socket
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest
import redis

from app.config import settings
from app.ratelimit import (
    ConcurrencyGuard,
    FailureLockout,
    RateLimitUnavailable,
    RedisBackend,
    SlidingWindowLimiter,
)

REDIS_URL = settings.redis_url


def _backend(url: str | None = None, timeout: float = 0.25, retry: float = 0.2) -> RedisBackend:
    return RedisBackend(url or REDIS_URL, timeout, retry, settings.rate_limit_prefix)


@pytest.fixture(autouse=True)
def _require_redis():
    try:
        redis.Redis.from_url(REDIS_URL, socket_connect_timeout=1).ping()
    except Exception:  # noqa: BLE001
        pytest.skip("test Redis not reachable (start lumen-test-redis, see AGENTS.md)")


@pytest.fixture()
def name() -> str:
    return "t-" + uuid.uuid4().hex[:10]


def _race(n: int, fn):
    """Run fn(i) in n threads released at the same instant; return the results."""
    barrier = threading.Barrier(n)

    def go(i):
        barrier.wait()
        return fn(i)

    with ThreadPoolExecutor(max_workers=n) as ex:
        return list(ex.map(go, range(n)))


# ── sliding window ──────────────────────────────────────────────────────────

def test_allows_up_to_limit_then_denies_with_whole_second_retry_after(name):
    lim = SlidingWindowLimiter(name, 3, 30, backend=_backend())
    assert [lim.hit("k").allowed for _ in range(3)] == [True, True, True]
    d = lim.hit("k")
    assert d.allowed is False and isinstance(d.retry_after, int) and 1 <= d.retry_after <= 30


def test_keys_are_independent(name):
    lim = SlidingWindowLimiter(name, 1, 30, backend=_backend())
    assert lim.hit("a").allowed and not lim.hit("a").allowed
    assert lim.hit("b").allowed


def test_window_expires(name):
    lim = SlidingWindowLimiter(name, 2, 1, backend=_backend())
    assert lim.hit("k").allowed and lim.hit("k").allowed and not lim.hit("k").allowed
    time.sleep(1.2)
    assert lim.hit("k").allowed  # old hits slid out of the window


def test_sliding_window_has_no_fixed_window_boundary_burst(name):
    """A fixed window would allow 2*limit around the boundary; a sliding window must not."""
    lim = SlidingWindowLimiter(name, 3, 2, backend=_backend())
    time.sleep(1.0)
    assert sum(lim.hit("k").allowed for _ in range(3)) == 3
    time.sleep(1.1)  # first window "rolled over" in wall-clock terms, but the hits are still < 2 s old
    assert sum(lim.hit("k").allowed for _ in range(3)) == 0


def test_retry_after_reflects_when_a_slot_frees(name):
    lim = SlidingWindowLimiter(name, 1, 3, backend=_backend())
    lim.hit("k")
    time.sleep(1.1)
    assert 1 <= lim.hit("k").retry_after <= 2


# ── atomicity & multi-instance ──────────────────────────────────────────────

def test_exactly_limit_requests_pass_under_a_concurrent_burst(name):
    """Atomicity, isolated from timeout behaviour (generous timeout so no call falls back)."""
    be = _backend(timeout=3.0)
    be.warm()
    lim = SlidingWindowLimiter(name, 10, 60, backend=be)
    results = _race(80, lambda i: lim.hit("burst").allowed)
    assert sum(results) == 10  # not 11, not 9: check-and-record is atomic


def test_burst_with_production_timeouts_stays_exact_after_warm_up(name):
    """Regression: a cold 80-thread burst at the 250 ms production timeout used to time out, fall back to
    per-process limits and grant 14 of 10. With a bounded pool + warm-up + one retry it must stay exact."""
    be = _backend()               # production-like: timeout 0.25 s
    assert be.warm() is True
    lim = SlidingWindowLimiter(name, 10, 60, backend=be)
    results = _race(80, lambda i: lim.hit("burst").allowed)
    assert sum(results) == 10 and be.status == "redis"


def test_warm_up_never_raises_when_redis_is_down():
    assert _backend("redis://127.0.0.1:1/0").warm() is False


def test_limit_is_shared_across_application_instances(name):
    """Two independent clients/pools (= two backend processes) enforce ONE shared limit."""
    a = SlidingWindowLimiter(name, 12, 60, backend=_backend())
    b = SlidingWindowLimiter(name, 12, 60, backend=_backend())
    results = _race(100, lambda i: (a if i % 2 else b).hit("shared").allowed)
    assert sum(results) == 12


def test_in_process_limits_alone_would_multiply_across_instances(name):
    """Documents the problem being fixed: with no shared store each instance grants the full limit."""
    a = SlidingWindowLimiter(name, 5, 60, backend=RedisBackend("", 0.25, 0.2, "x"))
    b = SlidingWindowLimiter(name, 5, 60, backend=RedisBackend("", 0.25, 0.2, "x"))
    assert sum(a.hit("k").allowed for _ in range(10)) + sum(b.hit("k").allowed for _ in range(10)) == 10


def test_lockout_is_shared_across_instances(name):
    a = FailureLockout(name, 3, 30, backend=_backend())
    b = FailureLockout(name, 3, 30, backend=_backend())
    for _ in range(3):
        a.record_failure("user@example.com")
    assert b.is_locked("user@example.com")  # recorded on A, enforced on B
    assert not b.is_locked("someone-else@example.com")
    b.clear("user@example.com")
    assert not a.is_locked("user@example.com")


def test_lockout_counts_are_not_lost_under_concurrency(name):
    lock = FailureLockout(name, 20, 30, backend=_backend())
    _race(20, lambda i: lock.record_failure("k"))
    assert lock.is_locked("k")  # all 20 increments landed (INCR is atomic)


def test_lockout_expires(name):
    lock = FailureLockout(name, 2, 1, backend=_backend())
    lock.record_failure("k"); lock.record_failure("k")
    assert lock.is_locked("k")
    time.sleep(1.2)
    assert not lock.is_locked("k")


def test_concurrency_guard_never_exceeds_its_limit_under_a_burst(name):
    g = ConcurrencyGuard(name, 3, ttl=30, backend=_backend())
    slots = _race(50, lambda i: g.acquire("u"))
    held = [s for s in slots if s is not None]
    assert len(held) == 3 and g.active("u") == 3


def test_concurrency_guard_is_shared_and_release_frees_a_slot(name):
    a = ConcurrencyGuard(name, 2, ttl=30, backend=_backend())
    b = ConcurrencyGuard(name, 2, ttl=30, backend=_backend())
    s1, s2 = a.acquire("u"), b.acquire("u")
    assert s1 and s2 and a.acquire("u") is None and b.acquire("u") is None
    b.release(s1)  # released from the other instance
    assert a.acquire("u") is not None


def test_concurrency_slot_expires_if_the_holder_dies(name):
    g = ConcurrencyGuard(name, 1, ttl=1, backend=_backend())
    assert g.acquire("u") is not None and g.acquire("u") is None
    time.sleep(1.2)  # holder crashed and never released
    assert g.acquire("u") is not None


# ── key hygiene & cleanup ───────────────────────────────────────────────────

def test_every_key_has_a_ttl_and_no_raw_identifier_is_stored(name):
    be = _backend()
    email, ip, uid = f"victim-{uuid.uuid4().hex[:6]}@example.com", "203.0.113.77", str(uuid.uuid4())
    SlidingWindowLimiter(name, 5, 60, backend=be).hit(ip)
    FailureLockout(name, 5, 60, backend=be).record_failure(email)
    ConcurrencyGuard(name, 5, ttl=60, backend=be).acquire(uid)

    r = redis.Redis.from_url(REDIS_URL, decode_responses=True)
    keys = list(r.scan_iter(f"{settings.rate_limit_prefix}:*:{name}:*"))
    assert len(keys) == 3
    for k in keys:
        assert r.ttl(k) > 0, f"{k} has no expiry (would never be cleaned up)"
        assert email not in k and ip not in k and uid not in k  # hashed
        members = r.zrange(k, 0, -1) if r.type(k) == "zset" else []
        assert all(email not in m and ip not in m and uid not in m for m in members)


def test_backend_status_reporting():
    assert _backend().status == "redis"
    assert RedisBackend("", 0.25, 0.2, "x").status == "memory"
    assert _backend("redis://127.0.0.1:1").enabled is True


# ── failure modes ───────────────────────────────────────────────────────────

DEAD = "redis://127.0.0.1:1/0"  # nothing listens here


def test_redis_down_falls_back_to_enforced_local_limits_never_unlimited(name):
    lim = SlidingWindowLimiter(name, 3, 60, backend=_backend(DEAD, retry=30))
    decisions = [lim.hit("k").allowed for _ in range(6)]
    assert decisions == [True, True, True, False, False, False]  # still limited, no exception


def test_circuit_breaker_stops_paying_the_timeout_on_every_request(name):
    be = _backend(DEAD, retry=30)
    lim = SlidingWindowLimiter(name, 100, 60, backend=be)
    lim.hit("k")  # first call discovers the outage
    assert be.available is False and be.status == "degraded-local"
    t0 = time.perf_counter()
    for _ in range(200):
        lim.hit("k")
    assert time.perf_counter() - t0 < 0.5  # no network attempts while the breaker is open


def test_all_three_guards_fall_back_when_redis_is_down(name):
    be = _backend(DEAD, retry=30)
    lock = FailureLockout(name, 2, 60, backend=be)
    lock.record_failure("k"); lock.record_failure("k")
    assert lock.is_locked("k")
    g = ConcurrencyGuard(name, 1, ttl=60, backend=be)
    s = g.acquire("k")
    assert s is not None and not s.shared and g.acquire("k") is None
    g.release(s)
    assert g.acquire("k") is not None


def test_hanging_redis_cannot_stall_requests(name):
    """A server that accepts the TCP connection but never answers: the call must time out fast."""
    srv = socket.socket(); srv.bind(("127.0.0.1", 0)); srv.listen(8)
    port, conns = srv.getsockname()[1], []

    def accept():
        while True:
            try:
                conns.append(srv.accept()[0])
            except OSError:
                return

    threading.Thread(target=accept, daemon=True).start()
    try:
        lim = SlidingWindowLimiter(name, 2, 60, backend=_backend(f"redis://127.0.0.1:{port}/0", timeout=0.2, retry=30))
        t0 = time.perf_counter()
        results = [lim.hit("k").allowed for _ in range(4)]
        assert time.perf_counter() - t0 < 2.0
        assert results == [True, True, False, False]  # enforced locally while Redis hangs
    finally:
        srv.close()
        [c.close() for c in conns]


def test_fail_closed_mode_refuses_instead_of_running_unlimited(name, monkeypatch):
    monkeypatch.setattr(settings, "rate_limit_fail_mode", "closed")
    be = _backend(DEAD, retry=30)
    with pytest.raises(RateLimitUnavailable):
        SlidingWindowLimiter(name, 5, 60, backend=be).hit("k")
    with pytest.raises(RateLimitUnavailable):
        FailureLockout(name, 5, 60, backend=be).is_locked("k")
    with pytest.raises(RateLimitUnavailable):
        FailureLockout(name, 5, 60, backend=be).record_failure("k")
    with pytest.raises(RateLimitUnavailable):
        ConcurrencyGuard(name, 5, ttl=60, backend=be).acquire("k")


def test_fail_closed_does_not_trigger_when_redis_is_simply_not_configured(name, monkeypatch):
    monkeypatch.setattr(settings, "rate_limit_fail_mode", "closed")
    lim = SlidingWindowLimiter(name, 1, 60, backend=RedisBackend("", 0.25, 0.2, "x"))
    assert lim.hit("k").allowed and not lim.hit("k").allowed  # memory mode is a deliberate dev setup


def test_recovers_to_redis_after_a_transient_failure(name, monkeypatch):
    be = _backend(retry=0.3)
    lim = SlidingWindowLimiter(name, 50, 60, backend=be)
    assert lim.hit("k").allowed and be.status == "redis"

    real = be._get_client
    flapping = {"on": True}

    def flaky():
        if flapping["on"]:
            raise redis.exceptions.ConnectionError("simulated outage")
        return real()

    monkeypatch.setattr(be, "_get_client", flaky)
    assert lim.hit("k").allowed          # served by the local fallback
    assert be.status == "degraded-local"
    flapping["on"] = False
    time.sleep(0.4)                      # breaker cool-down elapses
    assert lim.hit("k").allowed and be.status == "redis"  # back on the shared store


def test_a_failing_release_never_raises_into_request_handling(name, monkeypatch):
    g = ConcurrencyGuard(name, 2, ttl=30, backend=_backend(retry=30))
    slot = g.acquire("u")
    monkeypatch.setattr(g._b(), "_get_client", lambda: (_ for _ in ()).throw(redis.exceptions.ConnectionError("x")))
    g.release(slot)  # must swallow: the slot's TTL will free it
    g.release(None)


def test_script_cache_survives_a_redis_flush(name):
    """SCRIPT FLUSH / restart drops cached Lua; redis-py must transparently reload it."""
    be = _backend()
    lim = SlidingWindowLimiter(name, 5, 60, backend=be)
    assert lim.hit("k").allowed
    redis.Redis.from_url(REDIS_URL).script_flush()
    assert lim.hit("k").allowed


# ── half-open circuit breaker (found by the Redis-stall load test) ──────────

def _break(be, monkeypatch, calls=None):
    def broken():
        if calls is not None:
            calls.append(1)
        raise redis.exceptions.ConnectionError("redis is down")

    real = be._get_client
    monkeypatch.setattr(be, "_get_client", broken)
    return real


def test_breaker_notices_recovery_within_the_probe_interval_not_the_whole_retry_period(monkeypatch):
    from app.ratelimit import _BackendDown

    monkeypatch.setattr(settings, "redis_probe_seconds", 0.1)
    be = _backend(retry=30)                                    # "down for 30 s" in the old design
    real = _break(be, monkeypatch)
    with pytest.raises(_BackendDown):
        be.run(lambda c: c.ping())
    assert be.available is False
    with pytest.raises(_BackendDown):
        be.run(lambda c: c.ping())                             # inside the probe window: refused without touching Redis
    monkeypatch.setattr(be, "_get_client", real)               # Redis is back
    time.sleep(0.15)
    assert be.run(lambda c: c.ping()) is True                  # the probe succeeds
    assert be.available is True and be._down_until == 0.0      # closed again about 0.15 s after recovery, not 30 s


def test_only_one_probe_per_interval_while_redis_is_down(monkeypatch):
    from app.ratelimit import _BackendDown

    monkeypatch.setattr(settings, "redis_probe_seconds", 0.2)
    be = _backend(retry=30)
    calls: list[int] = []
    _break(be, monkeypatch, calls)
    with pytest.raises(_BackendDown):
        be.run(lambda c: c.ping())
    tripped_calls = len(calls)
    time.sleep(0.25)                                           # the next probe is due

    def hammer(_):
        try:
            be.run(lambda c: c.ping())
        except _BackendDown:
            pass

    with ThreadPoolExecutor(20) as ex:
        list(ex.map(hammer, range(20)))
    assert len(calls) - tripped_calls == 2                     # ONE probe (its two attempts), not twenty callers hitting a dead Redis


def test_fail_closed_refuses_during_the_outage_and_accepts_again_right_after_recovery(name, monkeypatch):
    monkeypatch.setattr(settings, "rate_limit_fail_mode", "closed")
    monkeypatch.setattr(settings, "redis_probe_seconds", 0.1)
    be = _backend(retry=30)
    lim = SlidingWindowLimiter(name, 100, 60, backend=be)
    real = _break(be, monkeypatch)
    with pytest.raises(RateLimitUnavailable):
        lim.hit("k")
    with pytest.raises(RateLimitUnavailable):
        lim.hit("k")                                           # still refused while down (fail-closed is preserved)
    monkeypatch.setattr(be, "_get_client", real)
    time.sleep(0.15)
    assert lim.hit("k").allowed is True                        # and served again as soon as the next probe succeeds
