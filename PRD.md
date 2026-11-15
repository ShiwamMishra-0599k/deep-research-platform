# Lumen — Product Requirements

## 1. Summary
Lumen is a web research assistant. A signed-in user asks a question in natural language; an AI agent searches live sources,
shows its progress, and returns a cited, structured answer (prose, tables, maps, metric cards). Conversations are saved per user.
It is built first for busy professionals in India (INR, NSE/BSE, Indian news, local search) but handles general research.

## 2. Problem
Answering a practical question ("EV chargers near me", "RBI repo rate decision", "Bitcoin in INR") means opening several sites,
reconciling sources and re-checking freshness. Chatbots answer quickly but without verifiable sources or live data.

## 3. Goals
1. Answers grounded in **live data and visible sources**, never invented numbers.
2. **Transparent process**: the user sees each step the agent takes while it works.
3. **Structured output** that is faster to scan than prose (tables, cards, maps).
4. **Safe by default**: user data is private to the user; the system resists abuse, misuse of the agent, and cost abuse.
5. **Resilient**: any dependency failure produces a clear message, never a hang or a broken page.

## 4. Non-goals (current)
Social/team features, file uploads, voice, payments, mobile apps, multi-language UI chrome (answers follow the user's language),
dark mode (design is frozen light-only), real-time brokerage/trading data, routing/navigation, scanned-PDF OCR, social-media research.

## 5. Users and primary flows
| Persona | Need |
|---|---|
| Professional on the go | Quick, sourced answers: prices, news, places, regulatory facts |
| Analyst | Repeatable lookups with citations they can open and verify |

Flows: register → sign in → start a conversation → ask → watch progress → read answer (blocks + sources) → follow up → revisit,
rename, delete conversations later → sign out.

## 6. Functional requirements and acceptance criteria

| # | Requirement | Acceptance |
|---|---|---|
| F1 | Account registration and sign-in | Email unique (case-insensitive); password 8–72 bytes; invalid credentials give one generic message; repeated failures are throttled |
| F2 | Conversations (create, list newest-first, rename, delete) | Only the owner can see or change them; others get 404; deleting removes its messages |
| F3 | Ask a question and stream live progress | One checkpoint per tool call within the same request; final result is one of: blocks, clarification, error |
| F4 | Eight research tools: web search, page reader, Wikipedia, nearby places, latest news, stock, forex, crypto | Each validates input, times out, falls back where defined, and returns structured data or a clear failure |
| F5 | Structured answers | Markdown always; table for places/news; map for places; metric cards for prices; unknown block types degrade to a JSON viewer |
| F6 | Citations | Sources listed per answer, limited to URLs the agent actually retrieved |
| F7 | Follow-up suggestions | 2–3 suggestions when an OpenAI key is configured; absent otherwise |
| F8 | Clarifying question | Agent asks at most one short question when a critical detail is missing |
| F9 | Persistence and history | Questions and answers (or the error that occurred) survive reload; a completed answer is saved even if the browser disconnects |
| F10 | Failure handling | PostgreSQL/agent/LLM/search/Redis failures produce a specific, safe message; never a stack trace or secret |
| F11 | Abuse controls | Per-user query rate and concurrent-stream limits; request size limits |

## 7. Non-functional requirements
- **Security:** see `SECURITY_REVIEW.md`. Header-only JWT, strong required secrets, SSRF-safe fetching, agent not publicly reachable, no internal errors to clients.
- **Privacy:** conversations are private to their owner; cache holds only public data; logs contain no tokens, passwords or secrets.
- **Reliability:** fast failure (DB connect 5 s, agent connect 5 s, research capped at 300 s); migrations are reproducible and non-destructive.
- **Accessibility:** WCAG 2.2 AA text contrast, keyboard operable, reduced-motion respected (`DESIGN.md`).
- **Cost control:** no unbounded LLM use per user (rate limits, concurrency cap, tool-call cap).
- **Observability:** liveness (`/health`) and readiness (`/health/ready`) endpoints; agent Prometheus metrics (authenticated).

## 8. Success measures (to be instrumented — none are collected today)
Answer completion rate, error-event rate by cause, median time to first checkpoint and to answer, citation click-through,
return usage. No targets are asserted until baseline data exists.

## 9. Open questions
1. Hosted multi-user deployment: shared rate-limit store, HttpOnly-cookie sessions, email verification, password reset?
2. Quotas per user (conversation count, storage)?
3. Which LLM/provider budget applies per user or per plan?
4. Data retention and account/data deletion policy.
