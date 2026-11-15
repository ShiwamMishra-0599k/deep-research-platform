"""Run the same checks as .github/workflows/ci.yml on your machine.

    python scripts/ci_local.py                 # everything
    python scripts/ci_local.py backend lint    # selected jobs: backend agent frontend lint docker security

Prerequisites (see AGENTS.md): Docker running, backend/.venv and research-agent/.venv (Python 3.12), `npm install` in
frontend/, and `uv` on PATH (for the lint/security tools). The test PostgreSQL (127.0.0.1:55432) and Redis (127.0.0.1:56379)
containers are created if missing -- the same ports the CI service containers use, so the test suites run unchanged.

This proves the COMMANDS work. It does not prove the workflow file itself executes on GitHub; only a GitHub run does.
"""
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BIN = "Scripts" if os.name == "nt" else "bin"
EXE = ".exe" if os.name == "nt" else ""
BPY = ROOT / "backend" / ".venv" / BIN / f"python{EXE}"
APY = ROOT / "research-agent" / ".venv" / BIN / f"python{EXE}"
results: list[tuple[str, bool, float]] = []


def run(label, cmd, cwd=ROOT, env=None, expect_fail=False):
    t0 = time.time()
    print(f"\n=== {label}\n$ {' '.join(str(c) for c in cmd)}", flush=True)
    r = subprocess.run([str(c) for c in cmd], cwd=cwd, env={**os.environ, **(env or {})})
    ok = (r.returncode != 0) if expect_fail else (r.returncode == 0)
    results.append((label, ok, time.time() - t0))
    return ok


def docker(*a):
    return subprocess.run(["docker", *a], capture_output=True, text=True)


def ensure_services():
    existing = docker("ps", "-a", "--format", "{{.Names}}").stdout.split()
    if "lumen-test-pg" not in existing:
        docker("run", "-d", "--name", "lumen-test-pg", "-e", "POSTGRES_DB=lumen_test", "-e", "POSTGRES_USER=lumen_test",
               "-e", "POSTGRES_PASSWORD=lumen_test", "-p", "127.0.0.1:55432:5432", "postgres:16-alpine")
    if "lumen-test-redis" not in existing:
        docker("run", "-d", "--name", "lumen-test-redis", "-p", "127.0.0.1:56379:6379", "redis:7-alpine")
    for name in ("lumen-test-pg", "lumen-test-redis"):
        docker("start", name)
    for _ in range(60):
        if docker("exec", "lumen-test-pg", "pg_isready", "-U", "lumen_test").returncode == 0 and \
           docker("exec", "lumen-test-redis", "redis-cli", "ping").stdout.strip() == "PONG":
            return
        time.sleep(1)
    sys.exit("test services did not become ready")


def uvx(tool, *args, cwd=ROOT):
    return ["uvx", tool, *args]


def job_backend():
    run("backend: pytest", [BPY, "-m", "pytest", "-q", "-p", "no:cacheprovider"], cwd=ROOT / "backend")


def job_agent():
    for name, extra in (("requirements.lock", []), ("requirements-dev.lock", ["--group", "dev"])):
        run(f"agent: {name} is up to date with pyproject.toml",
            ["uv", "pip", "compile", "pyproject.toml", *extra, "--universal", "--python-version", "3.12", "--generate-hashes",
             "-o", name], cwd=ROOT / "research-agent")
    run("agent: lockfiles unchanged by regeneration", ["git", "diff", "--exit-code", "--", "requirements.lock", "requirements-dev.lock"],
        cwd=ROOT / "research-agent")
    run("agent: ruff", [APY, "-m", "ruff", "check", "src", "tests"], cwd=ROOT / "research-agent")
    run("agent: pytest", [APY, "-m", "pytest", "-q", "-p", "no:cacheprovider"], cwd=ROOT / "research-agent")


def job_frontend():
    npx = shutil.which("npx") or "npx"
    npm = shutil.which("npm") or "npm"
    run("frontend: tsc", [npx, "--no-install", "tsc", "--noEmit"], cwd=ROOT / "frontend")
    run("frontend: vite build", [npx, "--no-install", "vite", "build"], cwd=ROOT / "frontend")
    run("frontend: vitest with enforced coverage", [npm, "run", "test:coverage"], cwd=ROOT / "frontend")
    run("frontend: npm audit (prod, high)", [npm, "audit", "--omit=dev", "--audit-level=high"], cwd=ROOT / "frontend")


def job_lint():
    run("lint: ruff (backend, e2e, scripts)", uvx("ruff", "check", "--config", "ruff.toml", "backend", "e2e", "scripts"))
    run("lint: workflow passes actionlint", ["uvx", "--from", "actionlint-py", "actionlint.exe", ".github/workflows/ci.yml"])
    run("lint: workflow + dependabot pass the GitHub schemas", ["uvx", "check-jsonschema", "--builtin-schema", "vendor.github-workflows", ".github/workflows/ci.yml"])


def job_docker():
    env = {"JWT_SECRET": "ci-only-placeholder-jwt-0123456789abcdef0123456789", "AGENT_API_KEY": "ci-only-placeholder-agent-key",
           "CSRF_SECRET": "ci-only-placeholder-csrf-0123456789abcdef0123456789",
           "POSTGRES_PASSWORD": "0123456789abcdef0123456789abcdef"}
    agent_env = ROOT / "research-agent" / ".env"
    created = not agent_env.exists()
    if created:
        agent_env.write_text("")
    try:
        run("docker: compose config accepts a full configuration", ["docker", "compose", "config", "-q"], env=env)
        bare = {k: v for k, v in os.environ.items() if k not in env}
        t0 = time.time()
        r = subprocess.run(["docker", "compose", "config", "-q"], cwd=ROOT, env=bare, capture_output=True)
        results.append(("docker: compose REFUSES a config with no secrets", r.returncode != 0, time.time() - t0))
    finally:
        if created:
            agent_env.unlink()
    run("docker: build backend image", ["docker", "build", "-q", "-t", "lumen-backend:ci", "backend"])
    run("docker: build agent image", ["docker", "build", "-q", "-t", "lumen-agent:ci", "research-agent"])
    for img in ("lumen-backend:ci", "lumen-agent:ci"):
        uid = docker("run", "--rm", "--entrypoint", "id", img, "-u").stdout.strip()
        results.append((f"docker: {img} runs as non-root (uid {uid})", uid not in ("", "0"), 0.0))
    docker("rmi", "-f", "lumen-backend:ci", "lumen-agent:ci")


def job_security():
    run("security: bandit", uvx("bandit", "-q", "-r", "backend/app", "backend/alembic", "research-agent/src"))
    run("security: pip-audit backend", uvx("pip-audit", "-r", "backend/requirements.txt", "--no-deps", "--disable-pip",
                                           "--ignore-vuln", "PYSEC-2026-4298"))
    run("security: pip-audit agent (locked set)", uvx("pip-audit", "-r", "research-agent/requirements.lock", "--no-deps", "--disable-pip"))


JOBS = {"backend": job_backend, "agent": job_agent, "frontend": job_frontend, "lint": job_lint,
        "docker": job_docker, "security": job_security}


def main():
    wanted = sys.argv[1:] or list(JOBS)
    unknown = [w for w in wanted if w not in JOBS]
    if unknown:
        sys.exit(f"unknown job(s) {unknown}; choose from {list(JOBS)}")
    if any(w in wanted for w in ("backend", "agent")):
        ensure_services()
    for w in wanted:
        JOBS[w]()
    print("\n=== summary")
    for label, ok, secs in results:
        print(f"{'PASS' if ok else 'FAIL'}  {label}  ({secs:.0f}s)")
    failed = [r for r in results if not r[1]]
    print(f"\n{len(results) - len(failed)}/{len(results)} steps passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
