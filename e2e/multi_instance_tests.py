"""Distributed rate limiting across SEPARATE backend processes sharing one Redis and one PostgreSQL.

Starts three real uvicorn backends (A, B, C), then proves that limits, lockouts and concurrency
caps are enforced across ALL of them (not per process), that bursts are exact, that Redis outage and
recovery behave as documented (fail-local and fail-closed), and that keys carry no raw identifiers.
Only the throwaway test containers are used; Redis is stopped/started to simulate an outage.

    python e2e/multi_instance_tests.py
"""
import http.client
import json
import sys
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import run  # noqa: E402

PORTS = {"A": 18281, "B": 18282, "C": 18283}
results: list[tuple[str, bool]] = []
REDIS_DB = "redis://127.0.0.1:56379/4"


def check(name, ok, detail=""):
    results.append((name, bool(ok)))
    print(f"{'PASS' if ok else 'FAIL'}  {name}{'' if ok or not detail else '  -> ' + str(detail)[:260]}", flush=True)


def call(port, method, path, body=None, token=None, headers=None, timeout=20):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    h = {"Content-Type": "application/json", **(headers or {})}
    if token:
        h["Authorization"] = f"Bearer {token}"
    try:
        c.request(method, path, json.dumps(body) if body is not None else None, h)
        r = c.getresponse()
        raw = r.read()
        try:
            data = json.loads(raw or b"null")
        except ValueError:
            data = raw.decode(errors="replace")
        return r.status, data, {k.lower(): v for k, v in r.getheaders()}
    finally:
        c.close()


def docker(*a):
    import subprocess

    return subprocess.run(["docker", *a], capture_output=True, text=True, timeout=120)


def backend_env(**extra):
    env = {**run.backend_env(**extra), "REDIS_URL": REDIS_DB, "LOGIN_PER_IP_PER_MINUTE": 6,
           "LOGIN_FAILURES_PER_EMAIL": 5, "QUERIES_PER_USER_PER_MINUTE": 8, "MAX_CONCURRENT_STREAMS_PER_USER": 2,
           "RESEARCH_AGENT_URL": f"http://127.0.0.1:{run.P_AGENT}", "REDIS_RETRY_SECONDS": 1,
           "API_REQUESTS_PER_USER_PER_MINUTE": 1000, "REGISTER_PER_IP_PER_HOUR": 1000}
    return {k: str(v) for k, v in env.items()}


def main():
    run.prepare_database()
    import redis

    r = redis.Redis.from_url(REDIS_DB, decode_responses=True)
    r.flushdb()
    run.start("fake_llm", [run.AGENT_PY, run.ROOT / "e2e" / "fake_llm.py", run.P_LLM])
    run.start("agent", [run.AGENT_PY, run.ROOT / "e2e" / "run_agent_fake_internet.py", run.P_AGENT], run.AGENT_ENV, cwd=run.ROOT / "research-agent")
    uv = lambda port: [run.BACKEND_PY, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", port, "--no-proxy-headers"]  # noqa: E731
    for tag, port in PORTS.items():
        run.start(f"backend_{tag}", uv(port), backend_env(), cwd=run.ROOT / "backend")
    # D: same code but fail-CLOSED, for the outage policy test
    run.start("backend_D", uv(18284), backend_env(RATE_LIMIT_FAIL_MODE="closed"), cwd=run.ROOT / "backend")

    assert run.wait_http(f"http://127.0.0.1:{run.P_LLM}/health")
    assert run.wait_http(f"http://127.0.0.1:{run.P_AGENT}/health", timeout=240), "agent"
    for port in (*PORTS.values(), 18284):
        assert run.wait_http(f"http://127.0.0.1:{port}/health/ready", timeout=120), port
    A, B, C = PORTS["A"], PORTS["B"], PORTS["C"]
    print("3 backend processes + 1 fail-closed backend up, sharing Redis db 4 and PostgreSQL\n", flush=True)

    st, ready, _ = call(A, "GET", "/health/ready")
    check("instances report the shared Redis limiter", ready.get("rate_limit") == "redis", ready)

    # 1. login IP limit (6/min) is shared: spread 12 attempts over three processes
    def login(i):
        port = [A, B, C][i % 3]
        return call(port, "POST", "/api/auth/login", {"email": f"ip{i}@example.com", "password": "wrong-wrong-1"})[0]

    with ThreadPoolExecutor(max_workers=12) as ex:
        codes = list(ex.map(login, range(12)))
    check("login IP limit holds ACROSS 3 processes (exactly 6 of 12 reach the password check)", codes.count(401) == 6 and codes.count(429) == 6, codes)
    st, body, hdr = call(B, "POST", "/api/auth/login", {"email": "late@example.com", "password": "wrong-wrong-1"})
    check("429 carries Retry-After from any instance", st == 429 and int(hdr.get("retry-after", 0)) >= 1, (st, hdr))

    r.flushdb()  # fresh budgets

    # 2. per-account lockout recorded on A and B is enforced on C
    email = f"lock-{uuid.uuid4().hex[:6]}@example.com"
    st, reg, _ = call(A, "POST", "/api/auth/register", {"email": email, "password": "lockout-test-pw1", "name": "L"})
    assert st == 200, (st, reg)
    # every attempt shares one peer IP, so stay under the 6/min IP limit: 5 failures + 1 check
    codes = [call([A, B][i % 2], "POST", "/api/auth/login", {"email": email, "password": "wrong-wrong-1"})[0] for i in range(5)]
    st_c, body_c, _ = call(C, "POST", "/api/auth/login", {"email": email, "password": "lockout-test-pw1"})
    check("5 failures spread over A and B lock the account on C (even for the correct password)", codes == [401] * 5 and st_c == 429, (codes, st_c))

    r.flushdb()

    # 3. per-user research rate limit (8/min) shared across processes; paid operation never exceeds it
    st, reg, _ = call(A, "POST", "/api/auth/register", {"email": f"q-{uuid.uuid4().hex[:6]}@example.com", "password": "query-test-pw-1", "name": "Q"})
    token = reg["token"]
    convs = [call(A, "POST", "/api/conversations", {}, token)[1]["id"] for _ in range(3)]

    def ask(i):
        port = [A, B, C][i % 3]
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=60)
        try:
            c.request("POST", f"/api/conversations/{convs[i % 3]}/query", json.dumps({"query": "clarify please"}),
                      {"Content-Type": "application/json", "Authorization": f"Bearer {token}"})
            resp = c.getresponse()
            resp.read()
            return resp.status
        finally:
            c.close()

    # Sequential on purpose: each request finishes before the next, so only the per-minute limit (not the
    # concurrent-stream cap, tested next) decides. They alternate between three different processes.
    codes = [ask(i) for i in range(24)]
    check("research limit (8/min per user) holds across processes: exactly 8 of 24 admitted", codes.count(200) == 8 and codes.count(429) == 16, codes)

    # 4. per-user concurrency (2) shared across processes: 6 slow streams started on 3 processes
    r.flushdb()
    st, reg, _ = call(A, "POST", "/api/auth/register", {"email": f"c-{uuid.uuid4().hex[:6]}@example.com", "password": "concurrency-pw-1", "name": "C"})
    tk = reg["token"]
    cids = [call(A, "POST", "/api/conversations", {}, tk)[1]["id"] for _ in range(6)]

    def slow(i):
        port = [A, B, C][i % 3]
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=60)
        try:
            c.request("POST", f"/api/conversations/{cids[i]}/query", json.dumps({"query": "slow research"}),
                      {"Content-Type": "application/json", "Authorization": f"Bearer {tk}"})
            resp = c.getresponse()
            status = resp.status
            if status == 200:
                resp.read1(64)       # hold the stream open briefly, then drop the connection
                time.sleep(2.5)
            return status
        finally:
            c.close()

    with ThreadPoolExecutor(max_workers=6) as ex:
        codes = list(ex.map(slow, range(6)))
    check("concurrent-stream cap (2 per user) holds across processes", codes.count(200) == 2 and codes.count(429) == 4, sorted(codes))
    # clients have disconnected: the abandoned streams must free their slots (cancellation propagates), not wait for the TTL
    t0, freed = time.time(), False
    while time.time() - t0 < 20:
        slot_keys = list(r.scan_iter("lumen:rl:slot:*"))
        if all(r.zcard(k) == 0 for k in slot_keys):
            freed = True
            break
        time.sleep(0.5)
    check("abandoned streams release their concurrency slots promptly (not by TTL expiry)", freed, [(k, r.zcard(k)) for k in r.scan_iter("lumen:rl:slot:*")])
    st, _, _ = call(B, "POST", f"/api/conversations/{cids[5]}/query", {"query": "clarify now"}, tk)
    check("the same user can start a new research after the slots were freed", st == 200, st)

    # 5. key hygiene in the shared store
    keys = list(r.scan_iter("lumen:rl:*"))
    no_ttl = [k for k in keys if r.ttl(k) <= 0]
    raw_hits = [k for k in keys if "@" in k or "127.0.0.1" in k or email in k]
    members = [m for k in keys if r.type(k) == "zset" for m in r.zrange(k, 0, -1)]
    check(f"all {len(keys)} rate-limit keys expire (none without TTL)", keys and not no_ttl, no_ttl[:3])
    check("no raw email / IP / user id appears in any key or member", not raw_hits and not any("@" in m or "127.0.0.1" in m for m in members), raw_hits[:3])

    # 6. Redis outage: default mode keeps limits (per process), fail-closed mode refuses
    docker("stop", "lumen-test-redis")
    time.sleep(1.5)
    codes = [call(A, "POST", "/api/auth/login", {"email": f"out{i}@example.com", "password": "wrong-wrong-1"})[0] for i in range(10)]
    check("Redis DOWN (fail-local): login is STILL limited on this instance (6 of 10 pass, never unlimited)", codes.count(401) == 6 and codes.count(429) == 4, codes)
    st, ready, _ = call(A, "GET", "/health/ready")
    check("Redis DOWN: backend stays ready and reports the degraded limiter", st == 200 and ready.get("rate_limit") == "degraded-local", (st, ready))
    st, body, hdr = call(18284, "POST", "/api/auth/login", {"email": "closed@example.com", "password": "wrong-wrong-1"})
    check("Redis DOWN (fail-closed instance): login refused with 503 + Retry-After", st == 503 and hdr.get("retry-after") == "5", (st, body))
    st, body, _ = call(18284, "GET", "/api/auth/me", None, token)
    check("Redis DOWN (fail-closed): ordinary authenticated reads still work", st == 200, (st, body))
    t0 = time.time()
    for _ in range(30):
        call(A, "POST", "/api/auth/login", {"email": "speed@example.com", "password": "wrong-wrong-1"})
    check("Redis DOWN: requests are not slowed (circuit breaker)", (time.time() - t0) / 30 < 1.0, round((time.time() - t0) / 30, 3))

    docker("start", "lumen-test-redis")
    ok = False
    for _ in range(40):
        time.sleep(1)
        st, ready, _ = call(A, "GET", "/health/ready")
        if ready.get("rate_limit") == "redis":
            ok = True
            break
        call(A, "POST", "/api/auth/login", {"email": "probe@example.com", "password": "wrong-wrong-1"})  # traffic re-tests the breaker
    check("Redis RESTORED: instance returns to the shared limiter by itself", ok)
    r2 = redis.Redis.from_url(REDIS_DB, decode_responses=True)
    r2.flushdb()
    codes = [call([A, B, C][i % 3], "POST", "/api/auth/login", {"email": f"rec{i}@example.com", "password": "wrong-wrong-1"})[0] for i in range(12)]
    check("after recovery the limit is global again (6 of 12 across processes)", codes.count(401) == 6 and codes.count(429) == 6, codes)

    print("\n--- summary")
    failed = [n for n, ok in results if not ok]
    print(f"{len(results) - len(failed)}/{len(results)} checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    code = 1
    try:
        code = main()
    except Exception as e:  # noqa: BLE001
        import traceback

        traceback.print_exc()
        print("ABORTED:", e)
    finally:
        docker("start", "lumen-test-redis")
        run.stop_all()
    sys.exit(code)
