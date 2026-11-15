"""Start the full stack for integration testing, run the browser scenario, tear everything down.

REAL:  React/Vite dev server, FastAPI backend, PostgreSQL, research agent (LangGraph + 8 tools), Redis, SSE.
FAKE:  the LLM (e2e/fake_llm.py) and third-party websites/APIs (e2e/run_agent_fake_internet.py).

Prerequisites (see AGENTS.md): isolated test containers on 127.0.0.1:55432 (postgres) and :56379 (redis),
backend/.venv and research-agent/.venv, `npm install` done in frontend/, Microsoft Edge or Chrome.

    python e2e/run.py            # prints PASS/FAIL per check, exits non-zero on failure
"""
import os
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "e2e" / "out"
OUT.mkdir(parents=True, exist_ok=True)
IS_WIN = os.name == "nt"
BIN = "Scripts" if IS_WIN else "bin"
BACKEND_PY = ROOT / "backend" / ".venv" / BIN / ("python.exe" if IS_WIN else "python")
AGENT_PY = ROOT / "research-agent" / ".venv" / BIN / ("python.exe" if IS_WIN else "python")

PG_ADMIN = "postgresql+psycopg://lumen_test:lumen_test@127.0.0.1:55432/lumen_test"
E2E_DB = "postgresql+psycopg://lumen_test:lumen_test@127.0.0.1:55432/lumen_e2e_test"
REDIS = "redis://127.0.0.1:56379/2"          # agent tool cache
RATE_LIMIT_REDIS = "redis://127.0.0.1:56379/5"  # backend rate limits (shared by every backend instance)
P_LLM, P_AGENT, P_API, P_WEB = 18999, 18004, 18080, 15173
SECRETS = {"JWT_SECRET": "e2e" + "Xq7" * 14, "AGENT_API_KEY": "e2e-agent-key-" + "k3" * 12, "CSRF_SECRET": "e2ecsrf" + "Mn4" * 14}

AGENT_ENV = {
    "REDIS_URL": REDIS, "AGENT_API_KEY": SECRETS["AGENT_API_KEY"],
    "OPENROUTER_API_KEY": "fake-key", "OPENROUTER_BASE_URL": f"http://127.0.0.1:{P_LLM}/v1",
    "OPENAI_API_KEY": "fake-openai-key", "LOG_LEVEL": "WARNING",
}


def backend_env(**extra):
    return {
        "DATABASE_URL": E2E_DB, "ALLOW_INSECURE_DEV_DB": "1", "JWT_SECRET": SECRETS["JWT_SECRET"], "CSRF_SECRET": SECRETS["CSRF_SECRET"], "AGENT_API_KEY": SECRETS["AGENT_API_KEY"],
        "RESEARCH_AGENT_URL": f"http://127.0.0.1:{P_AGENT}", "CORS_ALLOWED_ORIGINS": f"http://127.0.0.1:{P_WEB}",
        "QUERIES_PER_USER_PER_MINUTE": "60", "RESEARCH_TIMEOUT_SECONDS": "120",
        # Real Redis-backed limits. Per-IP limits are raised because every simulated user shares 127.0.0.1.
        "REDIS_URL": RATE_LIMIT_REDIS, "REGISTER_PER_IP_PER_HOUR": "1000", "LOGIN_PER_IP_PER_MINUTE": "1000",
        "API_REQUESTS_PER_USER_PER_MINUTE": "5000", **{k: str(v) for k, v in extra.items()},
    }


procs: list[subprocess.Popen] = []


def start(name, cmd, env_extra=None, cwd=None):
    env = {**os.environ, **(env_extra or {}), "PYTHONUNBUFFERED": "1", "E2E_OUT": str(OUT)}
    log = (OUT / f"{name}.log").open("w", encoding="utf-8")
    p = subprocess.Popen([str(c) for c in cmd], cwd=cwd or ROOT, env=env, stdout=log, stderr=subprocess.STDOUT)
    procs.append(p)
    return p


def wait_http(url, timeout=60, headers=None, ok=(200,)):
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            req = urllib.request.Request(url, headers=headers or {})
            with urllib.request.urlopen(req, timeout=3) as r:
                if r.status in ok:
                    return True
        except Exception:
            time.sleep(0.5)
    return False


def prepare_database():
    sys.path.insert(0, str(ROOT / "backend"))
    code = (
        "import sqlalchemy as sa,sys;"
        f"e=sa.create_engine('{PG_ADMIN}',isolation_level='AUTOCOMMIT');"
        "c=e.connect();"
        "c.execute(sa.text('DROP DATABASE IF EXISTS lumen_e2e_test'));"  # only the e2e database, never the project's
        "c.execute(sa.text('CREATE DATABASE lumen_e2e_test'))"
    )
    subprocess.run([str(BACKEND_PY), "-c", code], check=True)
    subprocess.run([str(BACKEND_PY), "-m", "app.migrate"], check=True, cwd=ROOT / "backend",
                   env={**os.environ, "DATABASE_URL": E2E_DB})
    subprocess.run([str(AGENT_PY), "-c",
                    f"import redis;[redis.Redis.from_url(u).flushdb() for u in ('{REDIS}', '{RATE_LIMIT_REDIS}')]"], check=True)


def stop_all() -> None:
    """Kill every spawned process AND its children (npx -> node would otherwise survive)."""
    for p in procs:
        if IS_WIN:
            subprocess.run(["taskkill", "/PID", str(p.pid), "/T", "/F"], capture_output=True)
        else:
            p.terminate()
    for p in procs:
        try:
            p.wait(timeout=10)
        except Exception:
            p.kill()


def main() -> int:
    for p in (BACKEND_PY, AGENT_PY):
        if not p.exists():
            print(f"missing virtualenv interpreter: {p}")
            return 2
    prepare_database()
    for f in OUT.glob("blocked-requests.log"):
        f.unlink()

    start("fake_llm", [AGENT_PY, ROOT / "e2e" / "fake_llm.py", P_LLM])
    start("agent", [AGENT_PY, ROOT / "e2e" / "run_agent_fake_internet.py", P_AGENT], AGENT_ENV, cwd=ROOT / "research-agent")
    start("backend", [BACKEND_PY, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", P_API],
          backend_env(), cwd=ROOT / "backend")
    npx = shutil.which("npx") or "npx"
    start("frontend", [npx, "vite", "--host", "127.0.0.1", "--port", P_WEB, "--strictPort"],
          {"VITE_PROXY_TARGET": f"http://127.0.0.1:{P_API}"}, cwd=ROOT / "frontend")  # same origin: Vite proxies /api, no VITE_API_URL

    ok = (
        wait_http(f"http://127.0.0.1:{P_LLM}/health")
        and wait_http(f"http://127.0.0.1:{P_AGENT}/health", timeout=240)  # heavy imports: slow cold start
        and wait_http(f"http://127.0.0.1:{P_API}/health/ready", timeout=120)
        and wait_http(f"http://127.0.0.1:{P_WEB}/", timeout=90)
    )
    if not ok:
        print("stack failed to start; see e2e/out/*.log")
        return 3
    print("stack up: fake-llm, agent, backend, frontend (real PostgreSQL + Redis)\n")

    node = shutil.which("node") or "node"
    r = subprocess.run([node, str(ROOT / "e2e" / "scenario.mjs")], cwd=ROOT,
                       env={**os.environ, "E2E_APP": f"http://127.0.0.1:{P_WEB}", "E2E_OUT": str(OUT)})

    blocked = OUT / "blocked-requests.log"
    leaked = blocked.read_text().strip() if blocked.exists() else ""
    print("\nHERMETIC CHECK (agent sealed at socket level):", "PASS - no request left the machine" if not leaked else "FAIL\n" + leaked)
    return r.returncode or (1 if leaked else 0)


if __name__ == "__main__":
    try:
        code = main()
    finally:
        stop_all()
    sys.exit(code)
