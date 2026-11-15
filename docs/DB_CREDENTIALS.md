# Database credentials: policy and rotation runbook

## Policy (enforced in code, not by convention)

| Rule | Enforced by |
|---|---|
| No default database credential exists anywhere | `DATABASE_URL` has no default in `backend/app/config.py`; compose requires `POSTGRES_PASSWORD` (`${POSTGRES_PASSWORD:?…}`) |
| Weak/default passwords are refused at startup | `Settings` validator: `lumen`, `postgres`, `password`, `admin`, `root`, `changeme`, …, anything under 12 chars, or empty |
| A weak password needs an explicit, local-only opt-in | `ALLOW_INSECURE_DEV_DB=1`, accepted only for `localhost`/`127.0.0.1`/the compose service `postgres`; off by default |
| Rejected credentials never reach logs | `hide_input_in_errors`; tested |
| Compose project is isolated | `name: lumen` in `docker-compose.yml` (see "Volume collisions" below) |
| Secrets are generated, not typed | `python scripts/init_env.py` (writes a git-ignored `.env`, never overwrites, never prints) |

The password is also embedded in `DATABASE_URL`, so use hex/alphanumeric (`openssl rand -hex 24`).

## Why rotation needs a procedure

`POSTGRES_PASSWORD` in an env file is read **only when the data directory is first created**. Changing it later changes nothing
inside an existing database, and the backend then fails to authenticate. The role's password must be changed in the database.

Findings from testing this (see `e2e/rotation_rehearsal.py`):

- `postgres:16-alpine` trusts the unix socket **and loopback** inside the server container; passwords are enforced only for
  connections from other hosts (the backend container, the published port). A test from inside the server container therefore
  proves nothing. The rotation script verifies from a second container on a private internal network.
- A script that checks only after changing the password can strand you half-rotated. This one runs the "wrong password must be
  rejected" preflight **before** changing anything.

## Rotating an existing volume

Prerequisites: stop the stack that uses the volume (`docker compose stop`; **never** `down -v`). Docker must be running.

```bash
# 1. Dry run: inspects, backs up, and proves the backup by restoring it into a scratch database. Changes nothing.
python scripts/rotate_db_password.py --volume lumen_postgres_data --dry-run

# 2. Rotate (generates a strong password and writes POSTGRES_PASSWORD into .env without displaying it)
OLD_DB_PW='<current password>' python scripts/rotate_db_password.py --volume lumen_postgres_data \
    --generate --write-env .env --old-password-env OLD_DB_PW

# 3. Start the stack; the backend now connects with the new password
docker compose up -d
```

What the script guarantees (it aborts before any change if a step fails):

0. Docker works, the volume exists, **no container (running or stopped) uses it**, and the PostgreSQL major version matches.
1. Starts a private server on the volume (internal Docker network, no published port).
2. The target role and database exist; **a wrong password is already rejected over the network** (else rotation is meaningless).
3. Records a manifest: per-table row count and content hash.
4. `pg_dump -Fc` backup copied to `backups/` (git-ignored) with a SHA-256 and the manifest.
5. **Restores the backup into a scratch database and compares the manifest.** If it differs, nothing is rotated.
6. `ALTER ROLE … PASSWORD` with `scram-sha-256`; the password travels over stdin, never on a command line.
7. The new password logs in from another container; a wrong password and the old one are rejected; the manifest is identical.
8. Clean shutdown; the container and network are removed; **the volume is kept**.

Exit code 0 only if everything held. After rotation, keep the backup until you have confirmed the application, then archive or delete it
(it contains user data and password hashes).

### If rotation or verification fails

The script says whether the password had already changed. The verified backup is in `backups/`. To restore into a fresh volume:
create a new volume, start `postgres:16-alpine` on it, `pg_restore -d <db> backup.dump`, and point the stack at that volume.

## Volume collisions (a real hazard found during this work)

Compose derives the project name — and therefore the volume names — from the **folder name**. Two checkouts whose folders are both
called `deep-research-platform` share `deep-research-platform_postgres_data`. On the development machine another stack (`alvoff-*`,
a different repository) was running on exactly that volume with a different role (`alvoff`), so `docker compose up` here would have
attached this project to someone else's live data. `docker-compose.yml` now sets `name: lumen`, so this repo uses `lumen_postgres_data`
and `lumen_redis_data`. `rotate_db_password.py` refuses any volume that a container uses, and refuses a volume whose role is not the one
you named.

If you already have data in a volume under an old name, **do not rename or copy it blindly**: first identify the owner with
`docker ps -a --filter volume=<name>` and `docker inspect <container> --format '{{index .Config.Labels "com.docker.compose.project.working_dir"}}'`.

## Redis

Redis holds only cache and rate-limit counters (no credentials, no PII: keys are HMAC-hashed). It is not published to the host and has no
password; protect it with network isolation. Because eviction is `allkeys-lru`, a flood of cache keys could in theory evict counters;
if that matters, give rate limiting its own Redis instance.
