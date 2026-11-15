# Lumen — Tasks

Legend: ✅ done and verified · 🟡 partly done · ⬜ open

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

## Milestone: release-readiness baseline (this change set)

- ✅ Redis-backed distributed rate limiting (login, register, research/paid LLM, per-user, global concurrency), atomic Lua, fail-local / fail-closed, trusted-proxy-aware client IP — verified with real Redis, 3 real processes and the real compose stack
- ✅ Database credential hardening: no default, weak passwords refused, compose requires `POSTGRES_PASSWORD`, `scripts/init_env.py`, pinned compose project name (fixes a real volume collision)
- ✅ Rotation tooling (`scripts/rotate_db_password.py`) proven by a 19-check rehearsal; runbook `docs/DB_CREDENTIALS.md`
- 🟡 CI: workflow + Dependabot + `scripts/ci_local.py` written and validated; **not yet executed on GitHub Actions**
- ✅ Frontend testing strategy → `docs/FRONTEND_TESTING_PLAN.md` (**awaiting approval; nothing installed**)
- ✅ HttpOnly-cookie migration plan → `docs/COOKIE_AUTH_MIGRATION_PLAN.md` (**awaiting approval; nothing implemented**)
- ✅ 230 backend, 379 agent tests; 42-check browser journey; 39-check outage suite; 17-check multi-instance suite; 19-check rotation rehearsal

## Waiting on you (decisions, in order of leverage)

1. ⬜ **Push to GitHub (or approve it) so the CI workflow runs once** — until then CI is validated but unproven. Fix whatever the first real run finds.
2. ⬜ **Approve the frontend test dependencies** (7 dev deps, Node ≥ 22 in CI) and whether CI should gate on coverage — `docs/FRONTEND_TESTING_PLAN.md` §10.
3. ⬜ **Approve the cookie-session migration** and its design decisions (same-origin vs two origins, session lifetime, migration window) — `docs/COOKIE_AUTH_MIGRATION_PLAN.md` §14. Recommended after #2 lands.
4. ⬜ **Decide where production secrets live** (secret manager / platform env) and which Postgres credentials the real deployment uses; then run the rotation procedure against that volume (it is verified, not yet applied to any real data).
5. ⬜ **Decide the legacy `deep-research-platform_*` volumes' fate** with whoever owns the `alvoff` stack — they belong to that stack, not to this repo.

## Next engineering milestone (recommended order)

1. ⬜ Execute CI on GitHub; fix findings; add branch protection requiring it
2. ⬜ Frontend test framework (after approval) → then the cookie-session migration (after approval)
3. ⬜ Account lifecycle: email verification, password reset, account/data deletion, retention policy
4. ⬜ Per-user quotas (conversation count, storage) and per-user LLM budget
5. ⬜ Deployment: TLS proxy with `TRUSTED_PROXY_CIDRS` set, strict CSP/headers for the static frontend, secret manager, image scanning + digest pinning
6. ⬜ Observability: structured logs with request ids, rate-limit hit/fallback metrics, alerting on `degraded-local`; scrub URLs in access logs
7. ⬜ Separate Redis (or `noeviction`) for rate limits if eviction risk matters; Redis auth/TLS if it ever leaves the private network
8. ⬜ Replace `python-jose` (PYSEC-2026-4298 has no fixed release); react-router v7 upgrade (after frontend tests exist)
9. ⬜ Live-provider contract tests (scheduled, tiny, real keys); pin the agent's unpinned dependency ranges (it has no lockfile)
10. 🟡 Conversation rename UI (API and store exist) — product decision

## Known issues / notes

- With Redis down, the default fail-local ceiling is per replica (documented; fail-closed is available).
- The research agent has no dependency lockfile (ranges in `pyproject.toml`), so CI installs can drift.
- The integration harness is Windows/Edge-oriented by default (`EDGE_PATH` overrides it).
- `docker compose up` on a machine where another project uses 8080/5433 needs a port override (another stack, `alvoff-*`, is present on the development machine and also owns the legacy `deep-research-platform_*` volumes).
