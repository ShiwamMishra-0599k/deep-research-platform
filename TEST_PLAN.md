# Lumen — Test Plan

What is tested, how, against what, and — just as important — what is **not**. Numbers below are from the final run on this codebase.

## 1. Test levels and what is real

| Level | Suite | Count | PostgreSQL | Redis | Agent | LLM | External sites |
|---|---|---|---|---|---|---|---|
| Backend unit + API | `backend/tests` | **353** | **real** (isolated container) | **real** (limits, db 3) | scripted fake | – | – |
| Agent unit + service | `research-agent/tests` | **379** | – | **real** (isolated) + dead/hanging cases | real code, fake `get_agent` for SSE | mocked | mocked; non-loopback sockets blocked |
| Full-stack integration (browser) | `e2e/run.py` → `scenario.mjs` | **61** checks | **real** | **real** | **real** (LangGraph + 8 tools) | **fake server** (`e2e/fake_llm.py`) | **faked** (`e2e/run_agent_fake_internet.py`), sealed at socket level |
| Failure & recovery | `e2e/failure_tests.py` | **39** checks | **real**, container stopped/started | **real**, stopped/started | **real**, killed/relaunched; plus a rogue agent | fake | faked, sealed |
| Distributed rate limiting | `e2e/multi_instance_tests.py` | **17** checks | **real** | **real**, stopped/started | **real** (3 backend processes + 1 fail-closed) | fake | faked, sealed |
| DB credential rotation rehearsal | `e2e/rotation_rehearsal.py` | **19** checks | **real**, synthetic legacy volume | – | – | – | – |
| CI commands | `scripts/ci_local.py` + clean Linux containers | **16** steps (+ Linux runs of the backend, agent and frontend jobs) | service containers | service containers | – | – | – |
| Deployment smoke | manual compose run (project `lumen-verify`) | see §4 | real | real | real image | – (no query run) | – |
| Frontend unit + component | `frontend/src/**/*.test.ts(x)` (Vitest, jsdom, Testing Library) | **253** (coverage 99.7 / 94.73 / 100 / 99.7; thresholds 70/60/70/70 enforced) | – (axios adapter mocked) | – | – | – | – |
| Frontend gates | `tsc`, `vite build`, `npm audit --omit=dev` | pass / pass / 0 vulnerabilities | – | – | – | – | – |

Labels matter: the integration suites exercise the real application stack end to end **with a fake LLM and fake third-party
sites**. They are not tests of live providers.

## 2. Suites in detail

**Backend (353; was 144, then 230 after rate limiting, then +123 for cookie sessions and production configuration):** the cookie/session suites are `test_sessions` (rotation compare-and-swap, reuse detection, grace window, idle/absolute expiry, revocation), `test_csrf` (missing/forged/other-session tokens, Origin and `Sec-Fetch-Site` rules, Bearer-present-but-bad never falls back, `AUTH_MODE` matrix, cookie flags) and `cookie_helpers`; `test_config` covers the production validator (fail-closed Redis, HTTPS-only CORS, Secure cookies, secret strength, DB URL from components); `test_deploy_templates` validates the AWS/nginx templates. JWT forgery tests run on PyJWT. Original suites: `test_auth` 48 (registration, validation, duplicate/case, concurrency race, login, timing-equal responses,
IP throttle, lockout, token expiry/forgery incl. `alg:none`/wrong aud/iss/HS512/tampered, query-token rejection, secret strength);
`test_conversations` 20 (CRUD, ownership/IDOR on every route, cross-user history isolation, cascade, SQL-injection strings);
`test_http_security` 15 (headers, 413 declared/streamed, CORS, no docs, no leaks); `test_research_sse` 28 (framing, ordering,
persistence, clarification, every failure mode, rate/concurrency limits, real ASGI client disconnect, 5 concurrent users);
`test_research_client` 19 (SSE parser, CRLF, oversize, status mapping, no redirects, real closed port); `test_migrations` 9
(fresh install, idempotent, no model/migration drift, **legacy database adoption with data intact**, partial schema refused,
failed migration rolls back, downgrade, concurrent runs); `test_failures` 5 (database down → 503, readiness, fast failure); **new:** `test_ratelimit_redis` 27 (atomicity under an 80-thread burst, exact limits shared across two clients, sliding-window boundary, expiry, lockout across instances, slot TTL, key hygiene, dead/hanging/flapping Redis, fail-closed, breaker, script-cache flush, cold-burst regression), `test_ratelimit_http` 17 (429 + `Retry-After` on every guarded route, exact concurrent HTTP bursts, outage behaviour over HTTP, X-Forwarded-For spoofing and trusted proxies), `test_client_ip` 22, `test_config` 20 (weak/default DB passwords, dev opt-in limits, secrets never echoed, process-level refusal to boot).

**Agent (379, 94 % line coverage):** all eight tools (success, failure, invalid input, fallbacks, timeouts, malformed upstream,
cache hit/miss, errors-never-cached) — `test_tools_data` 108, `test_tools_web` 45, `test_search_providers` 6; SSRF guard 81;
blocks/checkpoints/follow-ups 49; prompt-injection and forged-row cases 30; SSE handler 18 (framing, interruption, cancellation,
timeout, concurrency); server 25 (auth, limits, no CORS, protected ops endpoints); Redis 12 (real, dead, hanging); graph 5.
The author mutation-checked six controls (private-IP, error caching, planted sources, auth bypass, QL injection, error text): the suite failed each time.

**Integration scenario (61):** register -> cookie jar inspected over CDP (access/refresh/CSRF present; access `HttpOnly` + `SameSite=Lax` + `Path=/` + ~15 min; refresh `HttpOnly` + `SameSite=Strict` + `Path=/api/auth`; CSRF readable; page script cannot see the HttpOnly ones; `localStorage`/`sessionStorage` empty) -> progress checkpoints streamed live over a cookie+CSRF-authenticated SSE POST -> answer with markdown, map, table, insight cards and citations -> rows verified in PostgreSQL -> reload keeps the session -> **access cookie deleted (what the browser does at 15 min) and reloaded: still signed in, refresh token rotated** -> second conversation -> switch -> rename -> a state-changing request without the CSRF header gets 403 -> clarification -> LLM outage message with no internals -> error persisted -> delete (accessible dialog) -> ask again after deleting the active chat -> **real cross-site attacker pages on a different site (`localhost` vs `127.0.0.1`): auto-submitting form CSRF, login CSRF, credentialed fetch; nothing created, the victim stays themselves, the credentialed read is blocked** -> `logout-all` mid-query: refresh refused, back to sign-in, cookies cleared -> login -> logout clears all three cookies -> **an access cookie copied before logout is rejected afterwards** -> a pre-migration `localStorage` session is upgraded to cookies and purged -> **second person on the same tab sees nothing of the first** -> protected-route redirect -> bad-password message -> no unexpected console errors -> hermetic check. Headless Edge only.

**Failure suite (39):** Redis down/restored; search provider down (partial result); invalid upstream JSON; LLM down; agent killed
and relaunched; **PostgreSQL stopped and restarted with no data loss**; request timeout; rate limit; 4 abrupt browser
disconnects (slots released); 12 parallel streams across 6 users (no cross-talk, each persisted correctly); rogue agent (invalid
JSON, unknown events, agent-supplied error text, 3 MB event, truncated stream, bad block shape, HTTP 500 with a secret in the body,
redirect to cloud metadata, hostile markdown); hermetic check.

## 2b. Multi-process, rate-limit and credential suites

**Distributed limits (17):** three separate uvicorn processes share one Redis. 12 logins over 3 processes → exactly 6 reach the password check; 5 failures recorded on two processes lock the account on the third (even with the correct password); 24 research requests alternating across processes → exactly 8 admitted; 6 simultaneous slow streams across processes → exactly 2 held, and the dropped clients' slots are freed promptly (not by TTL); every key has a TTL and none contains a raw email/IP/user id; with Redis stopped the instance still limits (6 of 10), reports `degraded-local`, a fail-closed instance returns 503 + `Retry-After` for login while ordinary reads work, requests are not slowed (breaker), and after restart the instance returns to the shared limiter on its own and the limit is global again.

**Credential rotation rehearsal (19):** a volume initialised the old way (`lumen`/`lumen`), migrated, seeded with 3 users / 6 conversations / 24 messages through the live API; the script refuses a volume in use (even by a stopped container), a wrong role, a missing volume, a PG-major mismatch and a weak new password; a dry run makes a restore-verified backup with checksum and manifest and changes nothing (old password still works); the real rotation then passes (new password accepted and wrong/old rejected from another container, data hashes identical, secret written to an env file and never printed); the backend refuses the old weak credential, migrations are idempotent with the new one, and every user logs in and sees all their conversations and messages.

## 3. How to run

Prerequisites: Docker Desktop, Python 3.12 venvs (`backend/.venv`, `research-agent/.venv`), `npm install` in `frontend/`,
Microsoft Edge or Chrome, and the isolated services from `AGENTS.md`.

```bash
(cd backend && .venv/Scripts/python.exe -m pytest)            # needs lumen-test-pg and lumen-test-redis
(cd research-agent && .venv/Scripts/python.exe -m pytest --cov=research_agent)
python e2e/run.py            # browser journey over the real stack
python e2e/failure_tests.py  # real outages (stops/starts ONLY lumen-test-pg / lumen-test-redis)
backend/.venv/Scripts/python.exe e2e/multi_instance_tests.py   # 3 backends sharing Redis; Redis outage and recovery
backend/.venv/Scripts/python.exe e2e/rotation_rehearsal.py     # creates and removes ONLY lumen-rehearsal-* resources
python scripts/ci_local.py   # every CI job's commands, locally
(cd frontend && npx tsc --noEmit && npm run test:coverage && npx vite build)   # Node >= 22
uvx ruff check --config ruff.toml backend e2e/*.py ; (cd research-agent && .venv/Scripts/python.exe -m ruff check src tests)
uvx bandit -r backend/app research-agent/src ; uvx pip-audit -r backend/requirements.txt --no-deps --disable-pip ; (cd frontend && npm audit --omit=dev)
```

Safety: the backend suite refuses to run unless the database is local and named `*_test`; the e2e scripts drop only their own
`lumen_e2e_test` database. The project's real volumes are never referenced.

## 3b. CI

`.github/workflows/ci.yml` runs backend, agent, frontend, lint, docker and security jobs with PostgreSQL/Redis service containers mapped to the same host ports the suites already use (55432, 56379), no secrets and no paid APIs. **Status: not executed on GitHub Actions** (that requires pushing). What *is* verified: GitHub's workflow and Dependabot schemas and `actionlint` pass; each job's commands pass locally (`ci_local.py`, 16/16) and the backend (230), agent (379 + ruff) and frontend (Node 22, `npm ci`) jobs pass in clean Linux containers with a fresh install. That rehearsal found two bugs that would have failed the first real run (invalid YAML in a step name; agent dev tools live in a PEP 735 dependency group). Actions are pinned to the commit of their latest official release; the action code itself was not audited.

## 4. Deployment smoke test (compose, isolated project name, throwaway volumes)

Verified on the real images: migrations ran on start and again idempotently after a restart with data kept; `alembic_version = 0002`;
backend healthy via its healthcheck; the agent healthcheck gates the backend; backend published on `127.0.0.1` only, **agent and
Redis publish nothing**; both containers run as UID 10001; from inside the network the agent answers `/health` 200 and `/health/ready`,
`/metrics`, `/research` with 401 (no/bad token), 200 with the right token, `/docs` 404; through the published port: security headers
present, `?token=` rejected (401), header auth 200, `/docs` 404. The project's `deep-research-platform_*` volumes were not touched (they belong to another repository's running stack).
No research query was run in compose (it would call real providers with no key).

Re-run on the milestone-3 compose file (strong generated secrets, pinned project name `lumen`, Redis limits): backend healthy and reporting `rate_limit: redis`; the effective project volumes are `lumen_postgres_data`/`lumen_redis_data`; 24 bad logins → 20 pass, 4 get 429 with `Retry-After`; Redis db 1 holds 21 keys, all hashed (none contains `@` or an address) with TTLs, db 0 (agent cache) separate; `ALLOW_INSECURE_DEV_DB=0` and a 48-character database password in the container; agent and Redis publish nothing; a full `stop`/`start` of every service keeps the user, conversation and message and the user can log in and read them; the shipped image exits 1 with a weak `DATABASE_URL`.

## 5. Requirements coverage (PRD §6)

F1 auth ✔ (backend + browser) · F2 conversations ✔ · F3 streaming progress ✔ (browser) · F4 eight tools ✔ (mocked upstreams; live shapes spot-checked once) ·
F5 blocks ✔ (all four types + fallback rendered in a real browser) · F6 citations ✔ · F7 follow-ups ✔ (mocked OpenAI) · F8 clarification ✔ ·
F9 persistence ✔ (incl. disconnect) · F10 failure handling ✔ (real outages) · F11 abuse controls ✔.

## 6. Gaps (not tested)

- **The GitHub Actions workflow has never run on GitHub.**
- **Rate-limit behaviour with real reverse proxies** (nginx/ALB/Cloudflare header formats, IPv6 prefixes at scale); only the resolver logic and simulated peers are tested.
- **Redis Cluster / Sentinel / managed Redis** (single instance only); behaviour of the Lua scripts under replica failover.
- **Rate-limit load behaviour** beyond an 80-thread burst; Redis memory under key-spraying attacks beyond the bounded-structure reasoning.
- **Rotation against a production-sized database** (the rehearsal DB has ~36 rows) and against PostgreSQL versions other than 16.

- **Frontend tests are jsdom-based**: real cookie policy, layout, the real Leaflet map, real SSE transport and cross-browser behaviour are covered only by the single-browser (headless Edge) e2e run; see `docs/FRONTEND_TESTING_PLAN.md` for the table of what cannot be covered. Firefox, Safari, real devices and screen readers are untested.
- **Cookie sessions:** `AUTH_MODE=cookie` (stage 4) and the expired-session cleanup job are not implemented, so they are not tested; concurrent multi-tab refresh is tested at the API level (409 grace) and in unit tests, not with several real tabs racing.
- Real LLM/search providers; paid-provider behaviour (rate limits, quotas, latency, cost).
- Browsers other than Chromium-based Edge; real devices; screen readers.
- TLS, reverse proxy, multi-replica, load/soak/performance, memory growth, very long conversations.
- Live DNS-rebinding behaviour; container image CVE scanning.
- Rate limiting accuracy under several workers.
- Source-map/CSP behaviour of a production static hosting setup.
