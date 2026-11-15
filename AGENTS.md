# AGENTS.md — working agreement for AI coding agents and contributors

Read this before changing anything. Then read `ARCHITECTURE.md` and `SECURITY_REVIEW.md`.

## Hard rules

1. **The frontend design is frozen** (`DESIGN.md`). Do not change the palette, tokens, layout or add dark mode. Touch `frontend/` only to fix real functionality, security, accessibility or integration problems.
2. **Never delete data.** Do not remove Docker volumes (`deep-research-platform_postgres_data`, `…_redis_data`), do not run `docker compose down -v`, do not drop/truncate anything except in the isolated test database (name ends in `_test`, localhost only — `backend/tests/conftest.py` enforces this).
3. **No new dependencies without explicit approval.** Version bumps for security fixes are fine when documented.
4. **Local Git only.** Never force-push, rewrite remote history, or use destructive commands (`reset --hard`, `clean -fd`, `checkout -- .`) to tidy the tree.
5. **Never commit secrets** (`.env`, keys). `JWT_SECRET` and `AGENT_API_KEY` come from the environment.
6. **No paid calls in tests.** Mock the LLM and every external API.
7. Do not claim a test ran when it did not. Report failures and gaps honestly.

## Security invariants (each has a regression test — keep them passing)

- Auth is **header-only** bearer. Never accept tokens from query strings or cookies without a full, reviewed migration.
- Conversation/message access is always filtered by the authenticated user; cross-user access returns **404**.
- **Never send raw exception text, upstream response bodies, stack traces or key material to a client** (HTTP or SSE). Log server-side; send fixed messages.
- The research agent must stay unpublished and require `X-Internal-Token`; it must fail closed if `AGENT_API_KEY` is unset.
- All fetched web content is untrusted data. URL fetching goes through the SSRF guard; never fetch a model- or user-supplied URL with a bare `httpx.get`.
- Tool outputs destined for structured parsing must have `|` and newlines stripped from every field.
- Do not cache error results. Cache keys are global, so cached values must never contain user data.
- Secrets must be ≥32 chars and not placeholder-like; the app refuses to start otherwise.

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
| Frontend gates | `cd frontend && npx tsc --noEmit && npx vite build` |
| New migration | `cd backend && DATABASE_URL=… JWT_SECRET=… alembic revision --autogenerate -m "…"` then review it |
| Compose validation | `JWT_SECRET=… AGENT_API_KEY=… docker compose config -q` |

Other projects may occupy 8004/8080/5433/6379 on the same machine; use the high test ports above for local runs.

## Conventions

- Backend DB work inside `async def` endpoints goes through `run_in_threadpool` (sync SQLAlchemy).
- Any schema change needs an Alembic migration; `test_migrations.py` fails on drift.
- New block type: schema in `research-agent/.../blocks/schemas.py` → emitter in `block_formatter.py` → `frontend/src/types.ts` → component → `BlockRenderer` case. Unknown types must keep falling back to `FallbackBlock`.
- New tool: validate inputs, bound time and size, never cache failures, sanitise structured fields, add tests (success, failure, invalid input, cache, malformed upstream).
- Keep changes small and independently verifiable; add a regression test with every bug fix.
