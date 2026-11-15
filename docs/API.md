# Backend API reference

Machine-readable schema: [`openapi.json`](openapi.json), generated from the application by `scripts/gen_openapi.py`. A backend test fails when it is stale, and another fails when an endpoint
is missing from the tables below. The running service does **not** serve `/docs` or `/openapi.json` on purpose (it would publish the whole API surface).

Base path: `/api`. JSON in and out. Field names in responses are **camelCase** (`createdAt`, `followUps`), except inside the SSE `blocks` event, which carries the agent's own snake_case payload (`follow_ups`).
Requests larger than 64 KiB are rejected with `413`. A research question is at most 4,000 characters.

## Authentication

Browsers use **HttpOnly cookie sessions** on a single origin; `AUTH_MODE=dual` (the default) also accepts the legacy `Authorization: Bearer <token>` header during a migration window.

| Cookie (development / production names) | Purpose | Flags |
|---|---|---|
| `lumen_at` / `__Host-lumen_at` | access token, 15 minutes | HttpOnly, SameSite=Lax, Path=/, Secure in production |
| `lumen_rt` / `__Secure-lumen_rt` | refresh token, rotated on every use | HttpOnly, SameSite=Strict, Path=/api/auth only |
| `lumen_csrf` / `__Host-lumen_csrf` | CSRF token for this session | readable by page script (by design) |

* **State-changing requests** (`POST`, `PATCH`, `DELETE`) authenticated by cookie must send the CSRF value in the **`X-CSRF-Token`** header and come from an allowed `Origin` (otherwise `403`). Bearer requests are Origin-checked but have no CSRF token.
* Send **`X-Auth-Mode: cookie`** on register/login to receive cookies **without** a body token.
* A bad Bearer token is never silently replaced by a cookie: it is `401`.
* Tokens in query strings are not accepted.
* A session ends after 24 hours of inactivity or 7 days in total, on logout, or on refresh-token reuse (the whole session is revoked).

## Common errors

| Status | Meaning | Notes |
|---|---|---|
| 401 | missing, invalid or expired credentials | `WWW-Authenticate: Bearer` |
| 403 | CSRF check failed, or the `Origin` is not allowed | |
| 404 | not found **or not yours** | other users' conversations are indistinguishable from missing ones |
| 409 | duplicate email (register); refresh token reused inside the 10 s grace window (another tab won) | |
| 413 | request body too large | |
| 422 | validation error | `detail` lists locations and messages, never the submitted input |
| 429 | rate limit, lockout or concurrency cap | `Retry-After` seconds |
| 503 | service busy, restarting, or (production) the rate-limit store is unreachable | `Retry-After` set; the database being busy is shed with 503, never a hung request |

Error bodies are `{"detail": "..."}` with fixed, user-safe text. Internal details are logged, not returned.

Default limits (all configurable): register 10 per hour per IP; login 20 per minute per IP and 5 failures per account (then a 15 minute lockout); 12 research requests per minute per user; 2 concurrent research
streams per user and 200 in total; 300 authenticated requests per minute per user.

## Endpoints

### Auth

| Method | Path | Request | Success | Errors |
|---|---|---|---|---|
| POST | `/api/auth/register` | `{email, password (8 to 72 bytes), name (1 to 80)}` | 200 `{id, email, name, token?}` and session cookies | 409, 422, 429 |
| POST | `/api/auth/login` | `{email, password}` | 200 `{id, email, name, token?}` and session cookies | 401, 422, 429 |
| GET | `/api/auth/me` | | 200 `{id, email, name}` | 401 |
| POST | `/api/auth/refresh` | refresh cookie (+ CSRF header) | 200 `{id, email, name}`, new cookies (rotated) | 401, 403, 409 |
| POST | `/api/auth/logout` | cookies (+ CSRF header when a session exists) | 204, cookies cleared, session revoked; idempotent | 403 |
| POST | `/api/auth/logout-all` | authenticated | 204, every session of the user revoked | 401, 403 |
| POST | `/api/auth/session/upgrade` | legacy Bearer token | 200, converts a pre-migration token into a cookie session; **only in `AUTH_MODE=dual`** (404 otherwise) | 400, 401, 404 |

`token` is omitted when the client sent `X-Auth-Mode: cookie` and in cookie-only mode.

### Conversations

| Method | Path | Request | Success | Errors |
|---|---|---|---|---|
| GET | `/api/conversations` | | 200 `[{id, title, createdAt, updatedAt}]`, newest first | 401 |
| POST | `/api/conversations` | `{title?}` (max 200) | 200 one conversation (default title "New chat") | 401, 403 |
| GET | `/api/conversations/{id}` | | 200 one conversation | 401, 404 |
| PATCH | `/api/conversations/{id}` | `{title}` (not blank, max 200) | 204 | 401, 403, 404, 422 |
| DELETE | `/api/conversations/{id}` | | 204 (messages deleted with it) | 401, 403, 404 |
| GET | `/api/conversations/{id}/messages` | | 200 `[{id, role, content, blocks, sources, followUps, createdAt}]` oldest first; `role` is `USER` or `ASSISTANT` | 401, 404 |

### Research (streaming)

| Method | Path | Request | Success | Errors |
|---|---|---|---|---|
| POST | `/api/conversations/{id}/query` | `{query}` (1 to 4,000 characters) | 200 `text/event-stream` | 401, 403, 404, 429, 503 |

The question is stored before the research starts. The stream (one event per message; `data` is one line of JSON):

| Event | When | Payload |
|---|---|---|
| `user_message` | first | `{id, createdAt}` of the stored question |
| `checkpoint` | zero or more times | `{status: "in_progress", content, tool?}`: human-readable progress, for example "Fetching bitcoin price..." |
| `blocks` | terminal (answer) | `{status: "completed", data: {query, blocks: [{template_id, data}], sources: [url], follow_ups: [{label, query, category}]}}` |
| `clarification` | terminal (the agent needs one more detail) | `{status, content}`: a single question |
| `error` | terminal (failure) | `{status: "failed", content}`: fixed, user-safe text |
| `persisted` | after `blocks` or `clarification` | `{messageId}` of the stored answer |
| `persist_error` | if saving failed | `{status: "failed", content}` |

Exactly one terminal event is sent. Block `template_id`s: `markdown`, `data-table`, `leaflet-map`, `insight-cards`; an unknown id renders as a collapsible JSON fallback in the UI.
If the client disconnects after the answer is complete, the answer is still saved. If the service stops while a research is running, the stream is cut and an "interrupted, please ask again" note is saved
as the answer. While the service is stopping, new research requests get `503` with `Retry-After`.

### Health

| Method | Path | Success |
|---|---|---|
| GET | `/health` | 200 liveness |
| GET | `/health/ready` | 200 `{status: "ready", database: "ok", rate_limit: "redis" \| "degraded-local" \| "memory"}`; 503 when the database is down |

## What this reference does not cover

* The **agent service** (`research-agent`) is internal: it is reachable only from the backend with a shared secret (`X-Internal-Token`) and is not part of the public API.
* Response examples are described, not recorded from a production system; the field lists were checked against `openapi.json`.
