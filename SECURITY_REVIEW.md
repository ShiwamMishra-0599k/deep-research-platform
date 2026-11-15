# Lumen — Security Review

Scope: backend, research agent, database/migrations, Redis, Docker configuration, and the frontend where it touches
authentication, state and integration. Method: manual code review, regression tests per finding, live and mocked
integration tests, static analysis (bandit), dependency audits (pip-audit, npm audit), and a mutation check on the agent's
security controls. Status values are literal: **Fixed** = fixed and covered by a test that failed before; **Mitigated** = risk
reduced, not removed; **Accepted** = knowingly left; **Open** = needs work.

## 1. Findings

### Backend / API

| ID | Severity | Finding | Status | Evidence |
|---|---|---|---|---|
| B1 | High | Hardcoded default `JWT_SECRET` accepted silently; compose supplied it too | **Fixed** — no default; ≥32 chars and no placeholder words or the app refuses to start; compose requires the variable | `test_weak_or_placeholder_jwt_secret_refuses_to_start`, `docker compose config` fails without secrets |
| B2 | High | `?token=` accepted on every route (token in URLs, access logs, Referer) | **Fixed** — header-only bearer | `test_token_in_query_string_is_not_accepted`; verified against the running container |
| B3 | High | No rate limit on login/register (brute force) or on the paid LLM endpoint | **Fixed** — per-IP login/register limits, per-email lockout, per-user query rate and concurrent-stream caps | `test_auth.py` (throttle, lockout, reset), `test_research_sse.py` (rate, concurrency, slot release) |
| B4 | Medium | A finished answer could be lost on disconnect (disconnect checked before handling the event); upstream agent request never explicitly closed; an error mid-stream left the question with no reply | **Fixed** — state is updated before delivery; `aclose()` in `finally`; failures are persisted; completed answers saved after disconnect | `test_disconnect_*`, `test_unexpected_exception_in_agent_stream_is_sanitised` (this one caught a bug in my first fix) |
| B5 | Medium | Raw exception text and the agent's response body were forwarded to the browser | **Fixed** — fixed user-facing messages only; details logged server-side | `test_agent_error_event_is_replaced…`, `test_non_2xx_is_agent_unavailable_without_leaking_body`, live rogue-agent tests |
| B6 | Medium | User lookup by email; unknown email skipped password hashing (timing enumeration); concurrent registration race → 500 | **Fixed** — lookup by immutable user id; dummy verify; unique-index race → 409 | `test_login_wrong_password_and_unknown_user_look_identical`, `test_concurrent_registration…` |
| B7 | Medium | No security headers, no body limit, CORS allowed all methods/headers, `/docs` exposed | **Fixed** | `test_http_security.py` (15 tests) |
| B8 | Medium | JWT carried PII (email, name), no `iss`/`aud`/`jti`; claims not all required | **Fixed** | `test_newly_issued_token_has_expected_claims…`, 10 forged-token cases incl. `alg:none`, wrong aud/iss, HS512, tampered payload |
| B9 | Low | bcrypt silently truncates >72 bytes; rename title unbounded; blank rename silently ignored | **Fixed** | validation tests |
| B10 | Low | Sync DB calls inside `async def` blocked the event loop; no DB connect/statement timeouts | **Fixed** — threadpool + 5 s connect / 15 s statement timeouts | `test_failures.py`, live PostgreSQL outage test |
| B11 | Low | Validation errors could echo submitted values (including passwords) | **Fixed** | `test_validation_errors_do_not_echo_the_password` |

### Research agent (verified by the parent; mutation-checked)

| ID | Severity | Finding | Status |
|---|---|---|---|
| A1 | **Critical** | `/research`, `/health/ready`, `/metrics` unauthenticated; wildcard CORS; port published to the host | **Fixed** — `X-Internal-Token` (constant-time, fail-closed 503), no CORS, no docs, port no longer published. Verified inside the compose network: 401 without/with a bad token, 200 with the right one |
| A2 | **Critical** | SSRF in `read_webpage`: direct `httpx.get(url)` whenever "pdf" appeared in the URL; model-controlled URL | **Fixed** — `security/url_guard.py`: scheme/port allow-list, no userinfo, DNS resolved by us and the connection pinned to the validated IP (Host/SNI preserved), every redirect hop re-validated, blocks private/loopback/link-local/CGNAT/metadata incl. decimal/hex/octal and IPv4-mapped forms, 5 MB streamed cap, deadline. The parent independently probed 36 hostile URLs and a redirect-to-metadata: all refused |
| A3 | High | Overpass QL injection via `place_type`; unbounded `radius_meters` | **Fixed** — allow-list/regex; radius clamped 100–20 000 m |
| A4 | High | Error results cached for hours ("Search failed", "Could not fetch…") | **Fixed** — failures/empties never cached; non-string cache values treated as misses |
| A5 | High | Raw exception text sent over SSE | **Fixed** — three fixed messages |
| A6 | Medium | Page text could plant arbitrary "Sources" links | **Fixed** — sources only from search-result `Source:` lines, the URL actually opened, and Wikipedia links |
| A7 | Medium | Pipe-delimited rows (`PLACE\|`, `NEWS\|`, …) forgeable from content | **Fixed** — every field sanitised (visible in the live run: `Tata\|Power` → `Tata/Power`) |
| A8 | Medium | No request limits on the agent | **Fixed** — 512 KB body (413), query 1–4000, history ≤40, roles `user`/`assistant` |
| A9 | Medium | Agent stream not closed on disconnect; no overall deadline | **Fixed** — `finally: aclose()`, 240 s deadline |
| A10 | Medium | Redis down at startup crashed the service; no Redis timeouts | **Fixed** — never raises, short timeouts, lazy reconnect (live test: stop/start Redis container) |
| A11 | Medium | Prompt injection: tool output treated as instructions | **Mitigated** — system prompt marks tool output untrusted; web/page/wiki output wrapped in `<untrusted_content>` with delimiter spoofing neutralised; structured rows sanitised. A model can still be influenced by hostile text; this is reduction, not elimination |

### Tool correctness bugs fixed (data integrity)

`wiki_search` imported an undeclared package (fallback was dead) and blocked the loop; `nearby_places` used a flat ×111 km/degree
distance (wrong east–west) and did not sort; `get_crypto_price` raised on a null 24 h change; `get_stock_price` showed a missing
currency as ₹, and bare `TCS` resolves to a US company (now reported with exchange/currency/name); `get_forex_rate` left arithmetic
to the model (now computed with `Decimal` in code, labelled as an ECB reference rate); DuckDuckGo/yfinance blocked the event loop;
`latest_news` matched everything for an all-stopword topic. Live response shapes of Frankfurter, CoinGecko and Wikipedia were
checked once against the real services.

### Frontend (functional/security fixes only; design frozen)

| ID | Severity | Finding | Status |
|---|---|---|---|
| F1 | High (shared browser) | Logout did not clear chat state: the next person signing in on the same tab inherited the previous user's `activeId`, conversations and cached messages | **Fixed** — logout resets the store. Found by reading the code; the scenario test passes after the fix (it passed vacuously before because an earlier step failed, so I do not claim a before/after proof) |
| F2 | Medium | After deleting the active conversation the app stayed pointed at the deleted id; the next question hit 404 | **Fixed** — reproduced by the integration scenario (failed), fixed, passes |
| F3 | Low | Expired token mid-stream, 429, 5xx all showed "Connection lost" | **Fixed** — 401 returns to sign-in; specific messages for 429/404/413/422/5xx |
| F4 | Low | Leaflet CSS loaded from unpkg without Subresource Integrity | **Fixed** — SRI hash added (verified equal to the published value) |
| F5 | High (deps) | axios / follow-redirects / form-data advisories (production dependencies) | **Fixed** — `npm audit fix` within semver ranges |
| F6 | Moderate (deps) | react-router open redirect via `<Link>`/`useNavigate` with attacker-controlled paths | **Accepted** — the app only navigates to fixed routes and server-generated UUIDs; the fix is a breaking major upgrade of a frozen frontend |
| F7 | Dev-only | Vite/esbuild/babel/browserslist advisories (dev server/build tooling, none ship to users) | **Accepted** — run the dev server only on trusted networks |
| F8 | Medium | JWT stored in `localStorage` (readable by any XSS) | **Mitigated / decision below** |

### Infrastructure

| ID | Finding | Status |
|---|---|---|
| I1 | Compose published the agent (8004) and Redis (6379, no auth) on all interfaces | **Fixed** — not published; verified `docker port` is empty. Backend and Postgres bound to 127.0.0.1 |
| I2 | `create_all` on startup, no migrations, no composite indexes | **Fixed** — Alembic (baseline + 0002); advisory-locked; adopts old databases without touching data; verified on a legacy-schema fixture and a restarted container |
| I3 | Containers ran as root | **Fixed** — UID 10001 for backend and agent (verified) |
| I4 | No healthchecks | **Fixed** — backend readiness (DB), agent liveness; backend waits for a healthy agent |
| I5 | Postgres uses development credentials (`lumen`/`lumen`) | **Accepted for local dev** — deliberately not changed here because that would lock the backend out of the existing data volume. Rotate before any shared deployment |
| I6 | Redis unbounded memory | **Fixed** — 256 MB `allkeys-lru`, no persistence |

### Test-harness integrity (found during this work)

The fake internet was first built by patching `httpx`. The follow-up generator's OpenAI SDK bypassed it and reached the **real**
`api.openai.com` (with a fake key; only synthetic text was sent). The harness had reported "no unfaked network access". It is now
sealed at the socket layer (non-loopback connect and DNS refused and logged) and the suites assert that nothing escaped. Scope of
that guarantee: the Python services. The browser in the integration run still loads Leaflet CSS, fonts and map tiles from public CDNs.

## 2. Decision: HttpOnly-cookie sessions — evaluated, deferred

The case for: an XSS bug could steal a `localStorage` token. The case against doing it now:
1. It is a cross-cutting migration (auth store, axios, SSE client, CORS with credentials, CSRF tokens on every state-changing route), and the brief forbids a half-implemented authentication migration.
2. The frontend is frozen and has no unit-test framework to guard a rewrite of its auth layer.
3. Current XSS exposure is low: React escapes by default; Markdown is rendered without raw HTML; link URLs are sanitised by `react-markdown`; no `dangerouslySetInnerHTML`; hostile markdown/links were exercised end to end.
4. Token theft is bounded: 24 h expiry, no PII inside, header-only (never in URLs).

Recommended path (see `TASKS.md`): `Set-Cookie: HttpOnly; Secure; SameSite=Lax` access cookie + double-submit CSRF token header,
CORS with credentials on an explicit origin, short access / rotating refresh tokens, server-side revocation, all in one milestone with
frontend tests. Until then, serve the frontend with a strict CSP (`default-src 'self'`; allow only the Leaflet/fonts/tile hosts).

## 3. Residual risks and accepted limits

- **Rate limits are per process.** Several backend replicas multiply the limits. Use a shared store (Redis) before scaling out.
- **No token revocation** before the 24 h expiry; no refresh tokens; no email verification or password reset; no account/data deletion endpoint.
- **No per-user quotas** on conversation count or stored message size (a per-message blocks cap exists).
- **Prompt injection is mitigated, not solved.** An attacker-controlled search result can still appear as a Sources chip (the URL is real search-result provenance) and can still sway wording.
- **DNS-rebinding defence** is implemented by IP pinning and tested with simulated resolvers; it has not been tried against live adversarial DNS.
- **uvicorn access logs record request URLs.** The server no longer accepts `?token=`, but if a client sends one the URL is still logged.
- **Search/LLM providers see user queries** (Exa/Tavily/DuckDuckGo/OpenRouter/OpenAI). No PII filtering is applied. Review provider terms before real users.
- **TLS** is not terminated by compose; deploy behind a TLS proxy and set `ENABLE_HSTS=true`.
- **Container images** were built and run non-root but not scanned (no Trivy/Grype run); base images are not digest-pinned.
- **`python-jose` advisory PYSEC-2026-4298** (no fixed release): not exploitable here — it needs a public key used as an HMAC secret, and verification pins `algorithms=["HS256"]` with a random symmetric secret. Prefer migrating to a maintained JWT library in a dedicated change.
- The agent's default bind address (`0.0.0.0`) is intentional inside the container (bandit B104); the port is not published.

## 4. Not reviewed / not tested

Real LLM and paid search providers; HTTPS/TLS termination; multi-replica behaviour; load and soak; Firefox/Safari/real mobile
devices; accessibility with a screen reader (contrast, focus order and semantics were checked programmatically); the browser's
behaviour on a hostile network; penetration testing by a third party.

## 5. Re-running the evidence

See `TEST_PLAN.md`. Scanners: `uvx bandit -r backend/app research-agent/src`, `uvx pip-audit -r backend/requirements.txt`,
`cd frontend && npm audit --omit=dev`.
