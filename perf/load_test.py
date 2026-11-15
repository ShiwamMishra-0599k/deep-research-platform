"""Local load test of the whole stack: REAL backend, agent, PostgreSQL, Redis and SSE; FAKE LLM (fixed latency) and fake internet.

    backend/.venv or research-agent/.venv python (needs httpx, psutil, redis: the agent venv has them):
    research-agent/.venv/Scripts/python.exe perf/load_test.py --levels 1,5,10,25 --rounds 2 --llm-delay-ms 1500
    ...  --api-load            # also hammer cheap authenticated endpoints
    ...  --fault agent-kill    # kill the agent during a run, measure errors and time to recover

WHAT THIS MEASURES: the overhead and limits of OUR code (backend, agent orchestration, database, Redis, SSE fan-out) on ONE developer
machine, with an LLM that answers after a fixed delay. WHAT IT DOES NOT MEASURE: real model latency or cost, real network, a
production-sized machine, or anything about scalability beyond this box. Do not quote these numbers as production capacity.

Uses only the throwaway test containers (PostgreSQL 55432, Redis 56379). Creates and drops the database `lumen_perf_test` only.
"""
import argparse
import asyncio
import json
import os
import platform
import statistics
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

import httpx
import psutil
import redis

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "e2e"))
import run as stack  # noqa: E402  (helpers: start, wait_http, stop_all, backend_env, ports)

PERF_DB = "lumen_perf_test"
PERF_URL = f"postgresql+psycopg://lumen_test:lumen_test@127.0.0.1:55432/{PERF_DB}"
RL_REDIS = "redis://127.0.0.1:56379/7"      # rate limits for this run (db 7: nothing else uses it)
AGENT_CACHE_REDIS = "redis://127.0.0.1:56379/8"
API = f"http://127.0.0.1:{stack.P_API}"
OUT = ROOT / "perf" / "results"
QUERY = "Please research the Reserve Bank of India repo rate and give me a summary"   # routes to web_search in the fake LLM


def pct(vals, p):
    if not vals:
        return None
    s = sorted(vals)
    return round(s[min(len(s) - 1, max(0, int(round(p / 100 * (len(s) - 1)))))], 3)


def summarize(vals):
    return {"n": len(vals), "p50": pct(vals, 50), "p95": pct(vals, 95), "max": round(max(vals), 3) if vals else None,
            "mean": round(statistics.fmean(vals), 3) if vals else None}


# ── Resource sampling ───────────────────────────────────────────────────────────────────────────────────────────────
class Sampler(threading.Thread):
    def __init__(self, procs: dict[str, int]):
        super().__init__(daemon=True)
        self.procs = {n: psutil.Process(pid) for n, pid in procs.items()}
        for p in self.procs.values():
            p.cpu_percent(None)
        self.rows: list[dict] = []
        self._cache: dict[int, psutil.Process] = {}
        self.diag = None
        self.stop_flag = threading.Event()
        self.r = redis.Redis.from_url(RL_REDIS)
        self.ncpu = psutil.cpu_count()

    def cpu(self, proc):
        """psutil needs the SAME Process object between calls to compute a percentage; children() returns fresh ones."""
        try:
            cached = self._cache.get(proc.pid)
            if cached is None:
                self._cache[proc.pid] = proc
                proc.cpu_percent(None)       # establishes the baseline; the first reading is meaningless
                return 0.0
            return cached.cpu_percent(None)
        except psutil.Error:
            return 0.0

    def stuck_queries(self):
        """What the sessions that sit 'idle in transaction' last ran, and for how long (diagnostic for pool exhaustion)."""
        try:
            out = subprocess.run(["docker", "exec", "lumen-test-pg", "psql", "-U", "lumen_test", "-d", PERF_DB, "-tAc",
                                  "select left(regexp_replace(query, '\s+', ' ', 'g'), 110) q, count(*), round(max(extract(epoch from now()-state_change))::numeric,1) "
                                  f"from pg_stat_activity where datname='{PERF_DB}' and state='idle in transaction' group by 1 order by 2 desc limit 4"],
                                 capture_output=True, text=True, timeout=5).stdout.strip().splitlines()
            return out
        except Exception:
            return None

    def pg_connections(self):
        """Returns {state: count} for this database. 'idle in transaction' = a session someone is holding open mid-request."""
        try:
            out = subprocess.run(["docker", "exec", "lumen-test-pg", "psql", "-U", "lumen_test", "-d", PERF_DB, "-tAc",
                                  f"select coalesce(state,'none'), count(*) from pg_stat_activity where datname='{PERF_DB}' "
                                  "and pid <> pg_backend_pid() group by 1"],
                                 capture_output=True, text=True, timeout=5).stdout.strip().splitlines()
            return {k: int(v) for k, v in (line.split("|") for line in out if "|" in line)}
        except Exception:
            return None

    def run(self):
        while not self.stop_flag.is_set():
            row = {"t": time.time()}
            for n, p in self.procs.items():
                try:
                    # The venv's python.exe is a small launcher; the real interpreter is its child. Sum the whole tree.
                    tree = [p, *p.children(recursive=True)]
                    row[f"{n}_cpu"] = sum(self.cpu(x) for x in tree) / self.ncpu     # percent of the WHOLE machine
                    row[f"{n}_rss_mb"] = sum(x.memory_info().rss for x in tree) / 1e6
                except psutil.Error:
                    row[f"{n}_cpu"] = row[f"{n}_rss_mb"] = None
            try:
                info = self.r.info()
                row["redis_ops"] = info.get("instantaneous_ops_per_sec")
                row["redis_mem_mb"] = info.get("used_memory", 0) / 1e6
                row["redis_evicted"] = info.get("evicted_keys")
                row["redis_clients"] = info.get("connected_clients")
            except Exception:
                pass
            pg = self.pg_connections()
            if pg is not None:
                row["pg_conns"] = sum(pg.values())
                row["pg_idle_in_tx"] = pg.get("idle in transaction", 0)
                row["pg_active"] = pg.get("active", 0)
                if row["pg_idle_in_tx"] >= 10 and not self.diag:
                    self.diag = self.stuck_queries()
            self.rows.append(row)
            self.stop_flag.wait(1.0)

    def window(self, t0, t1):
        rows = [r for r in self.rows if t0 <= r["t"] <= t1]
        out = {}
        keys = {k for r in rows for k in r if k != "t"}
        for k in sorted(keys):
            v = [r[k] for r in rows if r.get(k) is not None]
            if v:
                out[k] = {"avg": round(statistics.fmean(v), 1), "max": round(max(v), 1)}
        if rows and "redis_evicted" in rows[0] and rows[-1].get("redis_evicted") is not None:
            out["redis_evicted_delta"] = rows[-1]["redis_evicted"] - (rows[0].get("redis_evicted") or 0)
        return out


class RedisProbe(threading.Thread):
    """Pings the rate-limit Redis every 100 ms from the HARNESS process. If Redis itself were slow this would show it; the backend's own
    0.25 s timeouts can then be attributed to Redis (this probe is slow too) or to the backend (this probe stays fast)."""

    def __init__(self):
        super().__init__(daemon=True)
        self.r = redis.Redis.from_url(RL_REDIS, socket_timeout=2)
        self.samples: list[tuple[float, float]] = []
        self.stop_flag = threading.Event()

    def run(self):
        while not self.stop_flag.is_set():
            t = time.perf_counter()
            try:
                self.r.ping()
                self.samples.append((time.time(), (time.perf_counter() - t) * 1000))
            except Exception:
                self.samples.append((time.time(), float("inf")))
            self.stop_flag.wait(0.1)

    def window(self, t0, t1):
        v = [ms for ts, ms in self.samples if t0 <= ts <= t1]
        fin = [x for x in v if x != float("inf")]
        return {"pings": len(v), "failed": len(v) - len(fin), "p50_ms": pct(fin, 50), "p99_ms": pct(fin, 99), "max_ms": round(max(fin), 1) if fin else None}


def backend_redis_timeouts_since(offset: int) -> tuple[int, int]:
    """(count of 'rate-limit store unavailable' warnings written since byte `offset`, new offset)."""
    path = stack.OUT / "perf_backend.log"
    data = path.read_bytes()
    chunk = data[offset:].decode("utf-8", errors="replace")
    return sum(1 for ln in chunk.splitlines() if "rate-limit store unavailable" in ln), len(data)


# ── Virtual users ───────────────────────────────────────────────────────────────────────────────────────────────────
async def register(client: httpx.AsyncClient, i: int) -> dict:
    email = f"perf{i}-{uuid.uuid4().hex[:8]}@example.com"
    r = await client.post(f"{API}/api/auth/register", json={"email": email, "password": "Aa1!" + uuid.uuid4().hex[:12], "name": f"perf{i}"})
    r.raise_for_status()
    return {"Authorization": f"Bearer {r.json()['token']}"}


async def one_research(client: httpx.AsyncClient, headers: dict, query: str, timeout: float) -> dict:
    t0 = time.monotonic()
    res = {"status": None, "ok": False, "first_event_s": None, "first_checkpoint_s": None, "total_s": None, "events": [], "error": None}
    try:
        c = await client.post(f"{API}/api/conversations", headers=headers, json={})
        c.raise_for_status()
        cid = c.json()["id"]
        t0 = time.monotonic()
        async with client.stream("POST", f"{API}/api/conversations/{cid}/query", headers=headers, json={"query": query}, timeout=timeout) as r:
            res["status"] = r.status_code
            if r.status_code != 200:
                await r.aread()
                res["error"] = f"http {r.status_code}"
                return res
            ev = None
            async for line in r.aiter_lines():
                if line.startswith("event:"):
                    ev = line[6:].strip()
                    now = time.monotonic() - t0
                    res["events"].append(ev)
                    if res["first_event_s"] is None:
                        res["first_event_s"] = now
                    if ev == "checkpoint" and res["first_checkpoint_s"] is None:
                        res["first_checkpoint_s"] = now
        res["total_s"] = time.monotonic() - t0
        res["ok"] = "blocks" in res["events"] and "persisted" in res["events"] and "error" not in res["events"]
        if not res["ok"]:
            res["error"] = "stream ended without blocks+persisted: " + ",".join(sorted(set(res["events"])))
    except Exception as e:   # noqa: BLE001 - a failed virtual user is a data point, not a crash
        res["error"] = type(e).__name__
        res["total_s"] = time.monotonic() - t0
    return res


async def level(users: list[dict], n: int, rounds: int, timeout: float, sampler: Sampler, probe: RedisProbe) -> dict:
    limits = httpx.Limits(max_connections=n * 2 + 10, max_keepalive_connections=n)
    _, log_offset = backend_redis_timeouts_since(10**12)
    async with httpx.AsyncClient(limits=limits, timeout=timeout) as client:
        t0 = time.time()
        results = []
        for _ in range(rounds):
            results += await asyncio.gather(*(one_research(client, users[i], QUERY, timeout) for i in range(n)))
        t1 = time.time()
    timeouts, _ = backend_redis_timeouts_since(log_offset)
    ok = [r for r in results if r["ok"]]
    status: dict[str, int] = {}
    for r in results:
        k = str(r["status"] or r["error"])
        status[k] = status.get(k, 0) + 1
    return {
        "concurrent_users": n, "rounds": rounds, "requests": len(results), "succeeded": len(ok),
        "failed": len(results) - len(ok), "outcomes": status, "wall_s": round(t1 - t0, 2),
        "throughput_per_s": round(len(ok) / (t1 - t0), 2),
        "total_s": summarize([r["total_s"] for r in ok]),
        "first_event_s": summarize([r["first_event_s"] for r in ok if r["first_event_s"] is not None]),
        "first_checkpoint_s": summarize([r["first_checkpoint_s"] for r in ok if r["first_checkpoint_s"] is not None]),
        "resources": sampler.window(t0, t1),
        "redis_probe_from_harness": probe.window(t0, t1),
        "backend_redis_timeout_warnings": timeouts,
        "errors_sample": sorted({r["error"] for r in results if r["error"]})[:5],
    }


async def api_load(users, conc, seconds):
    async def worker(h, stop, lat, errs):
        async with httpx.AsyncClient(timeout=20) as c:
            while time.monotonic() < stop:
                for path in ("/api/auth/me", "/api/conversations"):
                    t = time.monotonic()
                    try:
                        r = await c.get(API + path, headers=h)
                        (lat if r.status_code == 200 else errs).append(time.monotonic() - t if r.status_code == 200 else r.status_code)
                    except Exception as e:   # noqa: BLE001
                        errs.append(type(e).__name__)
    lat, errs = [], []
    stop = time.monotonic() + seconds
    t0 = time.time()
    await asyncio.gather(*(worker(users[i % len(users)], stop, lat, errs) for i in range(conc)))
    return {"concurrency": conc, "seconds": seconds, "requests_ok": len(lat), "errors": len(errs), "error_kinds": sorted(set(map(str, errs)))[:5],
            "rps": round(len(lat) / (time.time() - t0), 1), "latency_s": summarize(lat)}


# ── Stack ───────────────────────────────────────────────────────────────────────────────────────────────────────────
def prepare_database():
    code = ("import sqlalchemy as sa;"
            f"e=sa.create_engine('{stack.PG_ADMIN}',isolation_level='AUTOCOMMIT');c=e.connect();"
            f"c.execute(sa.text('DROP DATABASE IF EXISTS {PERF_DB}'));c.execute(sa.text('CREATE DATABASE {PERF_DB}'))")
    subprocess.run([str(stack.BACKEND_PY), "-c", code], check=True)
    subprocess.run([str(stack.BACKEND_PY), "-m", "app.migrate"], check=True, cwd=ROOT / "backend", env={**os.environ, "DATABASE_URL": PERF_URL})
    for u in (RL_REDIS, AGENT_CACHE_REDIS):
        redis.Redis.from_url(u).flushdb()


def start_stack(delay_ms: int, redis_timeout=None, fail_mode=None):
    stack.start("perf_fake_llm", [stack.AGENT_PY, ROOT / "e2e" / "fake_llm.py", stack.P_LLM], {"FAKE_LLM_DELAY_MS": str(delay_ms)})
    agent_env = {**stack.AGENT_ENV, "REDIS_URL": AGENT_CACHE_REDIS}
    agent = stack.start("perf_agent", [stack.AGENT_PY, ROOT / "e2e" / "run_agent_fake_internet.py", stack.P_AGENT], agent_env, cwd=ROOT / "research-agent")
    be_env = stack.backend_env(DATABASE_URL=PERF_URL, REDIS_URL=RL_REDIS, QUERIES_PER_USER_PER_MINUTE=100000,
                               MAX_CONCURRENT_STREAMS_PER_USER=50, MAX_CONCURRENT_STREAMS_GLOBAL=10000,
                               API_REQUESTS_PER_USER_PER_MINUTE=1000000, REGISTER_PER_IP_PER_HOUR=100000)
    be_env["DATABASE_URL"] = PERF_URL
    if redis_timeout is not None:
        be_env["REDIS_TIMEOUT_SECONDS"] = str(redis_timeout)
    if fail_mode:
        be_env["RATE_LIMIT_FAIL_MODE"] = fail_mode
    backend = stack.start("perf_backend", [stack.BACKEND_PY, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", stack.P_API],
                          be_env, cwd=ROOT / "backend")
    ok = (stack.wait_http(f"http://127.0.0.1:{stack.P_LLM}/health") and stack.wait_http(f"http://127.0.0.1:{stack.P_AGENT}/health", timeout=240)
          and stack.wait_http(f"{API}/health/ready", timeout=120))
    return ok, {"fake_llm": stack.procs[0].pid, "agent": agent.pid, "backend": backend.pid}


async def fault_agent_kill(users, agent_cmd_env, sampler):
    """Kill the agent while 10 researches are in flight; count outcomes; restart it; time how long until a fresh query succeeds."""
    n = 10
    async with httpx.AsyncClient(timeout=120) as client:
        tasks = [asyncio.create_task(one_research(client, users[i], QUERY, 120)) for i in range(n)]
        await asyncio.sleep(1.0)
        agent_proc = next(p for p in stack.procs if p.args and "run_agent_fake_internet.py" in " ".join(map(str, p.args)))
        t_kill = time.monotonic()
        subprocess.run(["taskkill", "/PID", str(agent_proc.pid), "/T", "/F"], capture_output=True) if os.name == "nt" else agent_proc.kill()
        res = await asyncio.gather(*tasks)
        during = {"in_flight": n, "succeeded": sum(r["ok"] for r in res), "failed": sum(not r["ok"] for r in res),
                  "error_kinds": sorted({r["error"] for r in res if r["error"]})[:5]}
        # agent is down: a new query must fail cleanly, not hang
        t = time.monotonic()
        down = await one_research(client, users[0], QUERY, 60)
        during["new_query_while_down"] = {"ok": down["ok"], "returned_in_s": round(time.monotonic() - t, 2), "error": down["error"]}
        t_restart = time.monotonic()
        stack.start("perf_agent2", [stack.AGENT_PY, ROOT / "e2e" / "run_agent_fake_internet.py", stack.P_AGENT], agent_cmd_env, cwd=ROOT / "research-agent")
        healthy = stack.wait_http(f"http://127.0.0.1:{stack.P_AGENT}/health", timeout=240)
        t_healthy = time.monotonic()
        rec = await one_research(client, users[1], QUERY, 120)
        return {"during_kill": during, "agent_restart_to_healthy_s": round(t_healthy - t_restart, 1) if healthy else None,
                "first_success_after_restart_s": round(time.monotonic() - t_restart, 1) if rec["ok"] else None,
                "recovered": bool(rec["ok"]), "kill_to_recovery_s": round(time.monotonic() - t_kill, 1)}


async def fault_redis_stall(users, stall_ms: int, seconds: float, conc: int):
    """Sustained research load while the rate-limit Redis is frozen for `stall_ms` (docker pause). Measures how long users are refused.

    Run it with --fail-mode closed to see the production behaviour: a guarded operation that cannot reach Redis is refused (503)."""
    t_begin = time.monotonic()
    events: list[tuple[float, bool, str]] = []        # (seconds since start, ok, outcome)
    stall_at = 6.0
    marks = {}

    async def worker(i):
        async with httpx.AsyncClient(timeout=120) as c:
            while time.monotonic() - t_begin < seconds:
                r = await one_research(c, users[i % len(users)], QUERY, 120)
                events.append((time.monotonic() - t_begin, bool(r["ok"]), str(r["status"] or r["error"])))

    async def staller():
        await asyncio.sleep(stall_at)
        marks["pause"] = time.monotonic() - t_begin
        subprocess.run(["docker", "pause", "lumen-test-redis"], capture_output=True)
        await asyncio.sleep(stall_ms / 1000)
        subprocess.run(["docker", "unpause", "lumen-test-redis"], capture_output=True)
        marks["resume"] = time.monotonic() - t_begin

    try:
        await asyncio.gather(staller(), *(worker(i) for i in range(conc)))
    finally:
        subprocess.run(["docker", "unpause", "lumen-test-redis"], capture_output=True)   # never leave the shared test Redis frozen
    bad = [(t, o) for t, ok, o in events if not ok]
    first, last = (min(t for t, _ in bad), max(t for t, _ in bad)) if bad else (None, None)
    return {
        "stall_ms": stall_ms, "paused_at_s": round(marks.get("pause", 0), 2), "resumed_at_s": round(marks.get("resume", 0), 2),
        "requests": len(events), "failed": len(bad), "failure_outcomes": sorted({o for _, o in bad}),
        "first_failure_s": round(first, 2) if first is not None else None, "last_failure_s": round(last, 2) if last is not None else None,
        "refusals_lasted_after_resume_s": round(max(0.0, last - marks.get("resume", 0)), 2) if last is not None else 0.0,
    }


def git_sha():
    sha = subprocess.run(["git", "rev-parse", "--short", "HEAD"], capture_output=True, text=True, cwd=ROOT).stdout.strip()
    dirty = subprocess.run(["git", "status", "--porcelain", "--", "backend/app", "research-agent/src", "e2e", "perf"],
                           capture_output=True, text=True, cwd=ROOT).stdout.strip()
    return sha + ("+uncommitted-changes" if dirty else "")


async def amain(a):
    ok, pids = start_stack(a.llm_delay_ms, a.redis_timeout_s, a.fail_mode)
    if not ok:
        print("stack failed to start; see e2e/out/perf_*.log")
        return 3
    sampler = Sampler(pids)
    sampler.start()
    probe = RedisProbe()
    probe.start()
    levels = [int(x) for x in a.levels.split(",")]
    hogs = [subprocess.Popen([sys.executable, "-c", "while True: pass"]) for _ in range(a.cpu_hog)]
    try:
        async with httpx.AsyncClient(timeout=30) as c:
            users = [await register(c, i) for i in range(max(levels))]
        async with httpx.AsyncClient(timeout=120) as c:
            warm = await one_research(c, users[0], QUERY, 120)   # untimed: imports, connection pools, first Redis script load
        print(f"warm-up request ok={warm['ok']} in {warm['total_s']:.1f}s (excluded from results)", flush=True)
        report = {
            "kind": "LOCAL LOAD TEST: fake LLM and fake internet; not production capacity",
            "when": time.strftime("%Y-%m-%dT%H:%M:%S"), "git": git_sha(),
            "machine": {"cpus": psutil.cpu_count(), "ram_gb": round(psutil.virtual_memory().total / 1e9, 1), "os": platform.platform(),
                        "python": platform.python_version()},
            "setup": {"cpu_hog_processes": a.cpu_hog, "backend_redis_timeout_s": a.redis_timeout_s if a.redis_timeout_s is not None else "default 0.25", "rate_limit_fail_mode": a.fail_mode or "default (local)", "backend_workers": 1, "agent_workers": 1, "llm_delay_ms_per_call": a.llm_delay_ms, "rounds": a.rounds,
                      "query": QUERY, "note": "each research makes 2 LLM calls (tool request, then answer) plus one fake web search"},
            "levels": [],
        }
        for n in levels:
            print(f"level {n} concurrent x {a.rounds} rounds ...", flush=True)
            lv = await level(users, n, a.rounds, a.timeout, sampler, probe)
            report["levels"].append(lv)
            print(f"  ok {lv['succeeded']}/{lv['requests']}  total p50 {lv['total_s']['p50']}s p95 {lv['total_s']['p95']}s  "
                  f"{lv['throughput_per_s']}/s  pg_conns max {lv['resources'].get('pg_conns', {}).get('max')}", flush=True)
        if a.api_load:
            print("API load ...", flush=True)
            report["api_load"] = await api_load(users, a.api_conc, a.api_seconds)
            print("  ", report["api_load"]["rps"], "rps, p95", report["api_load"]["latency_s"]["p95"], "s", flush=True)
        if a.fault == "agent-kill":
            print("fault: kill the agent mid-run ...", flush=True)
            report["fault_agent_kill"] = await fault_agent_kill(users, {**stack.AGENT_ENV, "REDIS_URL": AGENT_CACHE_REDIS}, sampler)
            print("  ", json.dumps(report["fault_agent_kill"]), flush=True)
        if a.fault == "redis-stall":
            print(f"fault: freeze Redis for {a.stall_ms} ms under sustained load ...", flush=True)
            report["fault_redis_stall"] = await fault_redis_stall(users, a.stall_ms, 25.0, a.stall_conc)
            print("  ", json.dumps(report["fault_redis_stall"]), flush=True)
        report["stuck_idle_in_transaction_sessions"] = sampler.diag
        report["backend_log_errors"] = sum(1 for line in (stack.OUT / "perf_backend.log").read_text(errors="replace").splitlines() if " ERROR " in line)
        OUT.mkdir(parents=True, exist_ok=True)
        path = OUT / f"load-{time.strftime('%Y%m%d-%H%M%S')}.json"
        path.write_text(json.dumps(report, indent=2), encoding="utf-8")
        print("wrote", path)
        return 0
    finally:
        for h in hogs:
            h.kill()
        sampler.stop_flag.set()
        probe.stop_flag.set()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--levels", default="1,5,10,25")
    ap.add_argument("--rounds", type=int, default=2)
    ap.add_argument("--llm-delay-ms", type=int, default=1500, help="fixed latency of every fake LLM call (a real model is slower and variable)")
    ap.add_argument("--timeout", type=float, default=180.0)
    ap.add_argument("--api-load", action="store_true")
    ap.add_argument("--api-conc", type=int, default=50)
    ap.add_argument("--api-seconds", type=int, default=10)
    ap.add_argument("--fault", choices=["agent-kill", "redis-stall"], default=None)
    ap.add_argument("--stall-ms", type=int, default=700, help="how long the rate-limit Redis is frozen for --fault redis-stall")
    ap.add_argument("--stall-conc", type=int, default=20, help="concurrent users during --fault redis-stall (1 = a single user: nothing in flight to heal the breaker)")
    ap.add_argument("--fail-mode", choices=["local", "closed"], default=None, help="RATE_LIMIT_FAIL_MODE for the backend (production uses closed)")
    ap.add_argument("--cpu-hog", type=int, default=0, help="run N busy-loop processes during the test to emulate a loaded host")
    ap.add_argument("--redis-timeout-s", type=float, default=None, help="REDIS_TIMEOUT_SECONDS for the backend (default 0.25)")
    a = ap.parse_args()
    for p in (stack.BACKEND_PY, stack.AGENT_PY):
        if not p.exists():
            print("missing virtualenv:", p)
            return 2
    prepare_database()
    try:
        return asyncio.run(amain(a))
    finally:
        stack.stop_all()
        subprocess.run([str(stack.BACKEND_PY), "-c", "import sqlalchemy as sa;e=sa.create_engine('" + stack.PG_ADMIN +
                        f"',isolation_level='AUTOCOMMIT');c=e.connect();c.execute(sa.text('DROP DATABASE IF EXISTS {PERF_DB} WITH (FORCE)'))"],
                       capture_output=True)


if __name__ == "__main__":
    sys.exit(main())
