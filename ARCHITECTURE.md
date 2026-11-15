# Lumen — Architecture

System-level architecture. The research agent's internals are documented in
[`research-agent/ARCHITECTURE.md`](research-agent/ARCHITECTURE.md); the visual system in [`DESIGN.md`](DESIGN.md);
threats and mitigations in [`SECURITY_REVIEW.md`](SECURITY_REVIEW.md).

## 1. Components and trust boundaries

```
 Browser (untrusted)                 Docker network (trusted)
┌────────────────────┐     ┌──────────────────────────────────────────────────────────────┐
│ React SPA          │     │  FastAPI backend ──X-Internal-Token──► Research agent        │
│  Vite · TypeScript │ TLS │   auth · persistence      (not published)   FastAPI          │
│ HttpOnly cookies   ├────►│   SSE bridge · limits                       Deep Agents loop │
└────────────────────┘     │        │                                    8 tools          │
        ▲                  │        ▼                                      │      │        │
        │ SSE              │   PostgreSQL 16                          Redis 7   external    │
        └──────────────────┤   users · conversations · messages     (cache)    APIs / LLM   │
                           └──────────────────────────────────────────────────────────────┘
```

| Boundary | Rule |
|---|---|
| Browser → backend | Only HTTP surface exposed to users. JWT bearer in the `Authorization` header. CORS restricted to configured origins. |
| Backend → agent | Server-to-server only. The agent is **not published** by compose and rejects any request without a valid `X-Internal-Token` (fail-closed when unconfigured). |
| Agent → outside world | All fetched content is **untrusted data**. URLs are validated (SSRF guard); responses are size- and time-limited. |
| Anything → PostgreSQL | Only the backend. All queries go through SQLAlchemy (parameterised). |
| Agent → Redis | Tool-result cache only (public data, no user identifiers), logical DB 0. Redis is not published to the host. |
| Backend → Redis | Shared rate-limit and concurrency state (logical DB 1), HMAC-hashed keys, every key has a TTL. See §6. |

## 2. Request lifecycle (research query)

1. `POST /api/conversations/{id}/query` (same origin) with the HttpOnly access cookie and the `X-CSRF-Token` header. (Legacy clients may still send `Authorization: Bearer`; see section 3a.)
2. Backend validates the cookie, the live server-side session and the CSRF token, applies per-user rate and concurrency limits, verifies **conversation ownership**, persists the user message and loads history (one transaction, off the event loop).
3. Backend opens a streaming request to the agent (`X-Internal-Token`, no redirects, bounded timeouts).
4. Agent runs the LangGraph/Deep Agents loop; each tool call becomes a `checkpoint`; results are converted into typed blocks.
5. Backend validates each agent event (known names, JSON object, block shape, size caps), **replaces any agent-supplied error text with a fixed message**, forwards it to the browser, and records the outcome.
6. The assistant message (or a safe error message) is persisted with retry; `persisted` is sent only after the commit succeeds (otherwise `persist_error`).
7. If the browser disconnects, the upstream agent request is cancelled; a *completed* answer is still saved to history.

### SSE events (backend → browser)

| Event | Payload | Terminal |
|---|---|---|
| `user_message` | `{id, createdAt}` | no |
| `checkpoint` | `{status, content, tool?}` | no |
| `blocks` | `{status, data:{query, blocks[], sources[], follow_ups[]}}` | result |
| `clarification` | `{status, content}` | result |
| `error` | `{status:"failed", content}` — fixed, user-safe wording | result |
| `persisted` | `{messageId}` | last |
| `persist_error` | `{status, content}` — answer shown but not saved | last |

Failure wording is stable: agent error / protocol error → "The research service hit an error…", agent unreachable → "…currently unavailable…", timeout → "…took too long…", stream ended with no result → "…interrupted…".

## 3. Authentication & sessions

- Passwords: bcrypt (passlib), 8–72 bytes (bcrypt's real limit; longer is rejected, not truncated).
- Tokens: HS256 JWT (PyJWT) with `sub` = user UUID, `iss`, `aud`, `iat`, `exp`, `jti`; cookie access tokens (15 min) also carry `sid`. Algorithm pinned; all claims required. Legacy Bearer tokens (24 h) are still accepted in `dual`/`bearer` mode.
- No credential in URLs (the old `?token=` query parameter was removed).
- Login is rate limited per IP and locked per email after repeated failures (shared across instances via Redis, §6); unknown emails take the same time and return the same response as wrong passwords.
- **HttpOnly cookie sessions are implemented** (section 3a; decisions in `SECURITY_REVIEW.md` section 2, plan in `docs/COOKIE_AUTH_MIGRATION_PLAN.md`).

## 3a. Sessions and cookies

One origin: the browser reaches the SPA and `/api` through the same host (production reverse proxy; Vite proxy in development).

| Cookie | Content | Flags |
|---|---|---|
| `lumen_at` (`__Host-lumen_at` in production) | 15-minute HS256 JWT with a session id (`sid`) | HttpOnly, SameSite=Lax, Path=/, Secure in production |
| `lumen_rt` (`__Secure-lumen_rt`) | opaque 256-bit refresh token (only its hash is stored) | HttpOnly, SameSite=Strict, Path=/api/auth, Secure in production |
| `lumen_csrf` (`__Host-lumen_csrf`) | per-session HMAC token | readable by script on purpose; SameSite=Lax |

* `auth_sessions` table (migration 0003): hash of the current and previous refresh token, `idle_expires_at` (24 h, extended at each refresh), `absolute_expires_at` (7 days), `revoked_at` + `revoked_reason`.
* Every request authenticated by the access cookie also checks that its session is live, so logout and `logout-all` take effect immediately.
* Refresh rotates the token with a compare-and-swap. A concurrent tab inside a 10 s grace gets 409 and retries; a replay outside the grace revokes the session.
* Unsafe methods need `X-CSRF-Token` equal to the HMAC for that session, and pass `OriginCheckMiddleware` (`Origin` must be an allowed origin; without it `Sec-Fetch-Site` must be same-origin/same-site/none).
* `AUTH_MODE`: `bearer` (legacy only) | `dual` (default; cookies plus legacy Bearer, body token suppressed when the client sends `X-Auth-Mode: cookie`) | `cookie`. A Bearer credential that is present but invalid is never retried as a cookie.
* Frontend: no token in web storage; boot calls `/auth/me` (refreshing once if the access cookie expired) behind a loading gate; legacy `localStorage` sessions are upgraded once via `/auth/session/upgrade` and purged.

## 4. Data model

```
users(id uuid pk, email unique, password_hash, name, created_at)
  ├─< auth_sessions(id uuid pk, user_id fk→users cascade, refresh_hash, prev_refresh_hash, idle_expires_at, absolute_expires_at,
  │                 revoked_at, revoked_reason, created_at, last_used_at, rotated_at, user_agent, ip_hash)
  └─< conversations(id uuid pk, user_id fk→users cascade, title, created_at, updated_at)
        └─< messages(id uuid pk, conversation_id fk→conversations cascade,
                     role ∈ {USER, ASSISTANT}, content, blocks jsonb, sources jsonb,
                     follow_ups jsonb, created_at)
```

Indexes: `users(email) unique`, `conversations(user_id)`, `conversations(user_id, updated_at)`,
`messages(conversation_id)`, `messages(conversation_id, created_at)`. Every read/write of conversations and messages is
scoped by the authenticated user's id (cross-user access returns 404, never 403).

### Migrations
Schema is managed by Alembic (`backend/alembic/`). The backend container runs `python -m app.migrate` before serving:
advisory-locked (safe with several replicas), idempotent, and able to **adopt** a database created by the former
`create_all` startup hook (stamps baseline `0001`, touches no data). A failing migration rolls back and stops startup.
`tests/test_migrations.py` also fails on model/migration drift.

## 5. Deployment topology (docker-compose)

The compose project name is pinned (`name: lumen`), so volumes are `lumen_postgres_data` / `lumen_redis_data` and can never collide with another checkout that happens to live in a folder with the same name (a real collision was found on the development machine; see `docs/DB_CREDENTIALS.md`).

| Service | Published | Notes |
|---|---|---|
| `backend` | `127.0.0.1:8080` | non-root, migrations on start, readiness healthcheck, uvicorn proxy-header handling off (client IP is resolved by the app, §6) |
| `research-agent` | **none** (`expose` only) | requires `AGENT_API_KEY` |
| `postgres` | `127.0.0.1:5433` | password REQUIRED (`POSTGRES_PASSWORD`, no default); see `docs/DB_CREDENTIALS.md` |
| `redis` | **none** | memory-bounded LRU, no persistence; DB 0 = agent cache, DB 1 = rate limits |

`JWT_SECRET`, `AGENT_API_KEY` and `POSTGRES_PASSWORD` are required (`python scripts/init_env.py` generates them); compose refuses to start without
them and the backend refuses weak or placeholder secrets and weak/default database passwords (opt-out only for local databases via `ALLOW_INSECURE_DEV_DB`). Terminate TLS in a reverse proxy in front of the backend and the static frontend.

## 6. Distributed rate limiting

Implemented in `backend/app/ratelimit.py`; verified with real Redis, with three separate backend processes sharing one Redis, and through the real compose stack.

| Limit | Default | Key | Where |
|---|---|---|---|
| Login attempts | 20 / min | client IP | `routers/auth.py` |
| Failed logins per account | 5 then locked 15 min | email (hashed) | `routers/auth.py` |
| Registrations | 10 / hour | client IP | `routers/auth.py` |
| Research queries (paid LLM) | 12 / min | user id | `routers/research.py` |
| Concurrent research streams | 2 per user, 200 global | user id / global | `routers/research.py` |
| Any authenticated request | 300 / min | user id | `deps.py` |

- **Atomic.** Each check is a single Lua script (sliding-window log in a ZSET, INCR+EXPIRE for lockouts, ZSET slots for concurrency); a burst of 80 concurrent requests grants exactly the limit. Time comes from the Redis server (`TIME`), so clock skew between instances cannot weaken a limit.
- **Hygiene.** Keys are namespaced and HMAC-hashed (no raw email, IP or user id is ever stored), every key has a TTL, memory per key is bounded by the limit. Concurrency slots expire after `research_timeout + 120 s`, so a crashed holder cannot lock anyone out.
- **Client IP.** `X-Forwarded-For` is believed only when the TCP peer is inside `TRUSTED_PROXY_CIDRS` (default: none). The chain is read from the right, skipping our own proxies; the first untrusted hop is the client, so prepended fake addresses change nothing. Malformed or oversized chains are ignored. IPv6 clients share a /64 bucket; IPv4-mapped IPv6 becomes IPv4.
- **Redis failure.** A single transient error is retried once; a second failure opens a 5 s circuit breaker (so a dead Redis does not slow requests). While it is open: `RATE_LIMIT_FAIL_MODE=local` (default) enforces the same limits **per process** (a bounded, degraded ceiling: replicas × limit), `closed` refuses login, register and research with `503` + `Retry-After`. Neither is ever unlimited. The generic per-user API limiter always degrades to local and never fails closed, so Redis cannot take ordinary reads down. The service stays ready; `/health/ready` reports `rate_limit: redis | degraded-local | memory`.
- **Startup.** The connection pool is bounded (64) and the Lua scripts are loaded before the first request; both were added after a cold 80-thread burst timed out and briefly exceeded a limit.
- **Memory mode.** With `REDIS_URL` empty (development/unit tests) only the in-process limiters run.

## 7. Failure behaviour

| Failure | Behaviour |
|---|---|
| PostgreSQL down | 503 `Service temporarily unavailable` (fast, no internals); `/health/ready` → 503; `/health` stays 200 |
| Agent down / 5xx / timeout | In-band `error` event with fixed wording; the question and the error are saved to the conversation |
| Redis down | Agent continues uncached (never crashes); backend limits fall back to per-process (or refuse, if fail-closed); both recover automatically |
| LLM / search provider down | Generic error event; search falls back Exa → Tavily → DuckDuckGo |
| Malformed or oversized agent event | Dropped/rejected; generic error; nothing stored from it |
| Client disconnect | Upstream cancelled, concurrency slot released, completed answers still saved |
| Access cookie expired (15 min) | 401 → frontend does one silent `POST /auth/refresh` (single-flight across concurrent requests) and replays the request once; only a refused refresh returns the user to sign-in |
| Session revoked / refresh token replayed | Refresh refused (401); cookies cleared; frontend returns to sign-in and other tabs sign out via `BroadcastChannel` |
| Rate limit | 429 + `Retry-After`; frontend shows a specific message |

## 8. Known limits (see `SECURITY_REVIEW.md` for the full list)

Sessions are HttpOnly cookies (section 3a); legacy Bearer tokens (dual mode) cannot be revoked before their 24 h expiry; XSS cannot steal the session but could still use it from the page; no email verification or password reset; no per-user conversation/storage quota; rate-limit state must be isolated from the agent cache's eviction policy before production (`docs/AWS_SECRETS_PLAN.md` section 7); development falls back to a per-replica ceiling during a Redis outage, production refuses instead (`RATE_LIMIT_FAIL_MODE=closed` is enforced).
