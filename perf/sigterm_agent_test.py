"""Graceful-shutdown test for the RESEARCH AGENT image: run it in Linux, stream researches, send SIGTERM (`docker stop`), observe.

    research-agent/.venv/Scripts/python.exe perf/sigterm_agent_test.py [--streams 6] [--llm-delay-ms 4000] [--stop-timeout 30]

The agent container talks to the scripted model on the host; the queries ask for a clarification, so no tool runs and nothing needs the internet.
Reports how long `docker stop` took, the exit code, whether in-flight streams completed, and whether a new connection was refused. Spends nothing.
"""
import argparse
import asyncio
import json
import secrets
import subprocess
import sys
import threading
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "e2e"))
import run as stack  # noqa: E402

NAME = "lumen-sigterm-agent"
PORT = 58004
URL = f"http://127.0.0.1:{PORT}"
OUT = ROOT / "perf" / "results"


def sh(*cmd, **kw):
    return subprocess.run([str(c) for c in cmd], capture_output=True, text=True, **kw)


async def one_stream(client, key, q, t0):
    res = {"events": [], "error": None}
    try:
        async with client.stream("POST", f"{URL}/research", headers={"X-Internal-Token": key}, json={"query": q}, timeout=120) as r:
            res["status"] = r.status_code
            async for line in r.aiter_lines():
                if line.startswith("event:"):
                    res["events"].append(line[6:].strip())
    except Exception as e:   # noqa: BLE001 - a cut stream is a data point
        res["error"] = type(e).__name__
    res["ended_s"] = round(time.monotonic() - t0, 2)
    res["completed"] = "done" in res["events"]
    return res


async def scenario(a, key):
    stop = {}

    def do_stop():
        t = time.monotonic()
        sh("docker", "stop", "-t", str(a.stop_timeout), NAME)
        stop["took_s"] = round(time.monotonic() - t, 2)

    async with httpx.AsyncClient(timeout=120) as c:
        await one_stream(c, key, "clarify please", time.monotonic())            # warm-up (imports, first model call)
        t0 = time.monotonic()
        tasks = [asyncio.create_task(one_stream(c, key, "clarify please", t0)) for _ in range(a.streams)]
        await asyncio.sleep(a.stop_after_s)
        th = threading.Thread(target=do_stop)
        th.start()
        await asyncio.sleep(1.0)
        try:
            async with httpx.AsyncClient(timeout=5) as c2:
                new_conn = f"http {(await c2.get(f'{URL}/health')).status_code}"
        except Exception as e:   # noqa: BLE001
            new_conn = type(e).__name__
        results = await asyncio.gather(*tasks)
        th.join()
    return results, stop, new_conn


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--streams", type=int, default=6)
    ap.add_argument("--llm-delay-ms", type=int, default=4000)
    ap.add_argument("--stop-after-s", type=float, default=2.0)
    ap.add_argument("--stop-timeout", type=int, default=30)
    ap.add_argument("--diagnose-after", type=float, default=None, help="seconds after SIGTERM to send SIGABRT (faulthandler thread dump)")
    a = ap.parse_args()
    key = secrets.token_hex(16)
    try:
        stack.start("sigagent_llm", [stack.AGENT_PY, ROOT / "e2e" / "fake_llm.py", stack.P_LLM], {"FAKE_LLM_DELAY_MS": str(a.llm_delay_ms)})
        if not stack.wait_http(f"http://127.0.0.1:{stack.P_LLM}/health"):
            print("scripted model did not start")
            return 3
        sh("docker", "rm", "-f", NAME)
        b = sh("docker", "build", "-q", "-t", "lumen-agent:perf", "research-agent", cwd=ROOT)
        if b.returncode:
            print("build failed", b.stderr[-300:])
            return 3
        env = {"AGENT_API_KEY": key, "OPENROUTER_API_KEY": "scripted", "OPENROUTER_BASE_URL": f"http://host.docker.internal:{stack.P_LLM}/v1",
               "REDIS_URL": "redis://host.docker.internal:56379/11", "LOG_LEVEL": "INFO"}
        if a.diagnose_after is not None:
            env["PYTHONFAULTHANDLER"] = "1"
        args = ["docker", "run", "-d", "--name", NAME, "-p", f"127.0.0.1:{PORT}:8004", "--add-host", "host.docker.internal:host-gateway"]
        for k, v in env.items():
            args += ["-e", f"{k}={v}"]
        r = sh(*args, "lumen-agent:perf")
        if r.returncode or not stack.wait_http(f"{URL}/health", timeout=120):
            lg = sh("docker", "logs", "--tail", "20", NAME)
            print("agent container failed to start", r.stderr[-200:], (lg.stdout + lg.stderr)[-800:])
            return 3
        if a.diagnose_after is not None:
            threading.Timer(a.stop_after_s + a.diagnose_after, lambda: sh("docker", "kill", "-s", "ABRT", NAME)).start()
        results, stop, new_conn = asyncio.run(scenario(a, key))
        code = sh("docker", "inspect", NAME, "--format", "{{.State.ExitCode}}").stdout.strip()
        logs = sh("docker", "logs", NAME)
        text = (logs.stdout + logs.stderr).splitlines()
        report = {
            "kind": "LOCAL graceful-shutdown test of the real agent image (Linux container); scripted model; not production",
            "when": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "setup": {"streams": a.streams, "llm_delay_ms_per_call": a.llm_delay_ms, "sigterm_after_s": a.stop_after_s, "docker_stop_timeout_s": a.stop_timeout},
            "docker_stop_took_s": stop.get("took_s"), "container_exit_code": code,
            "streams_completed": sum(r["completed"] for r in results), "streams_cut": sum(not r["completed"] for r in results),
            "stream_end_times_s": sorted(r["ended_s"] for r in results), "stream_errors": sorted({r["error"] for r in results if r["error"]}),
            "new_connection_during_shutdown": new_conn,
            "shutdown_log_lines": [ln for ln in text if any(k in ln.lower() for k in ("shutting down", "shutdown", "finished server", "waiting for", "cancel"))][:10],
            "last_log_lines": text[-(50 if a.diagnose_after is not None else 6):],
        }
        OUT.mkdir(parents=True, exist_ok=True)
        (OUT / f"sigterm-agent-{time.strftime('%Y%m%d-%H%M%S')}.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(json.dumps(report, indent=2))
        return 0
    finally:
        sh("docker", "rm", "-f", NAME)
        stack.stop_all()
        sh("docker", "rmi", "-f", "lumen-agent:perf")


if __name__ == "__main__":
    sys.exit(main())
