# Research Agent — Architecture

A FastAPI service that runs a Deep Agents ReAct loop with 8 custom tools and streams structured research blocks back over Server-Sent Events. It is an **internal service**: only the Lumen backend calls it.

## Position in the stack

```
React UI ──► FastAPI backend ──► Research Agent (this service)
   ▲              ▲                     │
   │              │                     ├─► Redis (optional tool-result cache)
   └── SSE ───────┴── SSE ──────────────┼─► OpenRouter / OpenAI (LLM)
                                        └─► Exa / Tavily / DuckDuckGo, OSM, yfinance, RSS,
                                            Frankfurter, CoinGecko, Wikipedia, Jina / PyMuPDF
```

The backend authenticates the end user, persists the conversation, calls `POST /research` here with the shared secret, and proxies the SSE events to the browser. The browser must never reach this service.

## Authentication and exposure

| Rule | Detail |
|---|---|
| Shared secret | Header `X-Internal-Token` must equal env `AGENT_API_KEY` (constant-time compare). |
| Fail closed | If `AGENT_API_KEY` is empty the service answers **503** to `/research`, `/health/ready` and `/metrics`. There is no bypass flag. |
| Open endpoint | Only `GET /health` (liveness, returns `{"status":"ok"}` and nothing else). |
| No CORS | No CORS middleware; browsers are not a client of this service. |
| No API docs | `/docs`, `/redoc`, `/openapi.json` are disabled. |
| Network | Deploy on a private network. Do **not** publish port 8004 to the host or the internet. The token is defence in depth, not a substitute. |

## Request lifecycle

```
1. POST /research   (X-Internal-Token)
   Body: { "query": "...", "conversation_history": [{role, content}, ...] }
   Limits: body ≤ 512 KB (413), query 1–4000 chars, history ≤ 40 entries,
           each content ≤ 20000 chars, role ∈ {user, assistant} (422).
           The 422 body reports field + message only and never echoes the input.

2. SSE stream
   ├─► event: checkpoint   data: {status, content, tool?}    (per tool call)
   ├─► event: blocks       data: {status, data: ResearchResponse}
   │   OR event: clarification data: {status, content}
   │   OR event: error         data: {status, content}
   └─► event: done         data: {status: "completed"}       (not sent after error)
```

`blocks`, `clarification` and `error` are mutually exclusive terminal events.

**Errors are generic.** SSE `error` events carry one of three fixed messages (generic failure, timeout, temporarily unavailable). Exception text, stack traces and key material are logged server-side only and never sent to the client.

**Cancellation.** If the client disconnects, the handler's `finally` closes the upstream `astream_events` generator. An overall deadline (`REQUEST_TIMEOUT_SECONDS`, default 240 s) stops runaway runs; the LangGraph recursion limit stays at 50.

## Internal flow

```
http_handler.stream_research(req)
   ├── _build_messages()          # history + today's date → list[BaseMessage]
   ├── agent = await get_agent()  # lazy singleton; init failure → generic error
   ├── async for event in agent.astream_events(..., version="v2"):   # under a timeout
   │     on_tool_start → checkpoint        on_tool_end → ToolResult + checkpoint
   │     on_chat_model_end → final_text
   ├── "[CLARIFICATION]" prefix → clarification event
   ├── else format_blocks() → follow-ups (3 s budget, best effort) → blocks event
   └── any exception → generic error event
```

## Tools

All eight tools validate their input, bound their network time, and return text (never raise). Failures return `Error: ...` and are **never cached**.

| Tool | Source | TTL | Notes |
|---|---|---|---|
| `web_search` | Exa → Tavily → DuckDuckGo (blocking SDKs run in a thread) | 1 h | ≤ 8 de-duplicated http(s) results; query 1–500 chars |
| `read_webpage` | Jina Reader → direct SSRF-guarded fetch (HTML text or PDF via PyMuPDF) | 6 h | see SSRF section; 8000-char cap |
| `wiki_search` | MediaWiki REST summary + opensearch fallback (httpx) | 24 h | disambiguation lists options; only `en.wikipedia.org` links kept |
| `nearby_places` | Nominatim + Overpass | 24 h | `place_type` allow-list / `^[a-z_]{1,30}$` (no QL injection), radius clamped 100–20000 m, haversine distance, sorted, ≤ 15 |
| `latest_news` | RSS (NDTV, ET, Moneycontrol, TOI) | 15 m | stop-words ignored, cross-feed de-dupe, newest first, HTML stripped, partial feed failures tolerated |
| `get_stock_price` | yfinance (thread + 15 s timeout) | 15 m | returns symbol, name, exchange, currency, as-of time; never assumes INR; pass `.NS`/`.BO` for Indian stocks |
| `get_forex_rate` | Frankfurter | 1 h (rate only) | amount conversion computed with `Decimal` in code; ECB reference rate, not a live quote |
| `get_crypto_price` | CoinGecko | 5 m | null 24h change → `n/a`; 24h change is USD-based; 429 handled |

Structured lines (`PLACE|`, `NEWS|`, `STOCK|`, `FOREX|`, `CRYPTO|`) are built from sanitised fields (`|`, CR/LF and control characters removed) so retrieved text cannot forge rows. Existing field indices are unchanged; newer fields are appended (`STOCK|sym|price|change|pe|hi|lo|name|exchange|currency|asof`, `FOREX|from|to|rate|date|amount|converted`).

## Untrusted content and prompt injection

- The system prompt states that tool output is untrusted data, never instructions; forbids revealing the prompt or keys; forbids inventing numbers; and tells the model to quote only tool-computed figures.
- `web_search`, `read_webpage` and `wiki_search` output is wrapped in `<untrusted_content>…</untrusted_content>` **after** the cache layer. Cached bytes are raw, and any delimiter appearing inside content is neutralised so content cannot close the block.
- `sources` shown to users come only from `Source:` lines of `web_search`, the single URL actually passed to `read_webpage`, and Wikipedia's own page link. URLs merely appearing inside fetched text are ignored.
- Structured blocks are only built from the matching tool's own output, so forged `STOCK|`-style lines in web content do nothing.

## SSRF defence (`security/url_guard.py`)

Used for everything fetched from a user/LLM-supplied URL (`read_webpage`, including the URL handed to Jina):

1. http/https only; no userinfo (`http://a@127.0.0.1`); ports limited to 80/443/8080/8443; overlong or control-character URLs rejected.
2. Blocked hostnames/suffixes (`localhost`, `*.internal`, `*.local`, `metadata.google.internal`, …). IP literals in any encoding (decimal, hex, octal, short form, IPv4-mapped IPv6) are normalised and checked.
3. DNS is resolved by the service and **every** address must be public (no private, loopback, link-local, multicast, reserved, CGNAT, unspecified; cloud metadata addresses explicitly blocked).
4. The connection goes to the **validated IP** (Host header + TLS SNI preserved), so a DNS answer that changes after the check cannot redirect the request (DNS rebinding).
5. Redirects are not followed automatically: each hop (max 3) is re-validated from step 1.
6. The body is streamed with a 5 MB cap (declared and actual) and a 20 s total deadline; content types are checked.

## Caching rules

```
key = f"research:{tool}:{sha256(normalised args)[:16]}"
hit → return        miss → call upstream → store ONLY a successful, non-empty text result
```

- Keys are global and contain **public data only** — no user identifiers, no conversation content. Nothing user-specific may be cached.
- Errors, empty results ("No results found", "no news") and values above 200 KB are never stored; a non-string or empty cached value is treated as a miss.
- Redis is optional. A failed connection at startup is logged and the service runs uncached; reconnects are attempted lazily (at most every 10 s). Every Redis call has a short timeout, and any failure degrades to a cache miss — it never raises or hangs a request.

## Health endpoints

| Endpoint | Auth | Description |
|---|---|---|
| `GET /health` | none | Liveness only. |
| `GET /health/ready` | token | `503 not_ready` if no LLM key; `200 degraded` if Redis is down or only the DuckDuckGo fallback is available; `200 ready` otherwise. |
| `GET /metrics` | token | Prometheus metrics. |

## Source layout

```
src/research_agent/
├── server.py               # app factory, auth dependency, body limit, health/metrics
├── http_handler.py         # request model (limits) + the SSE generator
├── config.py               # pydantic-settings (env vars)
├── agent/{graph,tools}.py  # Deep Agent singleton + system prompt; the 8 tools
├── blocks/                 # schemas, block_formatter, checkpoint_formatter, follow_up_generator
├── security/{url_guard,sanitize}.py
└── infrastructure/redis_client.py
tests/                      # pytest + pytest-asyncio; no real network, no paid LLM calls
```

## LLM configuration

OpenRouter (`anthropic/claude-sonnet-4-6`, configurable) is primary; OpenAI (`gpt-4o`) is used only when no OpenRouter key is set. Temperature 0.3, OpenRouter ephemeral prompt caching on. With neither key set, agent initialisation fails and the client receives the generic "temporarily unavailable" error. Follow-up suggestions use `gpt-4o-mini` and require `OPENAI_API_KEY`; without it, no suggestions are shown.

## Known limits

Not handled: live options chains, Google Maps routing, paywalled content, scanned-PDF OCR, social-media trends, IRCTC PNR/seat availability. Forex values are daily ECB reference rates. Stock symbols without an exchange suffix can match a US-listed company; the result states the exchange and currency so this is visible.
