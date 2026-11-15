# Architecture diagrams

GitHub renders the Mermaid blocks below. Each diagram describes behaviour that exists in the code; where something is measured or tested, the document that holds the evidence is linked.
Text versions of the same material: [`../ARCHITECTURE.md`](../ARCHITECTURE.md), [`../research-agent/ARCHITECTURE.md`](../research-agent/ARCHITECTURE.md).

## 1. Components and trust boundaries

```mermaid
flowchart LR
    subgraph Untrusted["Untrusted"]
        B["Browser<br/>React + TypeScript SPA<br/>HttpOnly cookie session"]
        W["Public web pages<br/>and third-party APIs"]
    end
    subgraph Trusted["Docker network (trusted)"]
        API["Backend<br/>FastAPI<br/>auth, limits, SSE bridge"]
        AG["Research agent<br/>FastAPI + LangGraph<br/>8 tools, typed blocks<br/>(not published)"]
        PG[("PostgreSQL 16<br/>users, sessions,<br/>conversations, messages")]
        RL[("Redis: rate limits<br/>db 1, HMAC keys, TTLs")]
        RC[("Redis: tool cache<br/>db 0, public data only")]
    end
    LLM["LLM endpoint<br/>OpenAI-compatible<br/>(OpenRouter, OpenAI or a local Ollama)"]

    B -- "HTTPS, same origin<br/>cookies + CSRF header" --> API
    API -- "SSE stream" --> B
    API -- "SQL" --> PG
    API -- "Lua scripts" --> RL
    API -- "X-Internal-Token<br/>SSE stream" --> AG
    AG -- "tool results" --> RC
    AG -- "chat completions" --> LLM
    AG -- "SSRF-guarded fetches" --> W
```

Rules at each boundary are listed in [`../ARCHITECTURE.md`](../ARCHITECTURE.md) section 1. The agent has no published port and rejects any request without the shared secret.

## 2. One research request

```mermaid
sequenceDiagram
    autonumber
    participant U as Browser
    participant A as Backend
    participant D as PostgreSQL
    participant R as Redis
    participant G as Agent
    participant L as LLM and tools

    U->>A: POST /conversations/{id}/query (cookie, X-CSRF-Token)
    A->>A: check session, CSRF, Origin
    A->>R: rate limit and concurrency slot (atomic Lua)
    A->>D: ownership check, save question, load history
    A-->>U: event user_message
    A->>G: POST /research (X-Internal-Token)
    loop agent loop
        G->>L: model call, then tool calls
        G-->>A: checkpoint (which tool, what it found)
        A-->>U: event checkpoint
    end
    G-->>A: blocks (typed answer, sources, follow-ups) and done (usage report)
    A->>A: validate shape and size, replace any agent error text
    A-->>U: event blocks
    A->>D: save answer (retried)
    A-->>U: event persisted
    A->>R: release concurrency slot
```

If the browser disconnects, the upstream agent request is cancelled; an answer that had already completed is still saved. Event payloads: [`API.md`](API.md).

## 3. Cookie session lifecycle

```mermaid
sequenceDiagram
    autonumber
    participant B as Browser
    participant A as Backend
    participant D as auth_sessions table

    B->>A: login
    A->>D: create session (hash of refresh token only)
    A-->>B: access cookie 15 min, refresh cookie (Strict, /api/auth), CSRF cookie
    Note over B,A: access cookie expires after 15 minutes
    B->>A: request returns 401
    B->>A: refresh (single-flight, CSRF header)
    A->>D: compare-and-swap: new refresh hash replaces the old one
    A-->>B: rotated cookies, request is replayed once
    Note over B,A: a stolen, already-used refresh token
    B->>A: refresh with the OLD token
    alt within 10 s of the rotation (another tab)
        A-->>B: 409, session kept
    else later (replay)
        A->>D: revoke the whole session
        A-->>B: 401
    end
```

Idle expiry 24 h, absolute expiry 7 days, logout and logout-all revoke server-side. Ended sessions are purged after 7 days by `app/cleanup.py`. Tests: [`COOKIE_AUTH_MIGRATION_PLAN.md`](COOKIE_AUTH_MIGRATION_PLAN.md) section 14.

## 4. Why database admission is gated (found by load testing)

```mermaid
flowchart TD
    R["Incoming request"] --> G{"Free database slot?<br/>16 slots, below the pool of 20"}
    G -- "yes" --> S["Take a short session,<br/>run the query,<br/>release the connection"]
    G -- "no, wait up to 10 s<br/>(holds no thread, no connection)" --> G
    G -- "still none after 10 s" --> X["503 + Retry-After<br/>(fast, clean)"]
    S --> OK["Respond"]
```

Before this, 50 concurrent researches failed (18 of 50 succeeded) because requests held one pooled connection while waiting for another, and every worker thread was blocked waiting on the pool: a hold-and-wait
deadlock that only the 30 s pool timeout ended. The measurements and the fix: [`PERFORMANCE.md`](PERFORMANCE.md) section 1.

## 5. Redis circuit breaker (half-open)

```mermaid
stateDiagram-v2
    [*] --> Closed
    Closed --> Open: two failed attempts in one call
    Open --> Open: caller refused (fail-closed) or served by local limits (fail-local)
    Open --> Probing: every 0.25 s, ONE call is let through
    Probing --> Closed: probe succeeds
    Probing --> Open: probe fails
```

Before the probe, a 0.9 s Redis stall left a single user refused for 4.7 s after Redis was back. After it, none. [`PERFORMANCE.md`](PERFORMANCE.md) section 3a.

## 6. Shutdown (SIGTERM)

```mermaid
sequenceDiagram
    autonumber
    participant O as Orchestrator
    participant S as Backend process
    participant U as Open research streams

    O->>S: SIGTERM
    S->>S: stop accepting connections, flag "shutting down"
    Note over S: new research requests get 503 + Retry-After
    S->>U: wait up to SHUTDOWN_GRACE_SECONDS (default 25)
    alt streams finish in time
        U-->>S: complete and saved
    else overdue
        S->>U: cancel
        U->>U: save an "interrupted, please ask again" note
    end
    S->>S: wait for unwinding streams, stop worker threads
    S-->>O: exit 0
```

Without the waiting and the worker stop, the process hung until SIGKILL (exit 137) and every cut answer was lost. Measured results: [`PERFORMANCE.md`](PERFORMANCE.md) section 3.

## 7. What kind of evidence supports which claim

```mermaid
flowchart LR
    A["A. Deterministic software tests<br/>scripted LLM, sealed fake internet<br/>proves the software is correct"]
    B["B. Component evaluations without an LLM<br/>real code on frozen data<br/>proves a component works"]
    C["C. Model-quality evaluation<br/>a real model answers the benchmark<br/>measures answer quality"]
    A -. "never reported as" .-> C
    B -. "never reported as" .-> C
```

Status of each tier: [`ZERO_BUDGET_PLAN.md`](ZERO_BUDGET_PLAN.md). Tier C has **not** been run.
