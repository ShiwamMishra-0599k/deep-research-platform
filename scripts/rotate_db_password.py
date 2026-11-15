"""Rotate the PostgreSQL role password of an ALREADY INITIALISED data volume, safely.

Why this exists: POSTGRES_PASSWORD in an env file is applied only when a data directory is first created.
Editing it later changes nothing inside an existing database, and the backend would simply fail to log in.
The role's password has to be changed in the database itself.

What it does (aborting before any change if a step fails):
  0. preconditions   docker reachable; volume exists; NO container (running or stopped) is using it;
                     PG major version matches the image
  1. start           a private throwaway postgres on the volume (internal Docker network, no published port)
  2. verify target   role and database exist
  3. manifest        per-table row count + content hash of the live data
  4. backup          pg_dump -Fc, copied to the host with a SHA-256, never inside the repo tree's tracked files
  5. validate backup pg_restore --list, then a FULL RESTORE into a scratch database and manifest comparison
  2b. preflight      a wrong password must already be rejected from another container (auth is enforced)
  6. rotate          ALTER ROLE ... PASSWORD (scram-sha-256); password travels via stdin only, never argv
  7. verify          new password works from another container; a wrong one (and the old one, if given)
                     is rejected; manifest identical (data intact)
  8. stop            clean shutdown; container removed; the volume is kept

It never runs `docker compose down -v`, never deletes a volume, and refuses to touch a volume that any
container uses. If the backup cannot be made and verified, it does NOT rotate.

    python scripts/rotate_db_password.py --volume lumen_postgres_data --dry-run     # steps 0-5 only
    python scripts/rotate_db_password.py --volume lumen_postgres_data --generate --write-env .env
    NEW_DB_PASSWORD=... python scripts/rotate_db_password.py --volume V --new-password-env NEW_DB_PASSWORD
"""
import argparse
import hashlib
import json
import os
import re
import secrets
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

IMAGE_DEFAULT = "postgres:16-alpine"
PASSWORD_RE = re.compile(r"^[A-Za-z0-9._~-]{24,128}$")
IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,62}$")


class Abort(RuntimeError):
    pass


def docker(*args, input_text=None, check=True, timeout=300):
    r = subprocess.run(["docker", *args], input=input_text, capture_output=True, text=True, timeout=timeout)
    if check and r.returncode != 0:
        raise Abort(f"docker {' '.join(args[:3])}... failed: {(r.stderr or r.stdout).strip()[:400]}")
    return r


def say(msg):
    print(msg, flush=True)


class Server:
    """A private postgres on the volume, driven with `docker exec`."""

    def __init__(self, volume, image, user, db):
        self.volume, self.image, self.user, self.db = volume, image, user, db
        tag = secrets.token_hex(4)
        self.name = f"lumen-rotate-{tag}"
        self.net = f"lumen-rotate-net-{tag}"

    def start(self):
        # An --internal network: no route to anything outside Docker, yet a second container can reach the server.
        # Needed because pg_hba.conf trusts loopback and the unix socket INSIDE the server container, so password
        # enforcement can only be tested from another host.
        docker("network", "create", "--internal", self.net)
        docker("run", "-d", "--name", self.name, "--network", self.net, "--network-alias", "db",
               "-v", f"{self.volume}:/var/lib/postgresql/data", self.image)
        for _ in range(90):
            if docker("exec", self.name, "pg_isready", "-U", self.user, check=False).returncode == 0:
                return
            if docker("inspect", "-f", "{{.State.Running}}", self.name, check=False).stdout.strip() != "true":
                raise Abort("postgres exited during start: " + docker("logs", "--tail", "15", self.name, check=False).stdout[-500:])
            time.sleep(1)
        raise Abort("postgres did not become ready in 90 s")

    def psql(self, sql, db=None, user=None, tuples=True, input_sql=False):
        args = ["exec", "-i", self.name, "psql", "-v", "ON_ERROR_STOP=1", "-U", user or self.user, "-d", db or self.db]
        if tuples:
            args += ["-tA"]
        if input_sql:
            return docker(*args, input_text=sql).stdout.strip()
        return docker(*args, "-c", sql).stdout.strip()

    def client_can_login(self, password, db=None):
        """Log in from ANOTHER container over the network, i.e. through the rule that enforces passwords."""
        r = docker("run", "--rm", "--network", self.net, "-e", f"PGPASSWORD={password}", self.image, "psql",
                   "-h", "db", "-U", self.user, "-d", db or self.db, "-tAc", "select 1", check=False, timeout=60)
        return r.returncode == 0 and r.stdout.strip() == "1"

    def stop(self):
        docker("stop", "-t", "60", self.name, check=False, timeout=120)
        docker("rm", self.name, check=False)
        docker("network", "rm", self.net, check=False)


def manifest(srv, db):
    tables = srv.psql("select table_name from information_schema.tables where table_schema='public' and table_type='BASE TABLE' order by 1", db=db).splitlines()
    out = {}
    for t in [x for x in tables if x]:
        if not IDENT_RE.match(t):
            raise Abort(f"unexpected table name {t!r}")
        row = srv.psql(f'select count(*) || \'|\' || coalesce(md5(string_agg(x::text, \'\' order by x::text)), \'\') from "{t}" x', db=db)
        n, h = row.split("|", 1)
        out[t] = {"rows": int(n), "md5": h}
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--volume", required=True)
    ap.add_argument("--user", default="lumen")
    ap.add_argument("--db", default="lumen")
    ap.add_argument("--image", default=IMAGE_DEFAULT)
    ap.add_argument("--backup-dir", default=str(Path(__file__).resolve().parent.parent / "backups"))
    ap.add_argument("--dry-run", action="store_true", help="inspect, back up and validate the backup; change nothing")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--generate", action="store_true", help="generate a strong password")
    g.add_argument("--new-password-env", help="name of an environment variable holding the new password")
    ap.add_argument("--old-password-env", help="env var with the OLD password, to prove it stops working")
    ap.add_argument("--write-env", help="write POSTGRES_PASSWORD=<new> into this env file (created/updated)")
    args = ap.parse_args()

    for label, val in (("user", args.user), ("db", args.db)):
        if not IDENT_RE.match(val):
            raise Abort(f"--{label} must be a plain identifier")
    new_pw = None
    if not args.dry_run:
        if args.generate:
            new_pw = secrets.token_hex(24)
        elif args.new_password_env:
            new_pw = os.environ.get(args.new_password_env, "")
        else:
            raise Abort("give --generate or --new-password-env (or use --dry-run)")
        if not PASSWORD_RE.match(new_pw):
            raise Abort("new password must be 24-128 chars from [A-Za-z0-9._~-] (it is embedded in DATABASE_URL)")
    old_pw = os.environ.get(args.old_password_env, "") if args.old_password_env else None

    # 0. preconditions ------------------------------------------------------------------------------
    say("[0] preconditions")
    docker("version", "--format", "{{.Server.Version}}")
    if docker("volume", "inspect", args.volume, check=False).returncode != 0:
        raise Abort(f"volume {args.volume!r} does not exist")
    users = docker("ps", "-a", "--filter", f"volume={args.volume}", "--format", "{{.Names}} ({{.Status}})").stdout.split("\n")
    users = [u for u in users if u.strip()]
    if users:
        raise Abort("refusing to touch a volume that containers use: " + ", ".join(users) +
                    " . Stop/remove them yourself first -- they may belong to another project.")
    ver = docker("run", "--rm", "-v", f"{args.volume}:/d:ro", "alpine", "cat", "/d/PG_VERSION").stdout.strip()
    img_major = docker("run", "--rm", "--entrypoint", "postgres", args.image, "--version").stdout.split()[-1].split(".")[0]
    if ver != img_major:
        raise Abort(f"volume is PostgreSQL {ver} but image is {img_major}; use --image postgres:{ver}-alpine")
    say(f"    volume ok, PostgreSQL {ver}, no container attached")

    srv = Server(args.volume, args.image, args.user, args.db)
    rotated = False
    try:
        say("[1] starting a private server on the volume (internal network, no published port)")
        srv.start()

        say("[2] verifying target role and database")
        try:
            has_role = srv.psql(f"select 1 from pg_roles where rolname='{args.user}'", db="postgres") == "1"
        except Abort as e:
            if "does not exist" in str(e):
                has_role = False
            else:
                raise
        if not has_role:
            raise Abort(f"role {args.user!r} not found in this volume (is it another project's data?)")
        if srv.psql(f"select 1 from pg_database where datname='{args.db}'", db="postgres") != "1":
            raise Abort(f"database {args.db!r} not found")

        say("    preflight: a wrong password must be rejected over the network (password auth is enforced)")
        if srv.client_can_login("definitely-wrong-" + secrets.token_hex(6)):
            raise Abort("this database accepts ANY password from other hosts (trust auth). Rotating would be meaningless; "
                        "fix pg_hba.conf first. Nothing was changed.")
        if old_pw is not None:
            if not srv.client_can_login(old_pw):
                raise Abort("the --old-password-env value does not log in: wrong old password or wrong role. Nothing was changed.")
            say("    the supplied OLD password logs in (so its later rejection is meaningful)")

        say("[3] manifest of live data")
        before = manifest(srv, args.db)
        for t, v in before.items():
            say(f"    {t}: {v['rows']} rows")
        if not before:
            say("    (no tables yet: nothing to lose, but the backup step still runs)")

        say("[4] backup")
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        Path(args.backup_dir).mkdir(parents=True, exist_ok=True)
        dump_host = Path(args.backup_dir) / f"{args.db}-{stamp}.dump"
        docker("exec", srv.name, "pg_dump", "-U", args.user, "-d", args.db, "-Fc", "-f", "/tmp/backup.dump")
        docker("cp", f"{srv.name}:/tmp/backup.dump", str(dump_host))
        sha = hashlib.sha256(dump_host.read_bytes()).hexdigest()
        dump_host.with_suffix(".dump.json").write_text(json.dumps({"sha256": sha, "manifest": before, "created": stamp}, indent=2))
        say(f"    {dump_host} ({dump_host.stat().st_size} bytes, sha256 {sha[:16]}...)")

        say("[5] validating the backup by restoring it into a scratch database")
        toc = docker("exec", srv.name, "pg_restore", "--list", "/tmp/backup.dump").stdout
        if not toc.strip():
            raise Abort("backup has an empty table of contents")
        scratch = f"{args.db}_verify"
        srv.psql(f'drop database if exists "{scratch}"', db="postgres")
        srv.psql(f'create database "{scratch}"', db="postgres")
        docker("exec", srv.name, "pg_restore", "-U", args.user, "-d", scratch, "--no-owner", "/tmp/backup.dump")
        restored = manifest(srv, scratch)
        srv.psql(f'drop database "{scratch}"', db="postgres")
        if restored != before:
            raise Abort("RESTORED DATA DIFFERS FROM LIVE DATA -- backup is not trustworthy; NOT rotating")
        say("    restore produced identical row counts and content hashes")

        if args.dry_run:
            say("DRY RUN complete: backup is valid, nothing was changed.")
        else:
            say("[6] rotating the role password")
            sql = "set password_encryption='scram-sha-256';\n" f"alter role \"{args.user}\" password '{new_pw}';\n"
            srv.psql(sql, db="postgres", input_sql=True)
            rotated = True

            say("[7] verifying")

            if not srv.client_can_login(new_pw):
                raise Abort("NEW password does not authenticate over the network")
            say("    new password authenticates (from another container, over the network)")
            if srv.client_can_login("definitely-wrong-" + secrets.token_hex(6)):
                raise Abort("a WRONG password was accepted after rotation")
            say("    a wrong password is rejected")
            if old_pw is not None:
                if srv.client_can_login(old_pw):
                    raise Abort("the OLD password still works")
                say("    the old password is rejected")
            after = manifest(srv, args.db)
            if after != before:
                raise Abort("DATA CHANGED DURING ROTATION")
            say("    data intact: identical row counts and content hashes")
            if args.write_env:
                p = Path(args.write_env)
                lines = p.read_text(encoding="utf-8").splitlines() if p.exists() else []
                lines = [ln for ln in lines if not ln.startswith("POSTGRES_PASSWORD=")] + [f"POSTGRES_PASSWORD={new_pw}"]
                p.write_text("\n".join(lines) + "\n", encoding="utf-8")
                say(f"    wrote POSTGRES_PASSWORD to {p} (value not displayed)")
            elif args.generate:
                say("    NOTE: --generate without --write-env: the password was NOT saved anywhere. "
                    "Re-run with --new-password-env to set one you control.")
    except Abort as e:
        say(f"ABORTED: {e}")
        if rotated:
            say("The password HAD been changed before this failure; the verified backup is in " + args.backup_dir)
        return 1
    finally:
        say("[8] clean shutdown (volume is kept)")
        srv.stop()
    if not args.dry_run:
        say("DONE. Update the backend's DATABASE_URL / POSTGRES_PASSWORD to the new value, then restart the stack.")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Abort as e:
        print(f"ABORTED: {e}")
        sys.exit(1)
