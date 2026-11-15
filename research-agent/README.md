# Lumen Research Agent (HTTP/SSE)

FastAPI service that runs a Deep Agents ReAct loop with 8 custom tools and streams checkpoints + structured research blocks back to the caller via Server-Sent Events.

## Access (internal service)

This service is **not for browsers or the public internet**. Only the Lumen backend calls it, over the private network.
Every endpoint except `GET /health` requires the header `X-Internal-Token: <AGENT_API_KEY>`; with no `AGENT_API_KEY`
configured it answers 503 (fail-closed). There is no CORS middleware and no `/docs`. Limits: body ≤ 512 KB (413), query 1–4000
characters, history ≤ 40 entries with roles `user`/`assistant` (422). Error events carry fixed user-safe text; details stay in the logs.

## Endpoint

`POST /research` — `Content-Type: application/json`, `X-Internal-Token: …`

```json
{
  "query": "EV charging stations in Lucknow",
  "conversation_history": [
    {"role": "user", "content": "..."},
    {"role": "assistant", "content": "..."}
  ]
}
```

Returns `text/event-stream` with these events:

- `checkpoint` — `{status, content, tool?}` per tool call
- `blocks` — final `{status, data: ResearchResponse}`
- `clarification` — `{status, content}` when the agent needs more info
- `error` — `{status, content}`
- `done` — terminal event

## Tools

`web_search`, `read_webpage`, `wiki_search`, `nearby_places`, `latest_news`, `get_stock_price`, `get_forex_rate`, `get_crypto_price`. All cached in Redis.

## Run

Use the parent project's `docker-compose.yml`. Or locally:

```bash
pip install -e .
cp .env.example .env  # fill OPENROUTER_API_KEY + EXA_API_KEY
AGENT_API_KEY=<same value as the backend> uvicorn research_agent.server:create_app --factory --port 8004
```
