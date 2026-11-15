# HttpOnly-Cookie Authentication — Migration Plan

> ## STATUS (2026-11-15): stages 1-3 IMPLEMENTED and tested; stages 4-5 NOT done.
> The user approved the migration with this policy: same-origin architecture; 15-minute access session; refresh up to 7 days with rotation on use;
> 24 h inactivity; revocation on logout; HttpOnly + Secure in production; SameSite=Lax unless stricter is needed; CSRF protection; Origin validation.
> The sections below keep the original design rationale; where the build differs, **`SECURITY_REVIEW.md` section 2 and `ARCHITECTURE.md` section 3a
> describe what actually exists**. The product runs in `AUTH_MODE=dual` (cookies plus legacy Bearer); it is **not** cookie-only yet (stage 4).

| Stage | Status |
|---|---|
| 1-2 Backend (migration 0003, cookies, CSRF, Origin check, refresh/logout/logout-all/upgrade, dual mode) | **Done** - 364 backend tests (sessions, CSRF, config, auth, cleanup) |
| 3 Frontend cookie flow + e2e | **Done** - 253 frontend tests (99.7 % statements); browser e2e 61/61 including real cookie-jar flags, cross-site CSRF/login-CSRF attacker page, revocation, replay of a copied cookie, expiry + silent refresh, legacy-session upgrade |
| 4 Cookie-only (`AUTH_MODE=cookie`, drop Bearer and body token) | **Not done** - deliberately after the migration window; legacy Bearer tokens cannot be revoked before their 24 h expiry |
| 5 Hardening | **Partly done:** expired-session cleanup is implemented and tested (`app/cleanup.py`, section 12). **Not done:** CSP/HSTS at the hosting layer, "active sessions" UI, security-event alerting |

## 1. Why, and when

**Today (verified in code):** `POST /api/auth/login|register` returns `{token, id, email, name}`; `frontend/src/store/auth.ts` writes `auth_token` and `auth_user`
to `localStorage`; `lib/api.ts` and `lib/sse.ts` read the token and send `Authorization: Bearer …`; `backend/app/deps.py` accepts the **header only**
(`?token=` was removed); JWT is HS256, `exp` 24 h (`jwt_expiration_minutes=1440`), claims `sub/iss/aud/iat/exp/jti`, no server-side session, no revocation.

**Risk being addressed:** any script running in the page (XSS, a compromised npm dependency, a malicious browser extension content script) can read
`localStorage` and **exfiltrate a token that stays valid for up to 24 h from anywhere**. An HttpOnly cookie cannot be read by script.

**Why it was deferred** (`SECURITY_REVIEW.md` §2): cross-cutting change, frozen frontend, no frontend test framework, and a half-migrated auth layer is worse than either end state.
Current XSS exposure is low (React escaping, no raw HTML markdown, no `dangerouslySetInnerHTML`).

**When it becomes worth doing:** before real multi-user/public hosting, before adding any feature that renders richer user/third-party HTML,
or when a customer/compliance review requires non-script-readable session credentials.

## 2. What it does NOT solve (read this first)

- **XSS can still act as the user.** Script in the page can call `fetch("/api/…", {credentials:"include"})` with the cookie attached and, if it can read the
  non-HttpOnly CSRF cookie/header value, can pass the CSRF check. HttpOnly removes *credential theft*, not *in-session abuse*. A strict **CSP** (`default-src 'self'`,
  no `unsafe-inline` scripts, pinned third-party hosts for Leaflet/fonts/tiles) and dependency hygiene remain necessary.
- It does not stop credential stuffing, phishing, or a stolen *device*.
- It adds new risks that must be tested: CSRF, session fixation, refresh-token theft/replay, CORS-with-credentials mistakes, cookie-scoping mistakes.
- It does not by itself add: password reset, email verification, MFA, per-device session UI.

## 3. Topology decision (drives every cookie attribute)

| | A. Same origin via reverse proxy (**recommended**) | B. Separate origins, same site (`app.example.com` + `api.example.com`) | C. Different registrable domains |
|---|---|---|---|
| Browser sees | one origin, `/api/*` proxied | two origins, one *site* | cross-site |
| CORS | not needed in production (dev uses the Vite proxy, also same-origin) | required with credentials | required with credentials |
| Cookie scope | host-only, `Path=/` → **`__Host-` prefix possible** | host-only on the API host → `__Host-` possible, `Domain` must stay unset | needs `SameSite=None; Secure` |
| SameSite | `Strict` works (see §4) | `Strict`/`Lax` work (same site) | `None` → **fails under third-party-cookie blocking**; not viable |
| CSRF exposure | lowest | medium (cross-origin XHR allowed by CORS config → needs exact origin allow-list) | highest |
| Complexity | needs a proxy that serves the SPA and forwards `/api` (the repo has none today; dev already proxies `/api` via `vite.config.ts`) | CORS config + `withCredentials`/`credentials:"include"` | not recommended |

**Recommendation: A.** The repository's own development setup (`frontend/vite.config.ts` proxies `/api` to the backend) already behaves like A.
The e2e harness currently runs the browser against a *different origin* than the API (`VITE_API_URL=http://127.0.0.1:18080/api`), i.e. B-like; it would be
switched to the proxy form (or kept as a B test) in Stage 3.

## 4. Target design

### 4.1 Token model — two options

**Option 1 (recommended for this app): opaque server-side session.** One random 256-bit session id in an HttpOnly cookie; the server stores only its hash.
Every request already does a DB lookup (`db.get(User, …)` in `deps.py`), so adding the session row costs one indexed read and gives **instant revocation**,
idle + absolute timeouts, and no JWT parsing on the cookie path. Fewer moving parts, no refresh dance.

**Option 2: short-lived access JWT + rotating refresh token with reuse detection** (what the brief asked to cover; use if stateless access validation at an
edge/another service is required). Specified fully below; Option 1 is the same table minus the access/refresh split.

### 4.2 Cookies (Option 2 values; Option 1 uses only the session cookie)

| Cookie | Content | HttpOnly | Secure | SameSite | Path | Lifetime |
|---|---|---|---|---|---|---|
| `__Host-lumen_at` | access JWT (existing `issue_token` + `sid` claim) | yes | yes | Strict | `/` | **15 min** (`access_token_minutes`) |
| `__Secure-lumen_rt` | opaque refresh token (`secrets.token_urlsafe(32)`) | yes | yes | Strict | `/api/auth` | idle 24 h sliding, absolute 30 d (decision §14) |
| `__Host-lumen_csrf` | signed CSRF token (§5) — **readable by JS on purpose** | **no** | yes | Strict | `/` | same as session |

**`__Host-` prefix:** the browser only accepts it when the cookie has `Secure`, `Path=/`, **no `Domain`**, and is set from a secure origin. Therefore:
- **Feasible** in topology A/B over HTTPS.
- **Not possible** for a refresh cookie scoped to `Path=/api/auth` (the prefix forces `Path=/`) → that one uses `__Secure-` (or accept `Path=/` and `__Host-`; trade-off: the refresh cookie is then sent on every request).
- **Not possible over plain `http://`** (dev). Dev would use un-prefixed names with `Secure=false`, controlled by `COOKIE_PREFIX`/`COOKIE_SECURE` settings; staging/production must use the prefixed names.
  Whether a given browser accepts `Secure` cookies from `http://localhost` varies by browser/version (Chromium and Firefox treat localhost as trustworthy; Safari historically does not) — **not verified in this environment**; the Vite proxy path avoids the question by using `Secure=false` in dev.

**SameSite:** `Strict` is workable because the SPA's HTML/JS/CSS needs no authentication; after a cross-site link navigation the page loads without the cookie and the
JS's own same-site `fetch` calls then carry it. `Strict` also protects the (non-idempotent) `POST /…/query` SSE call. Use `Lax` only if a future flow needs a
cookie on cross-site top-level navigations (e.g. OAuth/email-link sign-in). `None` is rejected (§3 column C). *Caveat for tests:* `localhost:5173` and `localhost:8080` are the **same site**
(ports are ignored), so SameSite offers no protection between local ports — the Origin check (§5) must carry that case.

**Always:** `Secure` (config-driven, true in prod), never set `Domain`, `Cache-Control: no-store` on auth responses (already applied to `/api/*` by `SecurityHeadersMiddleware`), `ENABLE_HSTS=true` in production.

### 4.3 Server-side session table (new Alembic migration `0003`)

```sql
-- sketch; to be reviewed as a normal autogenerate + hand-edit
CREATE TABLE auth_sessions (
  id              uuid PRIMARY KEY,
  user_id         uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  family_id       uuid NOT NULL,                -- all refresh tokens descended from one login
  refresh_hash    bytea NOT NULL UNIQUE,        -- sha256(token); the raw token is never stored
  created_at      timestamptz NOT NULL,
  last_used_at    timestamptz NOT NULL,
  idle_expires_at timestamptz NOT NULL,
  absolute_expires_at timestamptz NOT NULL,
  rotated_at      timestamptz,                  -- set when this token was exchanged for the next one
  revoked_at      timestamptz,
  revoked_reason  text,                         -- logout | reuse_detected | admin | password_change
  user_agent      text,                         -- truncated
  ip_hash         bytea                         -- HMAC of client IP (no raw IP)
);
CREATE INDEX ix_auth_sessions_user ON auth_sessions (user_id) WHERE revoked_at IS NULL;
CREATE INDEX ix_auth_sessions_family ON auth_sessions (family_id);
```
Downgrade = `DROP TABLE auth_sessions` (no user data lost). A periodic cleanup deletes rows past `absolute_expires_at` + a retention margin.

### 4.4 Lifecycle (Option 2)

| Event | Behaviour |
|---|---|
| Login / register | verify credentials (existing rate limiting + lockout unchanged), create `family_id`, session row, set the three cookies; response body no longer needs `token` (kept only in the dual-mode window, §9) |
| Authenticated request | `get_current_user` reads `__Host-lumen_at`; validates JWT; checks the session row (`sid`) is not revoked/expired (one indexed read; this is what makes revocation immediate) |
| Access token expiry (15 min) | client gets 401 → single-flight `POST /api/auth/refresh` (cookie sent automatically, CSRF header required) → new access + **new refresh token**, old row marked `rotated_at` |
| **Refresh reuse detection** | presenting a refresh token whose row already has `rotated_at` (or unknown but matching a known family) ⇒ **revoke the entire family** (`reuse_detected`), clear cookies, 401; log a security event (no token material). A short (few-second) reuse grace window can be added to tolerate two tabs refreshing at once — decision §14 |
| Idle / absolute timeout | `idle_expires_at` slides on each refresh; `absolute_expires_at` is fixed at login. Either passing ⇒ 401 → login |
| Logout | `POST /api/auth/logout` (CSRF required): revoke the session/family, clear all cookies; the UI resets state and tells other tabs (§8) |
| Logout everywhere / password change | `POST /api/auth/logout-all` and (when a password-change endpoint exists — it does **not** today) revoke all of the user's sessions |
| Deleted user | session rows cascade; access validation already returns 401 for a missing user |

Option 1 collapses this to: login creates a row and sets one session cookie; each request validates the row (sliding idle, absolute cap); logout revokes it; no refresh endpoint, no families.

## 5. CSRF design

Cookie-authenticated requests are ambient credentials, so state-changing calls need a second factor the browser does not add automatically.

**Chosen: signed double-submit token + Origin verification + custom-header requirement.**
- `__Host-lumen_csrf = base64url(random16) + "." + HMAC_SHA256(csrf_secret, session_id + "." + random16)` — readable by the SPA, **bound to the session** so a token from another session/attacker is rejected, and signed so it cannot be forged without the server secret (`csrf_secret` is a *new* setting, distinct from `jwt_secret`).
- SPA copies it into header `X-CSRF-Token` on every `POST/PUT/PATCH/DELETE`. Server requires header == cookie **and** a valid signature for the current session.
- A custom header cannot be sent cross-origin without a CORS preflight, which a disallowed origin fails (§6) — this alone blocks classic forged forms and simple XHR.
- **Origin check:** for unsafe methods, if `Origin` is present it must match the allow-list (`cors_allowed_origins`); if absent, require `Sec-Fetch-Site: same-origin|same-site|none` where the header exists, else fall back to the token alone. Required because SameSite does not separate `localhost` ports.
- Alternative considered — **synchronizer token** stored server-side per session (fetch via `GET /api/auth/csrf`): stronger against cookie-injection from sibling subdomains, but adds a round trip and storage. Cookie-injection from a sibling subdomain is a reason to prefer `__Host-` cookies and the session-bound signature.

**Scope:** all `POST/PUT/PATCH/DELETE` under `/api/` that rely on cookie auth: `POST /api/conversations`, `PATCH|DELETE /api/conversations/{id}`, **`POST /api/conversations/{id}/query` (the SSE call)**, `POST /api/auth/logout`, `/logout-all`, `/refresh`.
`POST /api/auth/login` and `/register` have no session yet: protect with the Origin check + existing IP/email rate limits (login-CSRF is a real attack: a forged login signs the victim into the attacker's account). `GET` routes are exempt and must stay side-effect free (verified: none mutate today).
Requests authenticated by an `Authorization: Bearer` header (dual-mode window) are not CSRF-vulnerable and skip the token check.

**SSE specifics:** `postSse` uses `@microsoft/fetch-event-source`, which forwards `RequestInit` to `fetch`. Same-origin (topology A) needs nothing extra for the cookie (`credentials` defaults to `same-origin`); topology B needs `credentials: "include"`. In both, add the `X-CSRF-Token` header. `onopen` already maps non-2xx to `SseHttpError`; a 401 there should attempt **one** refresh + one retry before redirecting. The stream itself is a normal POST response; cookies are sent only on the initial request.

## 6. CORS with credentials (only needed for topology B/dev-direct)

Current `main.py`: `CORSMiddleware(allow_origins=settings.cors_origins, allow_credentials=True, allow_methods=[GET,POST,PATCH,DELETE,OPTIONS], allow_headers=[Authorization, Content-Type, Accept], max_age=600)`; `config.cors_origins` already **refuses `*`**.
Needed changes: add `X-CSRF-Token` to `allow_headers`; keep **explicit origins only** (never reflect arbitrary `Origin`); confirm `Vary: Origin` is emitted (test it); `Access-Control-Allow-Credentials: true` only for allow-listed origins; preflight must **fail** for any other origin (no `Access-Control-Allow-Origin`). In topology A production the CORS surface can be disabled entirely (`cors_allowed_origins` empty → no middleware), which is the safest state.

## 7. Backend changes, per file

| File | Change |
|---|---|
| `backend/app/config.py` | `auth_mode: "bearer"\|"dual"\|"cookie"` (default `bearer` until Stage 4); `cookie_secure`, `cookie_prefix`, `cookie_samesite` (default `strict`), `access_token_minutes` (15), `session_idle_minutes`, `session_absolute_days`, `refresh_reuse_grace_seconds`, `csrf_secret` (validated like `jwt_secret`: ≥32 chars, no placeholder words, refuse equal to `jwt_secret`) |
| `backend/app/models.py` + `alembic/versions/0003_auth_sessions.py` | `AuthSession` model and migration from §4.3; `tests/test_migrations.py` drift test must stay green |
| `backend/app/security.py` | refresh-token generator + `sha256` hashing; `issue_token(user, sid)` adds `sid`; CSRF sign/verify (constant-time compare); cookie-setting helpers (single place that decides names/flags) |
| `backend/app/deps.py` | `_extract_token`: Bearer header (dual/bearer modes) **or** access cookie (dual/cookie modes); in `cookie` mode the header is **ignored**; session-row validation; unsafe-method CSRF + Origin enforcement for cookie-authenticated requests only; keep the per-user API limiter (`api_user_limiter`) |
| `backend/app/routers/auth.py` | login/register set cookies (and, in `dual`, still return `token`); new `POST /refresh`, `POST /logout`, `POST /logout-all`, `POST /session/upgrade` (dual window only, §9); `GET /me` unchanged; rate limits keyed per IP and per session/family for `/refresh` |
| `backend/app/schemas.py` | `AuthResponse.token` optional; drop it in `cookie` mode |
| `backend/app/middleware.py` (or a dependency) | Origin/Sec-Fetch-Site verification for unsafe methods under `/api/`; keep it pure-ASGI so SSE is not buffered |
| `backend/app/main.py` | CORS `allow_headers` += `X-CSRF-Token`; allow-list from config; optional "no CORS in topology A" branch |
| `backend/app/ratelimit.py` (Redis-backed work in progress) | add limiter names for `refresh` and CSRF failures; failure policy unchanged (never unlimited) |
| `docker-compose.yml` / `.env.example` | new required secret `CSRF_SECRET`; `COOKIE_SECURE`, `ENABLE_HSTS` documented for production |
| Docs | `ARCHITECTURE.md` §3, `SECURITY_REVIEW.md` §2 (status → implemented only after §13), `TEST_PLAN.md`, `README.md` API table |

## 8. Frontend changes, per file (design stays frozen — logic only)

| File | Change |
|---|---|
| `src/lib/api.ts` | remove Bearer injection after the window; `withCredentials: true` only when `VITE_API_URL` is cross-origin; request interceptor adds `X-CSRF-Token` (read from `document.cookie`) on unsafe methods; response interceptor: on 401 run **single-flight** `POST /auth/refresh`, retry the original request once, otherwise purge state and go to `/login`; **never** retry `/auth/login|register|refresh` |
| `src/lib/sse.ts` | `credentials: "include"` (topology B) / default (A); add `X-CSRF-Token`; remove `Authorization`; on 401 in `onopen`: one refresh + retry, else the existing purge/redirect; keep `SseHttpError` |
| `src/store/auth.ts` | **stop using `localStorage` for the token and identity.** Add `status: "loading"\|"authenticated"\|"anonymous"`; boot calls `GET /auth/me` once (cookie sent automatically) → `authenticated` or `anonymous`; `login/register` set `user` from the response body (no token handling); `logout()` calls `POST /auth/logout` (best-effort), resets `useChat` (already done today), and broadcasts `{type:"logout"}` on a `BroadcastChannel("lumen-auth")` (fallback: a `storage` event on a throw-away key) so other tabs reset too; on boot, **purge legacy `auth_token`/`auth_user`** keys |
| `src/App.tsx` | `Protected` renders a neutral loading state while `status==="loading"` — prevents the redirect flicker that the current synchronous-`localStorage` hydration avoids; `anonymous` → `<Navigate to="/login">`; `hydrate()` becomes the `/me` boot call |
| `src/pages/Login.tsx`, `Register.tsx` | no logic change beyond the store API (errors via `apiError` stay) |
| `src/components/ChatPanel.tsx`, `ConversationSidebar.tsx` | no change expected (the 401/429 message mapping and `select(...).then(ok=>…)` redirect remain); verify the new refresh path does not double-handle 401 |
| `e2e/scenario.mjs` | the check "JWT stored, no PII in token" becomes "**no token in `localStorage`; auth cookie present with `HttpOnly`, `Secure`(prod)/`SameSite` flags**" (read through CDP `Network.getCookies`); expired-session step simulates cookie expiry/revocation instead of planting a bad `auth_token` |

**Browser refresh:** the in-memory store is empty, `status="loading"`, `/me` succeeds via the cookie → no login bounce. **Protected routes:** unchanged semantics, now driven by `status`. **Multi-tab logout:** BroadcastChannel as above; a tab that missed the message learns on its next 401.

## 9. Migrating existing sessions

Existing tokens live ≤ 24 h (`jwt_expiration_minutes=1440`), so the exposure window of the old mechanism after deploy is bounded.

| Option | How | Pros | Cons |
|---|---|---|---|
| **Dual-mode window + upgrade (continuity)** | `auth_mode=dual` for **N = 2 days** (≥ token lifetime + margin): backend accepts Bearer *and* cookies; new frontend, on boot, if a legacy `auth_token` exists → `POST /auth/session/upgrade` (Bearer-authenticated) → server issues cookies → frontend deletes `auth_token`/`auth_user` | no forced sign-out | more code, a temporary endpoint, a period where header tokens (the thing being retired) are still honoured |
| **Forced re-login (simplest)** | deploy `cookie` mode directly; old tokens stop working; users sign in again; frontend purges legacy keys on boot | least code, no dual path | everyone is signed out once (cost is small: tokens already expire daily) |

Recommendation: forced re-login if the product has few active sessions; otherwise dual-mode with N=2 days. **Purging:** `localStorage.removeItem("auth_token"|"auth_user")` runs unconditionally on every boot of the new frontend until removed in a later release.
**Rollback:** the `auth_mode` flag. `cookie` → `dual` (accept both again; login returns the body token again so the *old* frontend keeps working) → `bearer` (cookies ignored). `auth_sessions` can stay or be dropped by downgrade `0003`; no user data depends on it. Roll the **backend forward first** and the **frontend second** so a rollback never strands a frontend without a backend that understands it.

## 10. Rate limiting and abuse interplay

Existing controls stay: per-IP login/register limits, per-email lockout, per-user API and query limits, per-user concurrent streams (now being moved to Redis).
Add: `/refresh` limited per session family and per IP (refresh-token guessing is infeasible at 256 bits, but loops/bugs must not hammer the DB); CSRF/Origin failures counted and logged (no token values); `/session/upgrade` limited like login. The cookie path must not bypass any limiter: limits are keyed on user/IP, not on credential type.

## 11. Cross-site and security test plan

Run at backend level (`TestClient` with crafted `Origin`/`Sec-Fetch-Site`/cookies) **and** in a real browser (two origins on different *sites*, e.g. `http://localhost:<port>` as the app and `http://127.0.0.1:<port2>` as the attacker — `localhost` vs `127.0.0.1` are different sites; two `localhost` ports are *not*).

| # | Case | Expected |
|---|---|---|
| 1 | Forged HTML form `POST` from the attacker origin to `/api/conversations` (browser sends cookies if SameSite allows) | rejected (403/401); no row created. Verify both with `SameSite=Strict` (cookie not sent) and with SameSite temporarily relaxed (CSRF check still blocks) |
| 2 | Cookie present, **missing** `X-CSRF-Token` on `POST/PATCH/DELETE` and on `…/query` | 403, no side effect |
| 3 | Token present but **mismatched** with cookie / signed for a different session / bad signature / expired | 403 |
| 4 | **Origin spoof:** `Origin: http://evil.example`, valid cookie + valid token | 403 (Origin allow-list) |
| 5 | Same-site different port on localhost (`Origin: http://localhost:9999`) | 403 — proves the Origin check, since SameSite cannot |
| 6 | `Origin` absent + `Sec-Fetch-Site: cross-site` | 403; absent both → token alone decides (documented behaviour) |
| 7 | **CORS preflight** from a disallowed origin (`OPTIONS` with `Access-Control-Request-Headers: x-csrf-token`) | no `Access-Control-Allow-Origin`; and for an allowed origin `Allow-Credentials: true`, `Vary: Origin`, never `*` |
| 8 | **Cookie flag assertions** on login/refresh/logout responses | `HttpOnly` on access/refresh; **absent** on the CSRF cookie; `Secure`, `SameSite=Strict`, correct `Path`, no `Domain`; `__Host-` names in prod config; logout sends expiring cookies |
| 9 | **XSS cannot read the session cookie** (inject `document.cookie` read in a test page) | access/refresh cookies not visible; CSRF cookie visible (by design) |
| 10 | …but script **can** make same-origin calls | documented residual risk; assert CSP header blocks inline script on the SPA host (tests the hosting layer, not this repo today) |
| 11 | **Replay after logout / revocation:** reuse the old access cookie after `logout`; reuse old refresh | 401 immediately (session row revoked) |
| 12 | **Refresh reuse detection:** use refresh token R1 → get R2; replay R1 | 401 **and the whole family revoked** (R2 stops working); event logged |
| 13 | Two tabs refresh simultaneously | at most one rotation wins; with a grace window the loser succeeds without a family revoke; without it, define and test the exact behaviour |
| 14 | Expiry: idle timeout, absolute timeout, access-token expiry mid-SSE-stream | 401 → single refresh → retry once; otherwise login; no refresh loop |
| 15 | **Login CSRF:** forged cross-site login | blocked by Origin check |
| 16 | Dual mode: Bearer header requests with no CSRF token still work and are **not** CSRF-checked; cookie requests are | as stated |
| 17 | `cookie` mode: a valid Bearer token in the header is **ignored** | 401 |
| 18 | Existing regression suite (header-only, no `?token=`, IDOR, rate limits, outage tests) | all still pass |
| 19 | Multi-tab logout via BroadcastChannel; browser refresh keeps the session; protected route has no login flicker | e2e |
| 20 | Legacy `localStorage` keys purged on first boot | e2e |

## 12. Staged rollout (the app works after every stage)

| Stage | Change | App state |
|---|---|---|
| 0 | Approvals (§14). **Recommended: land the frontend test framework first** (`FRONTEND_TESTING_PLAN.md`) so the auth store/interceptor rewrite is guarded | unchanged |
| 1 | Backend additive: migration `0003`, new settings (default `bearer`), CSRF/cookie helpers, **no behaviour change**; backend tests extended | identical to today |
| 2 | Backend `dual`: login/register also set cookies and still return the body token; `deps` accepts Bearer or cookie; CSRF enforced only for cookie auth; `/refresh`, `/logout`, `/logout-all`, `/session/upgrade`; CORS header allow-list | the **old frontend keeps working**; new endpoints unused |
| 3 | Frontend cookie flow (`api.ts`, `sse.ts`, `store/auth.ts`, `App.tsx`) + unit tests + updated e2e; optional upgrade of legacy sessions | new frontend on cookies; Bearer still accepted server-side |
| 4 | After the window (N days, §9): `auth_mode=cookie`, drop body token and Bearer acceptance, delete `session/upgrade`, remove legacy frontend purge after a further release | cookie-only |
| 5 | Hardening: CSP + HSTS on the hosting layer, `__Host-` names in production, cleanup job, optional "active sessions" UI/endpoint, security event logging/alerts | production posture |

Each stage is independently revertible by redeploying the previous build and/or flipping `AUTH_MODE`.

## 13. Acceptance criteria (all must be demonstrated, not asserted)

1. No credential-bearing value is readable by script: `localStorage`/`sessionStorage` contain no token; session cookies are `HttpOnly` (verified through the browser cookie jar).
2. Every case in §11 passes in both unit tests and the real-browser run; the prior 230 backend + 379 agent + 42 browser + 39 outage checks still pass.
3. A forged cross-site request cannot change state, including the SSE `POST …/query` and login.
4. Logout and revocation take effect on the next request; refresh reuse revokes the family.
5. Browser refresh and multi-tab behaviour work without login flicker or loops.
6. Legacy tokens are rejected in `cookie` mode and legacy `localStorage` keys are purged.
7. `SECURITY_REVIEW.md` §2 is updated **after** the evidence exists, and still lists what this does not solve (§2 above).

## 14. DECISIONS NEEDING USER APPROVAL (before any work)

1. **Deployment topology:** A (same origin via reverse proxy — recommended), B (same site, two origins), or "undecided"? (Determines `SameSite`, `__Host-`, CORS.)
2. **Token model:** Option 1 opaque server session (recommended, simpler) or Option 2 short access JWT + rotating refresh with reuse detection?
3. **Session lifetime:** idle timeout (suggest 24 h sliding?) and absolute cap (suggest 30 d?), access-token life (15 min if Option 2)? Is a short refresh-reuse grace window acceptable?
4. **Existing sessions:** keep a dual-mode migration window (N = 2 days) with the upgrade endpoint, or force a one-time re-login?
5. **Prerequisite:** require the frontend test framework to land first? (Recommended yes — it needs its own approval, see `FRONTEND_TESTING_PLAN.md` §10.)
6. **Production hosting:** who provides TLS, the proxy, CSP and HSTS? (Cookie `Secure` and `__Host-` depend on it.)
7. **New secret:** approve a distinct `CSRF_SECRET` required at startup.

## 15. Effort and risk (qualitative)

| Area | Effort | Risk | Notes |
|---|---|---|---|
| Backend sessions + cookies + CSRF + migration | Medium–Large | Medium | New attack surface; mitigated by §11 tests and the dual mode |
| Frontend store/interceptor/SSE rewrite | Medium | Medium–High without frontend tests; Medium with them | Refresh single-flight and SSE retry are the subtle parts |
| E2E/harness updates | Small–Medium | Low | Cookie assertions via CDP; two-origin attacker page |
| Docs/ops (proxy, TLS, CSP) | Medium | High if skipped | Secure/`__Host-` and CSP depend on the hosting layer, which the repo does not define |
| Overall | **Large** (a dedicated milestone) | Manageable if staged | The security gain is real but bounded (§2) |

## 16. Items I could not verify while writing this plan

- Cookie behaviour of `Secure`/`__Host-` on `http://localhost` per browser (see §4.2) — needs a real-browser check in Stage 2/3.
- That Starlette's `CORSMiddleware` emits `Vary: Origin` in every credentialed case — to be asserted by test case 7, not assumed.
- Whether the parent's in-progress Redis rate limiter changes will expose a stable per-request client-IP resolver to reuse for `ip_hash` and `/refresh` limits (the plan assumes it will).
- Production hosting layout (no reverse-proxy config exists in the repository).

## 12. Expired-session cleanup (implemented)

`auth_sessions` rows were never deleted. `backend/app/cleanup.py` now removes a row once its session **ended** (earliest of absolute expiry, idle expiry, revocation time) more than `SESSION_PURGE_RETENTION_DAYS` (default 7) ago. Live sessions are never touched; recent dead rows stay for a week as reuse-detection evidence.

* In-process loop: the API runs a pass every `SESSION_PURGE_INTERVAL_SECONDS` (default 3600; `0` disables; not started in `AUTH_MODE=bearer`). The first pass runs about a minute after start.
* One-shot: `python -m app.cleanup` (exit 0 on success) for cron or a scheduled container/ECS task. If you use that instead, set the interval to `0`.
* Several replicas are safe: a PostgreSQL advisory lock admits one pass at a time (others log "skipped"), deletes are batched at 1000 rows, and the delete is idempotent. The lock is taken on a dedicated connection; releasing it on a pooled one leaked it during development, and a regression test now checks `pg_locks`.
* Tested (9 tests in `test_sessions.py`): only long-dead rows go (absolute, idle and revoked each), live and recently ended rows stay, a working session survives, batching, lock contention skips and then recovers, no lock left behind over 25 passes, the CLI, a failing pass does not stop the loop, settings validation. Mutating the "earliest of" rule to "latest" makes the main test fail. **Not tested:** behaviour on a table with millions of rows (batched deletes should hold up; not measured).

## 13. Runbook: dual to cookie-only (stage 4). Not executed; nothing switches by itself

The default stays `AUTH_MODE=dual`. Nothing in this repository changes the mode; an operator sets it.

**What the switch does:** Bearer tokens are rejected, the login/register body no longer carries a token, and `/auth/session/upgrade` returns 404. **Cookie sessions keep working untouched**: the session rows, cookies and user data are the same in both modes, so existing cookie users notice nothing.

**Who is affected:** only a browser still holding a pre-migration `localStorage` token that has not opened the new frontend since. Such a token lives at most 24 h, and the new frontend upgrades it to a cookie session on first load while the mode is `dual`. After the switch that user signs in once. No data is lost.

**Preconditions (all must hold):**
1. The new frontend has been live for at least 48 h (two token lifetimes), so every active browser has loaded it and upgraded. After that no legitimate client sends a Bearer token.
2. API clients that use Bearer on purpose (scripts, tests, anything outside the web app) are identified and moved, or the owner accepts they stop working. None are known in this repository.
3. `COOKIE_SECURE=true` behind HTTPS in production (already enforced by the production validator), and `CSRF_SECRET` set.
4. No Bearer-authenticated request for the last 24 h. **Measure it with `python -m app.legacy_usage`** (`backend/app/legacy_usage.py`): per-day counts of accepted Bearer tokens, split into `legacy` (sid-less, pre-migration) and `session` (session-bound), plus a last-seen timestamp. Counts are batched in memory and stored in Redis (best-effort, never affects a request; no user ids, IPs or tokens). Zero for two full days with the new frontend live is the signal.

**Steps:** (a) deploy this backend and frontend still in `dual`; (b) wait out precondition 1 and confirm precondition 4; (c) take a database snapshot (rollback does not need it, this is routine); (d) set `AUTH_MODE=cookie` and restart the backend (config is read at start); (e) check that `/health/ready` is ready, a login sets `lumen_at`/`lumen_rt`/`lumen_csrf`, a Bearer call returns 401, an existing browser session still works, and logout revokes.

**Rollback:** set `AUTH_MODE=dual` and restart. Cookie sessions are unaffected; the body token returns for clients that ask for it. Backend first, frontend second, as in section 9.

**Do not** switch while any of the preconditions is unconfirmed. Switching is a configuration change, so it needs your explicit go-ahead at the time.

## 14. Verification matrix (fresh run on 2026-11-15, after the authentication and database-session changes)

Backend 364 tests passed; browser end-to-end 61/61; failure injection 39/39; multi-instance 17/17 (**15/17 on the first run**, 17/17 on the immediate rerun; the two misses were one extra request passing the shared IP limit while the logs show the Redis 0.25 s timeout falling back to local limits on a machine short of memory. The limiter code was not changed. Not proven to be the sole cause).

| Requirement | Evidence (test names in `backend/tests/` unless stated) | Not covered |
|---|---|---|
| Access-token expiry | `test_access_token_expiry_is_recovered_by_refresh`; e2e "reload after the access cookie expired stays signed in (silent refresh)" | |
| Refresh rotation | `test_refresh_rotates_the_token_and_issues_new_cookies`, `test_the_new_refresh_token_keeps_working_across_many_rotations`; e2e "refresh token ROTATED" | |
| Logout and logout-all revocation | `test_logout_revokes_and_old_cookies_are_dead`, `test_logout_all_revokes_every_session_of_the_user`, `test_other_sessions_survive_a_single_logout`, `test_logout_is_idempotent_and_never_errors`; compose smoke test (logout 204 then `/me` 401) | Revoking a legacy Bearer token before its 24 h expiry is impossible by design |
| CSRF | `test_logout_requires_csrf_when_a_session_exists`, `test_logout_all_requires_csrf_for_cookie_auth`, `test_csrf.py`; e2e cross-site attacker page; compose smoke test (403 without the token) | |
| Origin validation | `test_cross_origin_state_changing_requests_are_rejected_even_with_valid_cookie_and_csrf`, `test_origin_check_applies_to_bearer_requests_too`, CORS preflight tests | |
| Session expiry | `test_idle_expiry_ends_the_session`, `test_absolute_expiry_ends_the_session_even_when_active`, `test_refresh_slides_idle_expiry_but_never_past_the_absolute_cap` | |
| Refresh-token replay | `test_reuse_detection_revokes_the_whole_session_immediately`, `test_replaying_a_rotated_token_inside_the_grace_window_is_409_and_does_not_revoke`; compose smoke test (replay after grace 401 then session dead) | |
| Concurrent refresh (multi-tab) | `test_concurrent_refresh_with_the_same_token_has_exactly_one_winner`; frontend single-flight refresh and BroadcastChannel logout unit tests | **Several real browser tabs racing**: not tested |
| Reload behaviour | e2e: reload restores the conversation; reload after access-cookie expiry stays signed in | |
| Expired-session cleanup | 9 tests in `test_sessions.py` (section 12) | A table with millions of rows |
