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

## Next milestone (recommended order)

1. ⬜ **Shared rate limiting** (Redis-backed) so limits hold across replicas — prerequisite for any scale-out
2. ⬜ **HttpOnly-cookie sessions + CSRF + refresh/revocation** as one migration, with frontend tests (see `SECURITY_REVIEW.md` §2)
3. ⬜ **Frontend test framework** (e.g. Vitest + Testing Library; needs approval) covering auth store, chat store, SSE client, block renderers; run in CI
4. ⬜ **CI pipeline** running every gate in `TEST_PLAN.md` §3 on each change (needs Postgres + Redis service containers; e2e job optional)
5. ⬜ Account lifecycle: email verification, password reset, account/data deletion, retention policy
6. ⬜ Per-user quotas (conversation count, storage) and per-user LLM budget
7. ⬜ Deployment: TLS proxy, strict CSP/headers for the static frontend, non-default DB credentials from a secret store, image scanning + digest pinning
8. ⬜ Observability: structured logs with request ids, error-rate and latency metrics, alerting; scrub URLs in access logs
9. ⬜ Replace `python-jose` with a maintained JWT library (PYSEC-2026-4298 has no fixed release)
10. ⬜ react-router v7 upgrade (clears the remaining production advisory) once frontend tests exist
11. ⬜ Live-provider contract tests (scheduled, tiny, with real keys) to catch upstream API drift — the mocked suites cannot
12. 🟡 Conversation rename UI (API and store exist; the UI auto-titles only) — product decision

## Known issues / notes

- Rate limits and the concurrency guard are in-process (documented).
- The integration harness is Windows/Edge-oriented by default (`EDGE_PATH` overrides it).
- `docker compose up` on a machine where another project uses 8080/8004/5433/6379 needs a port override (other stacks were present while testing).
