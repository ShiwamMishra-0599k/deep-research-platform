# AGENTS.md — working agreement for AI coding agents and contributors

Read this before changing anything. Then read `ARCHITECTURE.md` and `SECURITY_REVIEW.md`.

## Hard rules

1. **The frontend design is frozen** (`DESIGN.md`). Do not change the palette, tokens, layout or add dark mode. Touch `frontend/` only to fix real functionality, security, accessibility or integration problems.
2. **Never delete data.** Do not remove Docker volumes (this repo's are `lumen_postgres_data` / `lumen_redis_data`; `deep-research-platform_*` belong to a DIFFERENT repository's running stack — leave them alone), do not run `docker compose down -v` against a real stack, do not drop/truncate anything except in the isolated test database (name ends in `_test`, localhost only — `backend/tests/conftest.py` enforces this).
3. **No new dependencies without explicit approval.** Version bumps for security fixes are fine when documented.
4. **Local Git only.** Never force-push, rewrite remote history, or use destructive commands (`reset --hard`, `clean -fd`, `checkout -- .`) to tidy the tree.
5. **Never commit secrets** (`.env`, keys). `JWT_SECRET` and `AGENT_API_KEY` come from the environment.
6. **No paid calls in tests.** Mock the LLM and every external API.
7. Do not claim a test ran when it did not. Report failures and gaps honestly.

## Security invariants (each has a regression test — keep them passing)

- Sessions are **HttpOnly cookies** (access 15 min, rotating refresh) with server-side revocation. Never accept tokens from query strings, never put a credential or identity in `localStorage`/`sessionStorage`, never expose the access/refresh cookie to script (only the CSRF cookie is readable), and never skip the CSRF + Origin checks on a cookie-authenticated unsafe request. A present-but-invalid Bearer credential must never fall back to a cookie. Design: `ARCHITECTURE.md` section 3a.
- Conversation/message access is always filtered by the authenticated user; cross-user access returns **404**.
- **Never send raw exception text, upstream response bodies, stack traces or key material to a client** (HTTP or SSE). Log server-side; send fixed messages.
- The research agent must stay unpublished and require `X-Internal-Token`; it must fail closed if `AGENT_API_KEY` is unset.
- All fetched web content is untrusted data. URL fetching goes through the SSRF guard; never fetch a model- or user-supplied URL with a bare `httpx.get`.
- Tool outputs destined for structured parsing must have `|` and newlines stripped from every field.
- Do not cache error results. Cache keys are global, so cached values must never contain user data.
- Secrets must be ≥32 chars and not placeholder-like; the app refuses to start otherwise. `DATABASE_URL` has no default and weak/default database passwords are refused (`ALLOW_INSECURE_DEV_DB` is for local databases only).
- Rate limits and concurrency guards go through `app/ratelimit.py` (Redis, atomic Lua, bounded fallback). Never add an in-memory-only limiter for a guarded operation, never fall back to *unlimited*, never store raw emails/IPs/user ids in Redis.
- Client IP comes from `app/client_ip.py` only. Never read `X-Forwarded-For` directly.
- Never run a destructive Docker command on a volume you did not create in this session. Identify the owner first (`docker ps -a --filter volume=<name>`). `scripts/rotate_db_password.py` refuses volumes in use.

## Layout & commands

```
backend/         FastAPI + SQLAlchemy + Alembic     (Python 3.12)
research-agent/  FastAPI + Deep Agents + 8 tools    (Python 3.12)
frontend/        React 18 + Vite + TypeScript + Tailwind
e2e/             full-stack integration harness (real PG/Redis/backend/agent; fake LLM + fake internet)
```

Environments (kept in git-ignored `.venv/`): `uv venv --python 3.12 .venv && uv pip install --python .venv/Scripts/python.exe -r requirements-dev.txt` (backend) or `-e .` (agent).

Isolated test services (never the project's compose volumes):

```bash
docker run -d --name lumen-test-pg -e POSTGRES_DB=lumen_test -e POSTGRES_USER=lumen_test \
  -e POSTGRES_PASSWORD=lumen_test -p 127.0.0.1:55432:5432 postgres:16-alpine
docker run -d --name lumen-test-redis -p 127.0.0.1:56379:6379 redis:7-alpine
```

| Task | Command |
|---|---|
| Backend tests | `cd backend && .venv/Scripts/python.exe -m pytest` |
| Agent tests | `cd research-agent && .venv/Scripts/python.exe -m pytest` |
| Agent lint | `cd research-agent && .venv/Scripts/python.exe -m ruff check src tests` |
| Frontend gates | `cd frontend && npx tsc --noEmit && npm run test:coverage && npx vite build` (Node >= 22; coverage thresholds 70/60/70/70 are enforced) |
| New migration | `cd backend && DATABASE_URL=… JWT_SECRET=… alembic revision --autogenerate -m "…"` then review it |
| Compose validation | `JWT_SECRET=… AGENT_API_KEY=… POSTGRES_PASSWORD=… docker compose config -q` |
| Generate secrets | `python scripts/init_env.py` (git-ignored `.env`; never overwrites) |
| Rotate a DB password | `python scripts/rotate_db_password.py --volume <name> --dry-run` first; see `docs/DB_CREDENTIALS.md` |
| Every CI job locally | `python scripts/ci_local.py` |
| Multi-instance limits / rotation rehearsal | `backend/.venv/Scripts/python.exe e2e/multi_instance_tests.py` / `…/rotation_rehearsal.py` |

Other projects may occupy 8004/8080/5433/6379 on the same machine; use the high test ports above for local runs.

## Conventions

- Backend DB work inside `async def` endpoints goes through `run_in_threadpool` (sync SQLAlchemy).
- Any schema change needs an Alembic migration; `test_migrations.py` fails on drift.
- New block type: schema in `research-agent/.../blocks/schemas.py` → emitter in `block_formatter.py` → `frontend/src/types.ts` → component → `BlockRenderer` case. Unknown types must keep falling back to `FallbackBlock`.
- New tool: validate inputs, bound time and size, never cache failures, sanitise structured fields, add tests (success, failure, invalid input, cache, malformed upstream).
- Keep changes small and independently verifiable; add a regression test with every bug fix.
