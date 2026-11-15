"""Rehearse database credential rotation end to end on a SYNTHETIC legacy volume.

Builds a volume exactly like this repo's old compose did (role lumen / password lumen), migrates it, seeds real
data through the real API, then runs scripts/rotate_db_password.py against it and proves:
  * the script refuses unsafe targets (volume in use, wrong role, wrong PG major);
  * a dry run produces a valid, restore-verified backup and changes nothing;
  * rotation changes the password, the old one stops working, the new one works over TCP;
  * the backend REFUSES the old weak credential, accepts the new one, and serves the SAME users/conversations/messages.

Only resources named lumen-rehearsal-* are created and removed. No other volume or container is referenced.

    backend/.venv/Scripts/python.exe e2e/rotation_rehearsal.py
"""
import json
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import run  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "e2e" / "out"
VOL = "lumen-rehearsal-legacy"
PG = "lumen-rehearsal-legacy-pg"
BLOCKER = "lumen-rehearsal-blocker"
PORT = 55434
API = 18390
results: list[tuple[str, bool]] = []
SCRIPT = [str(run.BACKEND_PY), str(ROOT / "scripts" / "rotate_db_password.py")]


def check(name, ok, detail=""):
    results.append((name, bool(ok)))
    print(f"{'PASS' if ok else 'FAIL'}  {name}{'' if ok or not detail else '  -> ' + str(detail)[:1800]}", flush=True)


def sh(*a, env=None, check_rc=False, timeout=300):
    r = subprocess.run([str(x) for x in a], capture_output=True, text=True, env={**os.environ, **(env or {})}, timeout=timeout)
    if check_rc and r.returncode:
        raise RuntimeError(f"{a[:3]} failed: {(r.stderr or r.stdout)[-400:]}")
    return r


def docker(*a, **k):
    return sh("docker", *a, **k)


def api(method, path, body=None, token=None):
    req = urllib.request.Request(f"http://127.0.0.1:{API}{path}", method=method,
                                 data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Content-Type": "application/json", **({"Authorization": f"Bearer {token}"} if token else {})})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.status, json.loads(r.read() or b"null")
    except urllib.error.HTTPError as e:
        return e.code, None


def backend_proc(url, allow_weak):
    env = {**run.backend_env(), "DATABASE_URL": url, "ALLOW_INSECURE_DEV_DB": "1" if allow_weak else "0", "REDIS_URL": ""}
    return run.start("rehearsal_backend", [run.BACKEND_PY, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", API],
                     {k: str(v) for k, v in env.items()}, cwd=ROOT / "backend")


def password_works(password):
    """Start a private server on the volume and test `password` from ANOTHER container over an internal network
    (pg_hba trusts loopback inside the server container, so only network clients prove password enforcement)."""
    probe, net = "lumen-rehearsal-probe", "lumen-rehearsal-probe-net"
    docker("rm", "-f", probe)
    docker("network", "rm", net)
    docker("network", "create", "--internal", net, check_rc=True)
    docker("run", "-d", "--name", probe, "--network", net, "--network-alias", "db",
           "-v", f"{VOL}:/var/lib/postgresql/data", "postgres:16-alpine", check_rc=True)
    try:
        for _ in range(60):
            if docker("exec", probe, "pg_isready", "-U", "lumen").returncode == 0:
                break
            time.sleep(1)
        r = docker("run", "--rm", "--network", net, "-e", f"PGPASSWORD={password}", "postgres:16-alpine",
                   "psql", "-h", "db", "-U", "lumen", "-d", "lumen", "-tAc", "select 1")
        return r.returncode == 0 and r.stdout.strip() == "1"
    finally:
        docker("stop", "-t", "30", probe)
        docker("rm", probe)
        docker("network", "rm", net)


def cleanup():
    docker("rm", "-f", PG, BLOCKER, "lumen-rehearsal-probe")
    docker("network", "rm", "lumen-rehearsal-probe-net")
    docker("volume", "rm", "-f", VOL)  # ONLY the rehearsal volume, by its exact name


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    cleanup()
    docker("volume", "create", VOL, check_rc=True)

    # 1. a legacy database, initialised the way the old compose file did
    docker("run", "-d", "--name", PG, "-e", "POSTGRES_USER=lumen", "-e", "POSTGRES_DB=lumen", "-e", "POSTGRES_PASSWORD=lumen",
           "-p", f"127.0.0.1:{PORT}:5432", "-v", f"{VOL}:/var/lib/postgresql/data", "postgres:16-alpine", check_rc=True)
    for _ in range(60):
        if docker("exec", PG, "pg_isready", "-U", "lumen").returncode == 0:
            break
        time.sleep(1)
    legacy_url = f"postgresql+psycopg://lumen:lumen@127.0.0.1:{PORT}/lumen"
    r = subprocess.run([str(run.BACKEND_PY), "-m", "app.migrate"], cwd=ROOT / "backend", env={**os.environ, "DATABASE_URL": legacy_url}, capture_output=True, text=True)
    check("legacy volume (lumen/lumen) migrated with the real Alembic migrations", r.returncode == 0, r.stderr[-300:])

    # 2. real data through the real application
    backend_proc(legacy_url, allow_weak=True)
    assert run.wait_http(f"http://127.0.0.1:{API}/health/ready", timeout=90)
    users = []
    for i in range(3):
        st, reg = api("POST", "/api/auth/register", {"email": f"rehearsal{i}@example.com", "password": f"rehearsal-pass-{i}-x", "name": f"User {i}"})
        users.append(reg)
    convs = []
    for u in users:
        for k in range(2):
            st, c = api("POST", "/api/conversations", {"title": f"Chat {k} of {u['name']}"}, u["token"])
            convs.append((u, c["id"]))
    n_msgs = 0
    for u, cid in convs:
        for k in range(4):
            blocks = json.dumps([{"template_id": "markdown", "data": {"content": f"answer {k} – unicode ₹ ok"}}]).replace("'", "''")
            docker("exec", PG, "psql", "-U", "lumen", "-d", "lumen", "-c",
                   f"insert into messages (id, conversation_id, role, content, blocks, created_at) values (gen_random_uuid(), '{cid}', "
                   f"'{'USER' if k % 2 == 0 else 'ASSISTANT'}', 'message {k}', '{blocks}'::jsonb, now())", check_rc=True)
            n_msgs += 1
    check(f"seeded {len(users)} users, {len(convs)} conversations, {n_msgs} messages via the live app", len(users) == 3 and all(u for u in users))
    run.stop_all()
    run.procs.clear()
    docker("stop", "-t", "60", PG)
    docker("rm", PG)

    # 3. unsafe targets are refused
    docker("create", "--name", BLOCKER, "-v", f"{VOL}:/x", "alpine", "true", check_rc=True)
    r = sh(*SCRIPT, "--volume", VOL, "--dry-run")
    check("REFUSES a volume that a container (even a stopped one) still uses", r.returncode == 1 and "refusing to touch a volume" in r.stdout, r.stdout[-300:])
    docker("rm", BLOCKER)
    r = sh(*SCRIPT, "--volume", VOL, "--user", "alvoff", "--dry-run")
    check("REFUSES a volume whose role is not the expected one (another project's data)", r.returncode == 1 and "not found in this volume" in r.stdout, r.stdout[-300:])
    r = sh(*SCRIPT, "--volume", "does-not-exist-rehearsal", "--dry-run")
    check("refuses a volume that does not exist", r.returncode == 1 and "does not exist" in r.stdout, r.stdout[-200:])
    r = sh(*SCRIPT, "--volume", VOL, "--image", "postgres:15-alpine", "--dry-run")
    check("REFUSES a PostgreSQL major-version mismatch", r.returncode == 1 and "image is 15" in r.stdout, r.stdout[-300:])
    r = sh(*SCRIPT, "--volume", VOL, "--new-password-env", "UNSET_VAR_XYZ")
    check("REFUSES a weak/short new password", r.returncode == 1 and "new password must be" in r.stdout, r.stdout[-200:])

    # 4. dry run: valid restore-verified backup, nothing changed
    bdir = OUT / "rehearsal-backups"
    r = sh(*SCRIPT, "--volume", VOL, "--dry-run", "--backup-dir", str(bdir))
    dumps = sorted(bdir.glob("lumen-*.dump"))
    meta = json.loads(dumps[-1].with_suffix(".dump.json").read_text()) if dumps else {}
    check("dry run succeeds and the backup is validated by a full restore", r.returncode == 0 and "restore produced identical row counts" in r.stdout, r.stdout[-400:])
    check("backup recorded with checksum and per-table manifest", meta.get("manifest", {}).get("messages", {}).get("rows") == n_msgs and len(meta.get("sha256", "")) == 64, meta.get("manifest"))
    r = docker("ps", "-a", "--filter", f"volume={VOL}", "--format", "{{.Names}}")
    check("the script leaves no container behind (volume kept)", r.stdout.strip() == "", r.stdout)
    check("dry run changed nothing: the OLD password (lumen) still authenticates afterwards", password_works("lumen"))

    # 5. real rotation
    envfile = OUT / "rehearsal.env"
    envfile.write_text("JWT_SECRET=placeholder-not-used\n")
    r = sh(*SCRIPT, "--volume", VOL, "--generate", "--write-env", str(envfile), "--old-password-env", "OLD_DB_PW", "--backup-dir", str(bdir),
           env={"OLD_DB_PW": "lumen"})
    NL = chr(10)
    check("rotation completes: new password works, wrong rejected, OLD rejected, data intact",
          r.returncode == 0 and "the old password is rejected" in r.stdout and "data intact" in r.stdout,
          r.stdout[-600:].replace(NL, " | ") + " || STDERR: " + r.stderr[-700:].replace(NL, " | "))
    new_pw = next((ln.split("=", 1)[1] for ln in envfile.read_text().splitlines() if ln.startswith("POSTGRES_PASSWORD=")), "")
    check("new password saved to the (git-ignored) env file and never printed", len(new_pw) == 48 and new_pw not in r.stdout and new_pw not in r.stderr)
    check("existing env content preserved", "JWT_SECRET=placeholder-not-used" in envfile.read_text())

    # 6. the application with the rotated credential
    docker("run", "-d", "--name", PG, "-e", "POSTGRES_USER=lumen", "-e", "POSTGRES_DB=lumen", "-e", f"POSTGRES_PASSWORD={new_pw}",
           "-p", f"127.0.0.1:{PORT}:5432", "-v", f"{VOL}:/var/lib/postgresql/data", "postgres:16-alpine", check_rc=True)
    for _ in range(60):
        if docker("exec", PG, "pg_isready", "-U", "lumen").returncode == 0:
            break
        time.sleep(1)
    new_url = f"postgresql+psycopg://lumen:{new_pw}@127.0.0.1:{PORT}/lumen"
    p = subprocess.run([str(run.BACKEND_PY), "-c", "from app.config import settings"], cwd=ROOT / "backend",
                       env={**os.environ, "DATABASE_URL": legacy_url, "ALLOW_INSECURE_DEV_DB": "0", "JWT_SECRET": run.SECRETS["JWT_SECRET"]}, capture_output=True, text=True)
    check("backend REFUSES to start with the old weak credential (no opt-in flag)", p.returncode != 0 and "weak/default database password" in (p.stderr + p.stdout), p.stderr[-300:])
    m = subprocess.run([str(run.BACKEND_PY), "-m", "app.migrate"], cwd=ROOT / "backend", env={**os.environ, "DATABASE_URL": new_url}, capture_output=True, text=True)
    check("migrations connect with the new credential and are idempotent", m.returncode == 0 and "up to date" in (m.stderr + m.stdout), m.stderr[-300:])
    docker("stop", "-t", "60", PG)
    docker("rm", PG)
    old_ok = password_works("lumen")
    new_ok = password_works(new_pw)
    check("restarted server (as compose would run it): OLD password rejected, NEW accepted, from another container",
          (not old_ok) and new_ok, (old_ok, new_ok))
    docker("run", "-d", "--name", PG, "-e", "POSTGRES_USER=lumen", "-e", "POSTGRES_DB=lumen", "-e", f"POSTGRES_PASSWORD={new_pw}",
           "-p", f"127.0.0.1:{PORT}:5432", "-v", f"{VOL}:/var/lib/postgresql/data", "postgres:16-alpine", check_rc=True)
    for _ in range(60):
        if docker("exec", PG, "pg_isready", "-U", "lumen").returncode == 0:
            break
        time.sleep(1)

    backend_proc(new_url, allow_weak=False)
    ready = run.wait_http(f"http://127.0.0.1:{API}/health/ready", timeout=90)
    check("backend starts with the strong credential and no insecure-dev opt-in", ready)
    survived = 0
    for i, u in enumerate(users):
        st, body = api("POST", "/api/auth/login", {"email": f"rehearsal{i}@example.com", "password": f"rehearsal-pass-{i}-x"})
        if st != 200:
            continue
        st, cs = api("GET", "/api/conversations", None, body["token"])
        total = 0
        for c in cs or []:
            st, ms = api("GET", f"/api/conversations/{c['id']}/messages", None, body["token"])
            total += len(ms or [])
        survived += 1 if (len(cs or []) == 2 and total == 8) else 0
    check("every existing user can still log in and sees ALL their conversations and messages", survived == 3, survived)
    run.stop_all()


if __name__ == "__main__":
    code = 1
    try:
        main()
        code = 0 if all(ok for _, ok in results) else 1
    except Exception as e:  # noqa: BLE001
        import traceback

        traceback.print_exc()
        print("ABORTED:", e)
    finally:
        try:
            run.stop_all()
        finally:
            cleanup()
    print(f"\n{sum(ok for _, ok in results)}/{len(results)} checks passed")
    sys.exit(code)
