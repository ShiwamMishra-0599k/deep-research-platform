"""Run Lumen locally with NO API keys and NO accounts: a scripted language model and a sealed fake internet.

    backend/.venv/Scripts/python.exe scripts/demo.py          # Ctrl+C stops everything and removes the demo containers

What is REAL: the React UI, FastAPI backend (cookie sessions, CSRF, rate limits), PostgreSQL, Redis, the research agent (LangGraph with its eight tools, block formatter), SSE.
What is SCRIPTED: the language model (e2e/fake_llm.py chooses tools by keyword and writes a canned answer) and every third-party website/API (e2e/run_agent_fake_internet.py).
So the demo shows the product's behaviour (streaming progress, tables, maps, sources, sign-in, history), NOT the quality of a real model's research. To see a real model, point the agent at a
local Ollama server (docs/ZERO_BUDGET_PLAN.md) or at a provider you have a key for.

Needs: Docker running, `backend/.venv` and `research-agent/.venv` (see README), `npm install` done in frontend/. Creates two throwaway containers (lumen-demo-pg on 55434,
lumen-demo-redis on 56381, loopback only, random password, deleted on exit) and never touches any other container or volume. Secrets are random per run and never stored.
"""
import atexit
import os
import secrets
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "e2e" / "out"
IS_WIN = os.name == "nt"
BIN = "Scripts" if IS_WIN else "bin"
BACKEND_PY = ROOT / "backend" / ".venv" / BIN / ("python.exe" if IS_WIN else "python")
AGENT_PY = ROOT / "research-agent" / ".venv" / BIN / ("python.exe" if IS_WIN else "python")
PG, REDIS_C = "lumen-demo-pg", "lumen-demo-redis"
P_PG, P_REDIS, P_LLM, P_AGENT, P_API, P_WEB = 55434, 56381, 18999, 18004, 18080, 15173

procs: list[subprocess.Popen] = []


def sh(*cmd, **kw):
    return subprocess.run([str(c) for c in cmd], capture_output=True, text=True, **kw)


def start(name, cmd, env_extra, cwd):
    OUT.mkdir(parents=True, exist_ok=True)
    log = (OUT / f"demo_{name}.log").open("w", encoding="utf-8")
    p = subprocess.Popen([str(c) for c in cmd], cwd=cwd, env={**os.environ, **env_extra, "PYTHONUNBUFFERED": "1", "E2E_OUT": str(OUT)}, stdout=log, stderr=subprocess.STDOUT)
    procs.append(p)
    return p


def wait_http(url, timeout=60):
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            with urllib.request.urlopen(url, timeout=3) as r:
                if r.status == 200:
                    return True
        except Exception:
            time.sleep(0.5)
    return False


def cleanup():
    for p in procs:
        if IS_WIN:
            subprocess.run(["taskkill", "/PID", str(p.pid), "/T", "/F"], capture_output=True)
        else:
            p.terminate()
    sh("docker", "rm", "-f", PG, REDIS_C)


def main() -> int:
    for py in (BACKEND_PY, AGENT_PY):
        if not py.exists():
            print(f"missing virtualenv interpreter: {py}\nSee the README (local development) to create backend/.venv and research-agent/.venv.")
            return 2
    if not (ROOT / "frontend" / "node_modules").exists():
        print("frontend/node_modules is missing: run `npm install` in frontend/ first.")
        return 2
    if sh("docker", "info").returncode:
        print("Docker is not running: start Docker Desktop first.")
        return 2

    pw = secrets.token_hex(16)                       # random per run, never stored
    jwt_secret, csrf_secret, agent_key = secrets.token_hex(32), secrets.token_hex(32), secrets.token_hex(24)
    db_url = f"postgresql+psycopg://lumen:{pw}@127.0.0.1:{P_PG}/lumen"
    atexit.register(cleanup)
    sh("docker", "rm", "-f", PG, REDIS_C)            # only these two names, only ours
    for cmd in (
        ["docker", "run", "-d", "--name", PG, "-e", "POSTGRES_DB=lumen", "-e", "POSTGRES_USER=lumen", "-e", f"POSTGRES_PASSWORD={pw}", "-p", f"127.0.0.1:{P_PG}:5432", "postgres:16-alpine"],
        ["docker", "run", "-d", "--name", REDIS_C, "-p", f"127.0.0.1:{P_REDIS}:6379", "redis:7-alpine", "--maxmemory", "64mb", "--maxmemory-policy", "noeviction"],
    ):
        r = sh(*cmd)
        if r.returncode:
            print("could not start a demo container:", r.stderr.strip()[-300:])
            return 3
    for _ in range(60):
        if sh("docker", "exec", PG, "pg_isready", "-U", "lumen").returncode == 0 and sh("docker", "exec", REDIS_C, "redis-cli", "ping").stdout.strip() == "PONG":
            break
        time.sleep(1)
    else:
        print("the demo database or Redis did not become ready")
        return 3

    backend_env = {
        "DATABASE_URL": db_url, "JWT_SECRET": jwt_secret, "CSRF_SECRET": csrf_secret, "AGENT_API_KEY": agent_key,
        "RESEARCH_AGENT_URL": f"http://127.0.0.1:{P_AGENT}", "CORS_ALLOWED_ORIGINS": f"http://127.0.0.1:{P_WEB}",
        "REDIS_URL": f"redis://127.0.0.1:{P_REDIS}/1", "RESEARCH_TIMEOUT_SECONDS": "120",
    }
    agent_env = {
        "REDIS_URL": f"redis://127.0.0.1:{P_REDIS}/0", "AGENT_API_KEY": agent_key, "OPENROUTER_API_KEY": "scripted-model-key",
        "OPENROUTER_BASE_URL": f"http://127.0.0.1:{P_LLM}/v1", "OPENAI_API_KEY": "fake-openai-key", "LOG_LEVEL": "WARNING",
    }
    if subprocess.run([str(BACKEND_PY), "-m", "app.migrate"], cwd=ROOT / "backend", env={**os.environ, **backend_env}, capture_output=True).returncode:
        print("database migration failed; see the output of `python -m app.migrate` in backend/")
        return 3

    start("llm", [AGENT_PY, ROOT / "e2e" / "fake_llm.py", P_LLM], {}, ROOT)
    start("agent", [AGENT_PY, ROOT / "e2e" / "run_agent_fake_internet.py", P_AGENT], agent_env, ROOT / "research-agent")
    start("backend", [BACKEND_PY, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", P_API], backend_env, ROOT / "backend")
    npx = shutil.which("npx") or "npx"
    start("frontend", [npx, "vite", "--host", "127.0.0.1", "--port", P_WEB, "--strictPort"], {"VITE_PROXY_TARGET": f"http://127.0.0.1:{P_API}"}, ROOT / "frontend")
    ok = (wait_http(f"http://127.0.0.1:{P_LLM}/health") and wait_http(f"http://127.0.0.1:{P_AGENT}/health", timeout=240)
          and wait_http(f"http://127.0.0.1:{P_API}/health/ready", timeout=120) and wait_http(f"http://127.0.0.1:{P_WEB}/", timeout=90))
    if not ok:
        print("the stack did not start; see e2e/out/demo_*.log")
        return 3
    print(f"""
==================================================================================================
 LUMEN DEMO  -  open  http://127.0.0.1:{P_WEB}/   (register any account; nothing leaves this machine)

 SCRIPTED DEMO: the language model and the internet are simulated.
 You are seeing the real application behaviour (streaming, tables, maps, sign-in, history),
 NOT the quality of a real model's research. Try: "Bitcoin price in INR", "USD to INR", "RBI news",
 "research the RBI repo rate" (a table plus cited sources), "EV charging stations in Lucknow" (a map).
 Press Ctrl+C here to stop and delete the demo containers.
==================================================================================================""", flush=True)
    try:
        while True:
            time.sleep(2)
            dead = [p for p in procs if p.poll() is not None]
            if dead:
                print("a demo process exited; see e2e/out/demo_*.log")
                return 4
    except KeyboardInterrupt:
        print("\nstopping ...")
        return 0


if __name__ == "__main__":
    sys.exit(main())
