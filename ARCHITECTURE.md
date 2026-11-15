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
│  JWT in localStorage├────►│   SSE bridge · limits                       Deep Agents loop │
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
| Agent → Redis | Tool-result cache only (public data, no user identifiers). Redis is not published to the host. |

## 2. Request lifecycle (research query)

1. `POST /api/conversations/{id}/query` with `Authorization: Bearer <jwt>`.
2. Backend validates the token, applies per-user rate and concurrency limits, verifies **conversation ownership**, persists the user message and loads history (one transaction, off the event loop).
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
- Tokens: HS256 JWT with `sub` = user UUID, `iss`, `aud`, `iat`, `exp` (24h default), `jti`. Algorithm pinned; all claims required.
- Header-only bearer auth (the old `?token=` query parameter was removed).
- Login is rate limited per IP and locked per email after repeated failures; unknown emails take the same time and return the same response as wrong passwords.
- **Decision on HttpOnly cookies:** evaluated and deferred — see `SECURITY_REVIEW.md` §Session model. No half-migration exists in the code.

## 4. Data model

```
users(id uuid pk, email unique, password_hash, name, created_at)
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

| Service | Published | Notes |
|---|---|---|
| `backend` | `127.0.0.1:8080` | non-root, migrations on start, readiness healthcheck |
| `research-agent` | **none** (`expose` only) | requires `AGENT_API_KEY` |
| `postgres` | `127.0.0.1:5433` | development credentials; change for shared deployments |
| `redis` | **none** | memory-bounded LRU cache, no persistence |

`JWT_SECRET` and `AGENT_API_KEY` are required; compose refuses to start without them and the backend refuses weak or
placeholder secrets. Terminate TLS in a reverse proxy in front of the backend and the static frontend.

## 6. Failure behaviour

| Failure | Behaviour |
|---|---|
| PostgreSQL down | 503 `Service temporarily unavailable` (fast, no internals); `/health/ready` → 503; `/health` stays 200 |
| Agent down / 5xx / timeout | In-band `error` event with fixed wording; the question and the error are saved to the conversation |
| Redis down | Agent continues uncached (never crashes) |
| LLM / search provider down | Generic error event; search falls back Exa → Tavily → DuckDuckGo |
| Malformed or oversized agent event | Dropped/rejected; generic error; nothing stored from it |
| Client disconnect | Upstream cancelled, concurrency slot released, completed answers still saved |
| Expired token mid-session | 401 → frontend returns to sign-in |
| Rate limit | 429 + `Retry-After`; frontend shows a specific message |

## 7. Known limits (see `SECURITY_REVIEW.md` for the full list)

Rate limits are per process (multi-replica needs a shared store); the JWT lives in `localStorage`; no token revocation
before expiry; no email verification or password reset; no per-user conversation/storage quota.
