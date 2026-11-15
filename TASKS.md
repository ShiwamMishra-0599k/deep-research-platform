# Lumen — Tasks

Legend: ✅ done and verified · 🟡 partly done · ⬜ open

## Master checklist (owner-approved sequence)

| # | Milestone | Status |
|---|---|---|
| 1 | Release readiness: rate limiting, DB credentials, CI, frontend tests, cookie sessions | ✅ built and tested locally |
| 2 | **A. Publish to GitHub** (`release-readiness/ci-validation`), Actions green, repo review | 🟡 blocked: the active `gh` account (ShiwamMishra-0599k) lacks the `workflow` token scope; README/docs prepared. No merge to `main` without explicit authorization |
| 3 | AI research quality and evaluation (golden questions, citation checks, real-provider runs) | ⬜ |
| 4 | Performance and cost optimization (measured, honest benchmarks) | ⬜ |
| 5 | **B. Production deployment**: design, IaC and cost estimate prepared first; **no AWS resource, charge or public deployment without explicit approval of architecture and cost** | ⬜ design not yet presented |
| 6 | **C. Portfolio launch**: case study, diagrams, real screenshots, demo script, benchmarks | ⬜ (after deployment validation) |

## Milestone: backend, agent and integration security (this change set)

- ✅ Frontend redesign verified and checkpointed (`11ad263`); design frozen
- ✅ Docs: PRD, ARCHITECTURE (root + agent), AGENTS, CLAUDE, DESIGN, TASKS, SECURITY_REVIEW, TEST_PLAN, README, e2e/README
- ✅ Backend hardening: required strong secrets, header-only JWT with iss/aud/jti, rate limits + lockout, security headers, size limits, CORS, safe errors, SSE correctness (disconnect, timeout, persistence of failures)
- ✅ Agent hardening: internal-token auth (fail-closed), no CORS, SSRF guard, untrusted-content handling, request limits, Redis resilience
- ✅ All eight tools audited and fixed (see `SECURITY_REVIEW.md`)
- ✅ Alembic migrations (baseline + 0002), legacy-database adoption, advisory lock
- ✅ Compose: agent/Redis unpublished, loopback binds, required secrets, healthchecks, non-root images, bounded Redis
- ✅ 144 backend + 379 agent tests; 42-check browser journey; 39-check real-outage suite
- ✅ Dependency fixes: `python-jose` 3.5.0, `npm audit fix`; SRI on Leaflet CSS
- ✅ Frontend functional fixes: logout clears state, stale-conversation after delete, 401/429/5xx handling, `persist_error`

## Milestone: release-readiness baseline

- ✅ Redis-backed distributed rate limiting (login, register, research/paid LLM, per-user, global concurrency), atomic Lua, fail-local / fail-closed, trusted-proxy-aware client IP, verified with real Redis, 3 real processes and the real compose stack
- ✅ Database credential hardening: no default, weak passwords refused, compose requires `POSTGRES_PASSWORD`, `scripts/init_env.py`, pinned compose project name (fixes a real volume collision with the `alvoff` stack)
- ✅ Rotation tooling (`scripts/rotate_db_password.py`) proven by a 19-check rehearsal on a synthetic volume; runbook `docs/DB_CREDENTIALS.md`. **No real credential was rotated**
- ✅ Hash-locked dependency lockfiles for the research agent (`requirements.lock`, `requirements-dev.lock`)

## Milestone: CI validation, frontend tests, cookie sessions

- ✅ Frontend test stack (Vitest, jsdom, Testing Library): 253 tests, coverage 99.7 / 94.73 / 100 / 99.7, thresholds 70/60/70/70 enforced in CI (Node >= 22)
- ✅ HttpOnly-cookie sessions, stages 1-3 (`AUTH_MODE=dual`): 15 min access, rotating refresh (7 days max, 24 h idle), reuse detection, server-side revocation, CSRF token + Origin check, same-origin topology. 353 backend tests; browser e2e 61/61
- ✅ `python-jose` replaced by PyJWT 2.15.1; react-router 7.18.5; `pip-audit` (backend) has no ignores; `npm audit --omit=dev` = 0
- ✅ Production configuration fails closed (`ENVIRONMENT=production`): Redis required and `RATE_LIMIT_FAIL_MODE=closed`, HTTPS CORS, Secure cookies, HSTS, strong secrets
- ✅ AWS Secrets Manager + Redis isolation **plan and example templates only** (`docs/AWS_SECRETS_PLAN.md`, `deploy/`); no AWS resource exists
- 🟡 CI: workflow validated (actionlint, schema, clean Linux containers) but **never executed on GitHub Actions**: the push is blocked by a missing `workflow` token scope
- ✅ Suites last run individually: backend 353, frontend 253, e2e 61, failure 39, multi-instance 17, rotation 19. The research agent's 379 tests and the combined `scripts/ci_local.py` replay were **not re-run after the final changes** (the replay was stopped for low memory)

## Waiting on you (decisions, in order of leverage)

1. ⬜ **Grant the token the `workflow` scope** (`gh auth refresh -h github.com -s workflow`) so the branch `release-readiness/ci-validation` can be pushed and CI run once.
2. ⬜ **Cookie stage 4:** when to end the migration window and switch production to `AUTH_MODE=cookie` (legacy Bearer tokens then stop working).
3. ⬜ **Production infrastructure:** create the Secrets Manager secrets, IAM task roles and the separate `noeviction` Redis from the plans; decide the real Postgres credentials, then run the rotation procedure on that volume.
4. ⬜ **Decide the legacy `deep-research-platform_*` volumes' fate** with whoever owns the `alvoff` stack; they are not this repo's.

## Next engineering milestone (recommended order)

1. ⬜ Execute CI on GitHub; fix findings; add branch protection requiring it
2. ⬜ Schedule `purge_expired_sessions` (it exists but nothing calls it, so expired `auth_sessions` rows accumulate)
3. ⬜ Account lifecycle: email verification, password reset, account/data deletion, retention policy
4. ⬜ Per-user quotas (conversation count, storage) and per-user LLM budget
5. ⬜ Deployment: TLS proxy with `TRUSTED_PROXY_CIDRS` set, strict CSP/headers for the static frontend, image scanning + digest pinning
6. ⬜ Observability: structured logs with request ids, rate-limit and session-reuse metrics, alerting on `degraded-local`; scrub URLs in access logs
7. ⬜ Live-provider contract tests (scheduled, tiny, real keys)
8. ⬜ Dev-tooling advisories (vite/esbuild/vitest): upgrade Vite and Vitest together
9. 🟡 Conversation rename UI (API and store exist), a product decision

## Known issues / notes

- With Redis down, development falls back to a per-replica ceiling; production refuses instead (enforced at startup).
- The integration harness is Windows/Edge-oriented by default (`EDGE_PATH` overrides it).
- `docker compose up` on a machine where another project uses 8080/5433 needs a port override (another stack, `alvoff-*`, is present on the development machine and also owns the legacy `deep-research-platform_*` volumes).
