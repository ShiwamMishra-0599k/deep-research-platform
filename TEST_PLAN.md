# Lumen — Test Plan

What is tested, how, against what, and — just as important — what is **not**. Numbers below are from the final run on this codebase.

## 1. Test levels and what is real

| Level | Suite | Count | PostgreSQL | Redis | Agent | LLM | External sites |
|---|---|---|---|---|---|---|---|
| Backend unit + API | `backend/tests` | **144** | **real** (isolated container) | – | scripted fake | – | – |
| Agent unit + service | `research-agent/tests` | **379** | – | **real** (isolated) + dead/hanging cases | real code, fake `get_agent` for SSE | mocked | mocked; non-loopback sockets blocked |
| Full-stack integration (browser) | `e2e/run.py` → `scenario.mjs` | **42** checks | **real** | **real** | **real** (LangGraph + 8 tools) | **fake server** (`e2e/fake_llm.py`) | **faked** (`e2e/run_agent_fake_internet.py`), sealed at socket level |
| Failure & recovery | `e2e/failure_tests.py` | **39** checks | **real**, container stopped/started | **real**, stopped/started | **real**, killed/relaunched; plus a rogue agent | fake | faked, sealed |
| Deployment smoke | manual compose run (project `lumen-verify`) | see §4 | real | real | real image | – (no query run) | – |
| Frontend gates | `tsc`, `vite build` | pass | – | – | – | – | – |

Labels matter: the integration suites exercise the real application stack end to end **with a fake LLM and fake third-party
sites**. They are not tests of live providers.

## 2. Suites in detail

**Backend (144):** `test_auth` 48 (registration, validation, duplicate/case, concurrency race, login, timing-equal responses,
IP throttle, lockout, token expiry/forgery incl. `alg:none`/wrong aud/iss/HS512/tampered, query-token rejection, secret strength);
`test_conversations` 20 (CRUD, ownership/IDOR on every route, cross-user history isolation, cascade, SQL-injection strings);
`test_http_security` 15 (headers, 413 declared/streamed, CORS, no docs, no leaks); `test_research_sse` 28 (framing, ordering,
persistence, clarification, every failure mode, rate/concurrency limits, real ASGI client disconnect, 5 concurrent users);
`test_research_client` 19 (SSE parser, CRLF, oversize, status mapping, no redirects, real closed port); `test_migrations` 9
(fresh install, idempotent, no model/migration drift, **legacy database adoption with data intact**, partial schema refused,
failed migration rolls back, downgrade, concurrent runs); `test_failures` 5 (database down → 503, readiness, fast failure).

**Agent (379, 94 % line coverage):** all eight tools (success, failure, invalid input, fallbacks, timeouts, malformed upstream,
cache hit/miss, errors-never-cached) — `test_tools_data` 108, `test_tools_web` 45, `test_search_providers` 6; SSRF guard 81;
blocks/checkpoints/follow-ups 49; prompt-injection and forged-row cases 30; SSE handler 18 (framing, interruption, cancellation,
timeout, concurrency); server 25 (auth, limits, no CORS, protected ops endpoints); Redis 12 (real, dead, hanging); graph 5.
The author mutation-checked six controls (private-IP, error caching, planted sources, auth bypass, QL injection, error text): the suite failed each time.

**Integration scenario (42):** register → progress checkpoints streamed live → answer with markdown, map, table, insight cards
and citations → rows verified in PostgreSQL → reload restores blocks → second conversation → switch → rename → clarification →
LLM outage message with no internals → error persisted → delete (accessible dialog) → ask again after deleting the active chat →
expired token → back to sign-in → login → logout → **second person on the same tab sees nothing of the first** → protected-route
redirect → bad-password message → no unexpected console errors → hermetic check.

**Failure suite (39):** Redis down/restored; search provider down (partial result); invalid upstream JSON; LLM down; agent killed
and relaunched; **PostgreSQL stopped and restarted with no data loss**; request timeout; rate limit; 4 abrupt browser
disconnects (slots released); 12 parallel streams across 6 users (no cross-talk, each persisted correctly); rogue agent (invalid
JSON, unknown events, agent-supplied error text, 3 MB event, truncated stream, bad block shape, HTTP 500 with a secret in the body,
redirect to cloud metadata, hostile markdown); hermetic check.

## 3. How to run

Prerequisites: Docker Desktop, Python 3.12 venvs (`backend/.venv`, `research-agent/.venv`), `npm install` in `frontend/`,
Microsoft Edge or Chrome, and the isolated services from `AGENTS.md`.

```bash
(cd backend && .venv/Scripts/python.exe -m pytest)
(cd research-agent && .venv/Scripts/python.exe -m pytest --cov=research_agent)
python e2e/run.py            # browser journey over the real stack
python e2e/failure_tests.py  # real outages (stops/starts ONLY lumen-test-pg / lumen-test-redis)
(cd frontend && npx tsc --noEmit && npx vite build)
uvx ruff check --config ruff.toml backend e2e/*.py ; (cd research-agent && .venv/Scripts/python.exe -m ruff check src tests)
uvx bandit -r backend/app research-agent/src ; uvx pip-audit -r backend/requirements.txt ; (cd frontend && npm audit --omit=dev)
```

Safety: the backend suite refuses to run unless the database is local and named `*_test`; the e2e scripts drop only their own
`lumen_e2e_test` database. The project's real volumes are never referenced.

## 4. Deployment smoke test (compose, isolated project name, throwaway volumes)

Verified on the real images: migrations ran on start and again idempotently after a restart with data kept; `alembic_version = 0002`;
backend healthy via its healthcheck; the agent healthcheck gates the backend; backend published on `127.0.0.1` only, **agent and
Redis publish nothing**; both containers run as UID 10001; from inside the network the agent answers `/health` 200 and `/health/ready`,
`/metrics`, `/research` with 401 (no/bad token), 200 with the right token, `/docs` 404; through the published port: security headers
present, `?token=` rejected (401), header auth 200, `/docs` 404. The project's `deep-research-platform_*` volumes were not touched.
No research query was run in compose (it would call real providers with no key).

## 5. Requirements coverage (PRD §6)

F1 auth ✔ (backend + browser) · F2 conversations ✔ · F3 streaming progress ✔ (browser) · F4 eight tools ✔ (mocked upstreams; live shapes spot-checked once) ·
F5 blocks ✔ (all four types + fallback rendered in a real browser) · F6 citations ✔ · F7 follow-ups ✔ (mocked OpenAI) · F8 clarification ✔ ·
F9 persistence ✔ (incl. disconnect) · F10 failure handling ✔ (real outages) · F11 abuse controls ✔.

## 6. Gaps (not tested)

- **No frontend unit/component tests** — there is no test framework in the project and adding one needs approval. The browser scenario covers the main flows; individual components, a11y beyond programmatic contrast/focus, and error edge states are not unit-tested.
- Real LLM/search providers; paid-provider behaviour (rate limits, quotas, latency, cost).
- Browsers other than Chromium-based Edge; real devices; screen readers.
- TLS, reverse proxy, multi-replica, load/soak/performance, memory growth, very long conversations.
- Live DNS-rebinding behaviour; container image CVE scanning.
- Rate limiting accuracy under several workers.
- Source-map/CSP behaviour of a production static hosting setup.
