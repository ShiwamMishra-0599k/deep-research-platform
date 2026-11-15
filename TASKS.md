# Lumen — Tasks

> **Direction change 2026-11-15: INR 0 budget, portfolio goal (AI/ML/Forward Deployed Engineer).** The paid evaluation and the AWS deployment are cancelled. Read `docs/ZERO_BUDGET_PLAN.md` for the revised milestones; the table below is kept for history, and rows 3 to 6 are re-scoped there.

Legend: ✅ done and verified · 🟡 partly done · ⬜ open

## Master checklist (owner-approved sequence)

| # | Milestone | Status |
|---|---|---|
| 1 | Release readiness: rate limiting, DB credentials, CI, frontend tests, cookie sessions | ✅ built and tested locally |
| 2 | **A. Publish to GitHub** (`release-readiness/ci-validation`), Actions green, repo review | ✅ pushed (HEAD 7e5a9ff + this note); GitHub Actions run 43697467299 passed all 6 jobs (ruff; security scans; frontend 253 tests; agent 379 tests; backend 353 tests; docker). Branch push trigger added to `ci.yml`. Not merged to `main`: requires explicit authorization. INTERVIEW_PREP.md and AI_LEARNING_ROADMAP.md (personal, not about Lumen) removed on this branch; still on `main` until merged |
| 3 | AI research quality and evaluation | 🟡 **prepared, not executed.** Built: 29-case answer-quality set (`eval/quality_set.json`), grading and aggregation with confidence intervals (`eval/quality_grading.py`), budget model (`eval/cost_model.py`), provider comparison, methodology and proposed cap of INR 3,500 (`docs/EVALUATION_PLAN.md`). **No real-provider result exists.** Needs from you: approval of the cap, your own provider accounts/keys (pre-paid, per-key limit), and a human spot-check of the reference facts. Still to build: token/cost logging in the agent |
| 4 | Performance and cost optimization | 🟡 **local measurements done; no cost or real-provider figures.** `perf/load_test.py` (concurrency, SSE, CPU/memory, Postgres, Redis, agent-kill recovery) and `perf/sigterm_test.py` (real image, Linux container). Found and fixed a connection-pool deadlock that failed 32 of 50 and 80 of 100 concurrent researches; all 150 succeed after. Graceful shutdown verified for the backend image; losing in-flight answers when draining outlasts the stop timeout is documented. Backend arm64 image verified; **agent arm64 not verified**. See `docs/PERFORMANCE.md`. Still open: multi-worker scaling, soak, token/cost instrumentation, Redis timeout under load, agent SIGTERM |
| 5 | **B. Production deployment**: **no AWS resource, charge or public deployment without explicit approval of architecture and cost** | 🟡 proposal and reproducible cost model written (`docs/DEPLOYMENT_PROPOSAL.md`, `deploy/aws/cost_estimate.py`): recommended option about 102.60 USD (9,670 INR) per month plus provider usage. **Awaiting your approval and AWS access**; Terraform and deploy scripts are the next prepared (not applied) step |
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
- ✅ HttpOnly-cookie sessions, stages 1-3 (`AUTH_MODE=dual`): 15 min access, rotating refresh (7 days max, 24 h idle), reuse detection, server-side revocation, CSRF token + Origin check, same-origin topology. 382 backend tests; browser e2e 61/61
- ✅ `python-jose` replaced by PyJWT 2.15.1; react-router 7.18.5; `pip-audit` (backend) has no ignores; `npm audit --omit=dev` = 0
- ✅ Production configuration fails closed (`ENVIRONMENT=production`): Redis required and `RATE_LIMIT_FAIL_MODE=closed`, HTTPS CORS, Secure cookies, HSTS, strong secrets
- ✅ AWS Secrets Manager + Redis isolation **plan and example templates only** (`docs/AWS_SECRETS_PLAN.md`, `deploy/`); no AWS resource exists
- ✅ CI: executed on GitHub Actions, run 43697467299 green (all 6 jobs) on 2026-11-15
- ✅ Suites last run individually on 2026-11-15: backend 382, frontend 253, agent 391, e2e 61 (earlier run), failure 39, multi-instance 17, rotation 19 (earlier runs); staged `scripts/ci_local.py` replay green; GitHub Actions green (run 43697467299 and later)

## Waiting on you (decisions, in order of leverage)

1. ⬜ **Grant the token the `workflow` scope** (`gh auth refresh -h github.com -s workflow`) so the branch `release-readiness/ci-validation` can be pushed and CI run once.
2. ⬜ **Cookie stage 4:** when to end the migration window and switch production to `AUTH_MODE=cookie` (legacy Bearer tokens then stop working).
3. ⬜ **Production infrastructure:** create the Secrets Manager secrets, IAM task roles and the separate `noeviction` Redis from the plans; decide the real Postgres credentials, then run the rotation procedure on that volume.
4. ⬜ **Decide the legacy `deep-research-platform_*` volumes' fate** with whoever owns the `alvoff` stack; they are not this repo's.

## Next engineering milestone (recommended order)

1. ⬜ Execute CI on GitHub; fix findings; add branch protection requiring it
2. ✅ Expired-session cleanup scheduled and tested (`app/cleanup.py`, 9 tests, advisory-lock leak found and fixed on the way); stage-4 runbook written (`docs/COOKIE_AUTH_MIGRATION_PLAN.md` section 13), mode not switched; needs a Bearer-usage counter first
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
