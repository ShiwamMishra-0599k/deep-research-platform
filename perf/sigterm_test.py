"""Graceful-shutdown test: run the REAL backend Docker image (Linux), start research streams, send SIGTERM (`docker stop`), observe.

    research-agent/.venv/Scripts/python.exe perf/sigterm_test.py [--streams 8] [--llm-delay-ms 5000] [--stop-timeout 30]

Why a container: Windows has no SIGTERM semantics (terminate() is a hard kill), so the signal path of the shipped image can only be
tested on Linux. The agent and the fake LLM run on the host (sealed fake internet); only PostgreSQL/Redis test containers are used.

Reports: how long `docker stop` took, the exit code, whether in-flight streams completed, whether a NEW connection was refused
during shutdown, and whether the answers of completed streams were saved. Spends nothing; touches only database `lumen_sigterm_test`.
"""
import argparse
import asyncio
import json
import os
import secrets
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

import httpx
import redis

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "e2e"))
import run as stack  # noqa: E402

DB = "lumen_sigterm_test"
ROLE = "lumen_sigterm_role"
PW = secrets.token_hex(16)     # random per run, never stored; the backend refuses weak passwords on non-local hosts
HOST_DB = f"postgresql+psycopg://{ROLE}:{PW}@host.docker.internal:55432/{DB}"
NAME = "lumen-sigterm-backend"
PORT = 58081
URL = f"http://127.0.0.1:{PORT}"
OUT = ROOT / "perf" / "results"


def sh(*cmd, **kw):
    return subprocess.run([str(c) for c in cmd], capture_output=True, text=True, **kw)


def prepare():
    code = ("import sqlalchemy as sa;"
            f"e=sa.create_engine('{stack.PG_ADMIN}',isolation_level='AUTOCOMMIT');c=e.connect();"
            f"c.execute(sa.text('DROP DATABASE IF EXISTS {DB} WITH (FORCE)'));c.execute(sa.text('DROP ROLE IF EXISTS {ROLE}'));"
            f"c.execute(sa.text(\"CREATE ROLE {ROLE} LOGIN PASSWORD '{PW}'\"));c.execute(sa.text('CREATE DATABASE {DB} OWNER {ROLE}'))")
    subprocess.run([str(stack.BACKEND_PY), "-c", code], check=True)
    redis.Redis.from_url("redis://127.0.0.1:56379/9").flushdb()
    redis.Redis.from_url("redis://127.0.0.1:56379/10").flushdb()


def start_host_services(delay_ms):
    stack.start("sig_fake_llm", [stack.AGENT_PY, ROOT / "e2e" / "fake_llm.py", stack.P_LLM], {"FAKE_LLM_DELAY_MS": str(delay_ms)})
    stack.start("sig_agent", [stack.AGENT_PY, ROOT / "e2e" / "run_agent_fake_internet.py", stack.P_AGENT],
                {**stack.AGENT_ENV, "REDIS_URL": "redis://127.0.0.1:56379/10"}, cwd=ROOT / "research-agent")
    return stack.wait_http(f"http://127.0.0.1:{stack.P_LLM}/health") and stack.wait_http(f"http://127.0.0.1:{stack.P_AGENT}/health", timeout=240)


def start_backend():
    sh("docker", "rm", "-f", NAME)
    b = sh("docker", "build", "-q", "-t", "lumen-backend:perf", "backend", cwd=ROOT)
    if b.returncode:
        return False, b.stderr[-300:]
    env = {"DATABASE_URL": HOST_DB, "JWT_SECRET": stack.SECRETS["JWT_SECRET"], "CSRF_SECRET": stack.SECRETS["CSRF_SECRET"],
           "AGENT_API_KEY": stack.SECRETS["AGENT_API_KEY"], "RESEARCH_AGENT_URL": f"http://host.docker.internal:{stack.P_AGENT}",
           "REDIS_URL": "redis://host.docker.internal:56379/9", "QUERIES_PER_USER_PER_MINUTE": "1000", "MAX_CONCURRENT_STREAMS_PER_USER": "20",
           "REGISTER_PER_IP_PER_HOUR": "1000", "RESEARCH_TIMEOUT_SECONDS": "120"}
    args = ["docker", "run", "-d", "--name", NAME, "-p", f"127.0.0.1:{PORT}:8080", "--add-host", "host.docker.internal:host-gateway"]
    for k, v in env.items():
        args += ["-e", f"{k}={v}"]
    r = sh(*args, "lumen-backend:perf")
    if r.returncode:
        return False, r.stderr[-300:]
    return stack.wait_http(f"{URL}/health/ready", timeout=120), ""


async def stream_one(client, headers, q, t_start):
    res = {"events": [], "ended_s": None, "error": None, "status": None}
    try:
        cid = (await client.post(f"{URL}/api/conversations", headers=headers, json={})).json()["id"]
        res["conv"] = cid
        async with client.stream("POST", f"{URL}/api/conversations/{cid}/query", headers=headers, json={"query": q}, timeout=120) as r:
            res["status"] = r.status_code
            async for line in r.aiter_lines():
                if line.startswith("event:"):
                    res["events"].append(line[6:].strip())
    except Exception as e:   # noqa: BLE001 - a cut stream is a data point
        res["error"] = type(e).__name__
    res["ended_s"] = round(time.monotonic() - t_start, 2)
    res["completed"] = "blocks" in res["events"] and "persisted" in res["events"]
    return res


async def scenario(a):
    async with httpx.AsyncClient(timeout=30) as c:
        users = []
        for i in range(a.streams):
            r = await c.post(f"{URL}/api/auth/register", json={"email": f"sig{i}-{uuid.uuid4().hex[:6]}@example.com", "password": "Aa1!" + uuid.uuid4().hex[:12], "name": f"s{i}"})
            users.append({"Authorization": f"Bearer {r.json()['token']}"})
        # warm-up so the first stream does not pay cold-start
        await stream_one(c, users[0], "warm up research of the RBI repo rate", time.monotonic())

    stop_info = {}

    def do_stop():
        t = time.monotonic()
        r = sh("docker", "stop", "-t", str(a.stop_timeout), NAME)
        stop_info["docker_stop_s"] = round(time.monotonic() - t, 2)
        stop_info["rc"] = r.returncode

    limits = httpx.Limits(max_connections=a.streams * 2 + 5)
    async with httpx.AsyncClient(limits=limits, timeout=120) as c:
        t0 = time.monotonic()
        tasks = [asyncio.create_task(stream_one(c, users[i], "Please research the Reserve Bank of India repo rate", t0)) for i in range(a.streams)]
        await asyncio.sleep(a.stop_after_s)          # streams are mid-flight (each LLM call takes llm-delay-ms)
        t_stop = time.monotonic() - t0
        th = threading.Thread(target=do_stop)
        th.start()
        await asyncio.sleep(1.0)
        # A NEW connection while shutting down: should be refused or reset, not accepted and then dropped silently
        new_conn = {}
        t = time.monotonic()
        try:
            async with httpx.AsyncClient(timeout=5) as c2:
                r = await c2.get(f"{URL}/health")
                new_conn = {"result": f"http {r.status_code}", "after_s": round(time.monotonic() - t, 2)}
        except Exception as e:   # noqa: BLE001
            new_conn = {"result": type(e).__name__, "after_s": round(time.monotonic() - t, 2)}
        results = await asyncio.gather(*tasks)
        th.join()
    return results, t_stop, stop_info, new_conn


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--streams", type=int, default=8)
    ap.add_argument("--llm-delay-ms", type=int, default=5000, help="each research makes 2 LLM calls, so a stream lasts about 2x this")
    ap.add_argument("--stop-after-s", type=float, default=3.0)
    ap.add_argument("--stop-timeout", type=int, default=30, help="`docker stop -t`: seconds before SIGKILL (ECS default stopTimeout is 30)")
    a = ap.parse_args()
    prepare()
    ok = False
    try:
        if not start_host_services(a.llm_delay_ms):
            print("host services failed to start")
            return 3
        ok, why = start_backend()
        if not ok:
            lg = sh("docker", "logs", "--tail", "25", NAME)
            print("backend container failed to start", why, (lg.stdout + lg.stderr)[-1500:])
            return 3
        results, t_stop, stop_info, new_conn = asyncio.run(scenario(a))
        exit_code = sh("docker", "inspect", NAME, "--format", "{{.State.ExitCode}}").stdout.strip()
        logs = sh("docker", "logs", NAME)
        shutdown_lines = [ln for ln in (logs.stdout + logs.stderr).splitlines() if any(k in ln.lower() for k in ("shutting down", "shutdown", "finished server", "waiting for"))]
        persisted = 0
        try:
            out = sh("docker", "exec", "lumen-test-pg", "psql", "-U", "lumen_test", "-d", DB, "-tAc", "select count(*) from messages where role='ASSISTANT'").stdout.strip()
            persisted = int(out)
        except Exception:   # noqa: BLE001
            persisted = None
        report = {
            "kind": "LOCAL graceful-shutdown test of the real backend image (Linux container); fake LLM; not production",
            "when": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "setup": {"streams": a.streams, "llm_delay_ms_per_call": a.llm_delay_ms, "sigterm_sent_after_s": round(t_stop, 1), "docker_stop_timeout_s": a.stop_timeout},
            "docker_stop_took_s": stop_info.get("docker_stop_s"), "container_exit_code": exit_code,
            "streams_completed": sum(r["completed"] for r in results), "streams_cut": sum(not r["completed"] for r in results),
            "stream_end_times_s": sorted(r["ended_s"] for r in results), "stream_errors": sorted({r["error"] for r in results if r["error"]}),
            "new_connection_during_shutdown": new_conn,
            "assistant_messages_in_db_including_warmup": persisted,
            "shutdown_log_lines": shutdown_lines[:6],
        }
        OUT.mkdir(parents=True, exist_ok=True)
        path = OUT / f"sigterm-{time.strftime('%Y%m%d-%H%M%S')}.json"
        path.write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(json.dumps(report, indent=2))
        return 0
    finally:
        sh("docker", "rm", "-f", NAME)
        stack.stop_all()
        subprocess.run([str(stack.BACKEND_PY), "-c", "import sqlalchemy as sa;e=sa.create_engine('" + stack.PG_ADMIN +
                        f"',isolation_level='AUTOCOMMIT');c=e.connect();c.execute(sa.text('DROP DATABASE IF EXISTS {DB} WITH (FORCE)'));c.execute(sa.text('DROP ROLE IF EXISTS {ROLE}'))"], capture_output=True)
        sh("docker", "rmi", "-f", "lumen-backend:perf")


if __name__ == "__main__":
    sys.exit(main() if os.name else 1)
