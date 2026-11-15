# Lumen — Deep Research Platform

A full-stack research assistant. Sign in, ask a question, watch the agent work, and get a cited answer made of structured
blocks (markdown, tables, maps, metric cards). React frontend, FastAPI backend, PostgreSQL, and a separate Python research agent
(Deep Agents / LangGraph) with eight tools, streaming everything over Server-Sent Events.

```
┌────────────┐  HTTPS+SSE   ┌─────────────────────┐  SSE + X-Internal-Token  ┌──────────────────────┐
│ React UI   │◄────────────►│ FastAPI backend     │◄────────────────────────►│ Research agent       │
│ Vite / TS  │  Bearer JWT  │ auth · DB · SSE     │   (not published)        │ Deep Agents · 8 tools│
└────────────┘              └──────────┬──────────┘                          └───────────┬──────────┘
                                       ▼                                                 ▼
                                 PostgreSQL 16                                    Redis 7 (tool cache)
```

Documentation: [`PRD.md`](PRD.md) · [`ARCHITECTURE.md`](ARCHITECTURE.md) · [`research-agent/ARCHITECTURE.md`](research-agent/ARCHITECTURE.md) ·
[`DESIGN.md`](DESIGN.md) · [`SECURITY_REVIEW.md`](SECURITY_REVIEW.md) · [`TEST_PLAN.md`](TEST_PLAN.md) · [`TASKS.md`](TASKS.md) · [`AGENTS.md`](AGENTS.md)

## Quick start (Docker)

```bash
# 1. secrets (both REQUIRED — compose and the backend refuse to start without them)
cp .env.example .env
#    edit .env:  JWT_SECRET=$(openssl rand -base64 48)   AGENT_API_KEY=$(openssl rand -base64 32)

# 2. agent keys
cp research-agent/.env.example research-agent/.env
#    fill OPENROUTER_API_KEY (or OPENAI_API_KEY) and EXA_API_KEY / TAVILY_API_KEY (DuckDuckGo is the keyless fallback)

# 3. start postgres + redis + agent + backend (migrations run automatically)
docker compose up --build -d

# 4. frontend
cd frontend && npm install && npm run dev      # http://localhost:5173
```

| Service | Reachable from | Notes |
|---|---|---|
| frontend dev server | host `:5173` | proxies `/api` to the backend |
| backend | host `127.0.0.1:8080` | put a TLS proxy in front for real use |
| postgres | host `127.0.0.1:5433` | development credentials — change for shared use |
| research-agent | **compose network only** | requires `X-Internal-Token`; never published |
| redis | **compose network only** | cache only, memory-bounded |

> If another project already uses 8080/5433 on your machine, override the published ports with a compose override file.

## Repository layout

| Path | What |
|---|---|
| `frontend/` | React 18 + Vite + TypeScript + Tailwind + Framer Motion. `BlockRenderer` maps block types to components with a JSON fallback for unknown types |
| `backend/` | FastAPI on Python 3.12, SQLAlchemy 2, Alembic, JWT (HS256) + bcrypt, rate limiting, SSE bridge |
| `research-agent/` | FastAPI service: Deep Agents ReAct loop, 8 tools, SSRF guard, Redis cache, block formatter |
| `e2e/` | Full-stack integration harness (real PostgreSQL/Redis/backend/agent; fake LLM and fake third-party sites) |
| `docker-compose.yml` | postgres, redis, research-agent, backend |

## API (backend)

All `/api/conversations*` routes need `Authorization: Bearer <jwt>`. Cross-user access returns **404**. (Tokens in query strings are not accepted.)

| Method | Path | Description |
|---|---|---|
| POST | `/api/auth/register` | `{email, password (8–72 bytes), name}` → `{token, id, email, name}`; 409 duplicate; 429 throttled |
| POST | `/api/auth/login` | `{email, password}` → same shape; 401 invalid; 429 throttled/locked |
| GET | `/api/auth/me` | current user |
| GET / POST | `/api/conversations` | list (newest first) / create |
| GET / PATCH / DELETE | `/api/conversations/{id}` | one / rename `{title}` (204) / delete (204) |
| GET | `/api/conversations/{id}/messages` | transcript with blocks, sources, follow-ups |
| POST | `/api/conversations/{id}/query` | `{query ≤ 4000}` → SSE stream; 429 when rate/concurrency limited |
| GET | `/health`, `/health/ready` | liveness / readiness (database) |

### SSE events from `/query`

`user_message` → `checkpoint`* → one of `blocks` · `clarification` · `error` → `persisted` (or `persist_error`).
Error text is always a fixed, user-safe message; details are logged server-side. Full table in [`ARCHITECTURE.md`](ARCHITECTURE.md).

## Database and migrations

Schema is managed by Alembic (`backend/alembic/`). The backend container runs `python -m app.migrate` before serving
(idempotent, advisory-locked, adopts databases created by the old `create_all` hook without touching data).

```bash
cd backend && DATABASE_URL=… JWT_SECRET=… alembic revision --autogenerate -m "describe change"   # then review the file
cd backend && DATABASE_URL=… python -m app.migrate                                                # apply
```

## Testing

See [`TEST_PLAN.md`](TEST_PLAN.md) for what each suite covers, what is real versus mocked, and the known gaps.

```bash
# isolated services (never the project's compose volumes) — see AGENTS.md
(cd backend && .venv/Scripts/python.exe -m pytest)                       # 144 tests, real PostgreSQL
(cd research-agent && .venv/Scripts/python.exe -m pytest)                # 379 tests, real Redis, no real network
python e2e/run.py && python e2e/failure_tests.py                         # browser journey + real outages
(cd frontend && npx tsc --noEmit && npx vite build)
```

## Local development without Docker

```bash
cd backend && uv venv --python 3.12 .venv && uv pip install --python .venv/Scripts/python.exe -r requirements-dev.txt
cp .env.example .env     # set DATABASE_URL, JWT_SECRET, AGENT_API_KEY
.venv/Scripts/python.exe -m app.migrate && .venv/Scripts/python.exe -m uvicorn app.main:app --reload --port 8080
```

The agent: `cd research-agent && uv venv --python 3.12 .venv && uv pip install --python .venv/Scripts/python.exe -e .`, then run
`uvicorn research_agent.server:create_app --factory --port 8004` with the same `AGENT_API_KEY`.

## Adding a block type

1. Schema in `research-agent/src/research_agent/blocks/schemas.py`; emit it from `block_formatter.py` (+ tests).
2. Mirror the type in `frontend/src/types.ts`.
3. Component in `frontend/src/components/blocks/<Name>Block.tsx`.
4. Case in `BlockRenderer.tsx`. Until then the block renders through `FallbackBlock`, so unknown types never break the UI.

## Security summary

Header-only JWT with required strong secret, rate limiting and lockout, ownership-checked data access, fixed error messages,
security headers and body limits, an internal-token-protected agent that is not published, an SSRF guard for all fetched URLs,
untrusted-content handling for prompt injection, and reproducible migrations. Open items and accepted risks are listed honestly
in [`SECURITY_REVIEW.md`](SECURITY_REVIEW.md).

## Development tips

- Logs: `docker compose logs -f backend` / `research-agent`
- Health: `curl http://127.0.0.1:8080/health/ready`
- **Never** run `docker compose down -v` against your real stack — it deletes the database volume.
