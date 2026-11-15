# Performance and resilience: what was measured, and what was not

Everything here was measured on **one developer machine** (Windows 11, 4 CPUs, 8 GB RAM, Docker Desktop with a 3.9 GB VM), with a **fake LLM that answers
after a fixed delay** and a sealed fake internet. Real model latency, real provider cost, real network conditions and production hardware were **not**
involved. These numbers show the overhead and the limits of Lumen's own code. They are **not** a production capacity claim and must not be quoted as one.

Reproduce: `research-agent/.venv/Scripts/python.exe perf/load_test.py --levels 1,5,10,25,50,100,150,250 --rounds 1 --llm-delay-ms 1500 --api-load --fault agent-kill`
(needs the test containers from `AGENTS.md`; results are written to `perf/results/`, which is git-ignored). Graceful shutdown: `perf/sigterm_test.py`.

## 1. Measured: a defect the load test found, and the fix

The first run at 50 and 100 concurrent research requests failed: **18 of 50** and **20 of 100** succeeded; the rest were 500/503 after a 30 second wait.
The backend log showed `QueuePool limit of size 10 overflow 10 reached, connection timed out`. Four separate mechanisms were involved, found one at a time:

| # | Cause | Evidence | Fix |
|---|---|---|---|
| 1 | The authentication dependency kept one pooled connection for the whole request, and the research handler then opened a second. | A regression test showed 1 connection checked out when the handler started | Authentication uses its own short session |
| 2 | Sync generator dependencies closed their session on the same 40-thread pool that runs requests. Every thread could be blocked waiting for a connection while the requests holding connections waited for a thread to release them: a hold-and-wait deadlock that ended only at the 30 s pool timeout | Mutation test: the test fails when the close uses the default pool | Sessions close on a dedicated limiter |
| 3 | Response serialisation also needs a worker thread while the connection is still held (the 20 stuck sessions were the `refresh` after `INSERT` in "create conversation") | Sessions sampled in `pg_stat_activity` at 100 users | An **admission gate of 16 slots**, below the pool of 20, so a request never waits for a connection while holding anything; overload is shed with a fast `503` and `Retry-After` |
| 4 | A pool timeout surfaced as a generic 500 | Log | Mapped to 503 with `Retry-After` |

All four have regression tests (`test_research_sse.py`, `test_db_session_release.py`; the full backend suite passes, 364 tests). A first fix for cause 1 alone moved 50 users from 18 to 50
successes but left 100 users at 60, which is why the diagnosis continued.

## 2. Measured: concurrent research requests (final code, one backend worker and one agent worker)

One run on the committed code (`perf/results/load-20261115-165945.json`). Each user makes one research request: the fake LLM is called twice (1.5 s each) plus one fake web search,
so the floor is about 3.3 s. `CPU` is the share of the **whole 4-core machine** used by that process tree (about 25 percent = one core fully busy).

| Concurrent users | Succeeded | Total time p50 / p95 | Time to first event p95 | Throughput | CPU peak agent / backend | Memory peak agent / backend | PostgreSQL connections peak |
|---|---|---|---|---|---|---|---|
| 1 | 1 / 1 | 3.4 s | 0.2 s | 0.3 /s | 0.5 % / 0.5 % | 85 / 52 MB | 3 |
| 5 | 5 / 5 | 3.7 / 3.8 s | 0.3 s | 1.2 /s | 2.4 % / 7.5 % | 87 / 59 MB | 5 |
| 10 | 10 / 10 | 4.4 / 4.7 s | 1.0 s | 2.0 /s | 6.1 % / 9.2 % | 90 / 63 MB | 10 |
| 25 | 25 / 25 | 7.3 / 7.6 s | 1.6 s | 3.1 /s | 11.8 % / 19.9 % | 169 / 80 MB | 10 |
| 50 | 50 / 50 | 8.7 / 10.2 s | 3.7 s | 4.4 /s | 17.7 % / 20.4 % | 177 / 103 MB | 10 |
| 100 | 100 / 100 | 17.9 / 19.1 s | 8.8 s | 4.6 /s | 18.3 % / 21.1 % | 188 / 175 MB | 16 |
| 150 | 150 / 150 | 25.3 / 28.8 s | 15.3 s | 4.7 /s | 19.4 % / 18.2 % | 196 / 226 MB | 16 |
| 250 | **243 / 250** | 37.8 / 45.6 s | 22.3 s | 4.6 /s | 19.6 % / 21.2 % | 213 / 253 MB | 16 |

**What it shows**
* Up to 150 simultaneous researches all succeeded. Beyond about 50 users the work queues: latency grows roughly in proportion to users while throughput stays flat (about 4.6 per second in this run). The ceiling in this run is one core for each of the backend and the agent (both near 25 percent of a 4-core machine). This points to CPU, not the database (16 connections at most) or Redis (about 1,000 operations per second, 0 evictions).
* Time to first event grows with load (8.8 s at 100 users), which means that when the service is saturated users wait before seeing any progress. The 503 shedding only triggers when waiting for a database slot exceeds 10 s.
* Backend memory grew by roughly 0.8 MB per open stream (52 MB at 1 stream, 253 MB at 250), so memory sizing for long-lived streams is a real input to task sizing.
* **At 250 simultaneous users, 7 requests failed with a client-side `ReadError`.** The backend log has no matching error; none failed at 150 or below. Cause not established (it may be the load generator's single process and the Windows loopback, or the server resetting connections). One such failure was also seen in each of two earlier 250-user runs (248/250, 249/250).
* **Run-to-run spread is large on this machine** (it was short of memory: Windows memory compression was holding about 3.6 GB). An earlier run of the same code reached about 6.6 researches per second at the plateau and 100 users at p50 13.8 s, against 4.6 per second and 17.9 s here; the cheap-endpoint rate was 88 per second then and 51 per second here. Treat the ratios, not the absolute values, as meaningful.
* **Redis timeouts under load:** three times the rate-limit store timed out (the 0.25 s limit) and the backend fell back to per-process limits for 5 s, as designed for `RATE_LIMIT_FAIL_MODE=local`. **In production the mode is fail-closed**, so a slow Redis at that load would have caused refusals instead. The timeout value and the Redis sizing (isolated `noeviction` ElastiCache) need to be tested under load before relying on fail-closed.

**Before the fix (same harness, committed code of that time):** 25 users 25/25; **50 users 18/50; 100 users 20/100** (see section 1).


## 3. Measured: other behaviour

* **Cheap authenticated endpoints** (`/api/auth/me` and `/api/conversations`, 50 concurrent clients, 10 s): about **82 to 101 requests per second**, p95 0.8 to 0.9 s, 0 errors, on both the code before and after the pool fix (A/B on the same machine state; run-to-run spread is about 20 percent). An earlier single reading of 140 per second was a favourable moment, not the baseline.
* **Agent killed mid-run** (10 researches in flight): all 10 ended with a clean error event and a saved generic message, none hung; a new query while the agent was down failed in 2.2 s; after the agent restarted (7.6 s to healthy) the next query succeeded 11.7 s after the restart, with **no backend restart**.
* **Graceful shutdown (real backend image, Linux container, SIGTERM via `docker stop`; `perf/sigterm_test.py`).** Three measured cases, 8 streams in flight each time (each about 10.5 s):
  1. *Streams finish inside the grace period* (default `SHUTDOWN_GRACE_SECONDS=25`): all 8 completed, `docker stop` returned after 9.0 s, exit code 0, a new connection during shutdown was refused immediately.
  2. *Before the change*, streams outliving the stop timeout (forced with 4 s): the container was killed (exit 137), **all 8 streams were cut and no answer or note was saved**.
  3. *After the change* (grace 4 s, stop timeout 25 s, same long streams): uvicorn cancelled the 8 overdue streams after the grace period, **each saved an "interrupted, please ask again" note (8 of 8)**, and the container **exited 0 after 6.2 s** instead of being killed.
  Getting case 3 right took three attempts, each found by measurement: the notes alone left exit code 137, because idle anyio worker threads (non-daemon) kept the interpreter alive (confirmed with a `faulthandler` thread dump); stopping them at the end of the lifespan was not enough, because uvicorn cancels overdue streams but does not wait for them to unwind, so they created new workers afterwards; the lifespan shutdown now waits (up to 8 s) for in-flight streams before stopping the workers. New research requests during the drain get an immediate `503` with `Retry-After`.
  **Consequence for deployment:** keep `SHUTDOWN_GRACE_SECONDS` below the orchestrator's stop timeout (Fargate allows at most 120 s). A research can still last up to 300 s, so a deploy during a long research ends it, but the user now sees a clear message and the conversation stays consistent. Not yet tested for the agent image.
* **ARM64:** the backend image builds for `linux/arm64` (under emulation, about 8 minutes) and its native dependencies (psycopg binary, pydantic-core, bcrypt) load as `aarch64`. The agent image is covered in section 5.

## 4. What was not measured

* Real LLM latency, token counts and cost per research (the agent does not record token usage yet; see `docs/EVALUATION_PLAN.md`).
* More than one backend or agent process: all figures are **one worker each**. The CPU data below says one backend core is the ceiling, so throughput should scale with processes, but this was not tested.
* Memory growth over time (soak), very long conversations, large result payloads, slow clients.
* Real network latency, a database that is not on the same machine, TLS, a reverse proxy or load balancer in front.
* PostgreSQL restart and Redis restart under load. Single failures were tested before in the failure suite (39 tests) and the multi-instance suite (17), not under this load.
* The gate size (16) and pool (10 + 10) are reasoned from the local pool size, not tuned; the right values depend on the database's `max_connections` and the number of backend tasks.
* Cost: nothing here prices any infrastructure. See `docs/DEPLOYMENT_PROPOSAL.md` (an estimate, not a measurement).

## 5. ARM64 agent image

The **backend** image builds for `linux/arm64` (emulated, about 8 minutes) and the native dependencies load as `aarch64`; it was not run against a database under emulation.

The **agent** image (LangGraph, PyMuPDF and other heavier dependencies) was **not verified on ARM**: the emulated build was still running after more than 20 minutes when I stopped it because
the machine had under 100 MB of free memory. This is **unverified, not failed.** It needs either a machine with more memory, a native arm64 builder (for example a CI runner or an AWS Graviton build host), or GitHub Actions with
an arm64 runner. Until it is verified, the deployment proposal's ARM Fargate option (USD 102.60 per month) rests on an assumption, and the x86 images are what has actually been built and run in this project, so x86 (USD 120.75 per month) is the option backed by evidence.

