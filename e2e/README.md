# e2e — full-stack integration harness

Exercises the whole application through a real browser:

```
Browser (headless Edge/Chrome) → Vite → FastAPI → PostgreSQL → research agent (LangGraph / Deep Agents)
                                                         → 8 real tools → Redis → (fake) third-party sites
```

**Real:** frontend, backend, PostgreSQL, Redis, the research agent and all eight tools, SSE, persistence, rendering.
**Fake:** the LLM (`fake_llm.py`, an OpenAI-compatible server) and every third-party website/API
(`run_agent_fake_internet.py`). No paid calls, no secrets. The agent process is sealed at the socket layer — any non-loopback
connection or DNS lookup is refused and logged, and the suites fail if anything was blocked.

> Honest scope: the *Python services* make no external calls. The *browser* still loads Leaflet CSS, web fonts and OpenStreetMap
> tiles from public CDNs. This is an integration test with a fake LLM and fake sites, not a test of live providers.

## Run

Prerequisites: Docker Desktop with the isolated test containers from `AGENTS.md` (postgres on `127.0.0.1:55432`, redis on
`127.0.0.1:56379`), `backend/.venv` and `research-agent/.venv`, `npm install` in `frontend/`, Microsoft Edge (or set `EDGE_PATH`
to Chrome).

```bash
python e2e/run.py            # 42-check user journey (register → research → blocks → reload → delete → logout → …)
python e2e/failure_tests.py  # 39 checks with real outages
```

Ports used: fake LLM `18999`, agent `18004`, backend `18080` (+ `18081`, `18082` for the failure suite), rogue agent `18090`,
frontend `15173`. Logs and screenshots go to `e2e/out/` (git-ignored). The agent takes ~40 s to import on a cold start.

## Safety

- `run.py` drops and recreates only the database `lumen_e2e_test` inside the throwaway test container.
- `failure_tests.py` stops and starts only `lumen-test-pg` and `lumen-test-redis`, and starts them again when it finishes.
- Nothing here references the project's compose volumes.

## Files

| File | Role |
|---|---|
| `run.py` | starts the stack, runs `scenario.mjs`, tears down, asserts the hermetic seal |
| `scenario.mjs` | browser user journey with assertions |
| `failure_tests.py` | outage and recovery tests, concurrency, rogue-agent protocol abuse |
| `fake_llm.py` | deterministic OpenAI-compatible server (streaming + tool calls) |
| `run_agent_fake_internet.py` | launches the real agent with a fake internet and a socket seal |
| `cdp.mjs` | dependency-free Chrome DevTools driver |
