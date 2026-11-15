# Case study: building and hardening Lumen

Lumen is an AI research assistant: a React SPA, a FastAPI backend (auth, persistence, streaming), a separate research-agent service (LangGraph with eight tools) and PostgreSQL and Redis.
It is a **portfolio project built and tested locally with no budget**. It has no users, has never been deployed, and has **not** been evaluated against a real language model (see section 6). This document
describes the engineering decisions and, more usefully, the defects that measurement found and how they were proven fixed. Every number below is from this repository's own tests and harnesses; each links to where it is recorded.

## 1. The shape of the system

Diagrams: [`DIAGRAMS.md`](DIAGRAMS.md). The browser talks only to the backend. The agent has no published port and trusts only a shared secret; everything it fetches from the internet is treated as untrusted data
(SSRF guard, size and time limits, prompt-injection tests). The answer is not text: the agent returns typed blocks (markdown, table, map, insight cards) that the UI renders, and an unknown block type degrades to a safe fallback.

## 2. Decisions and their trade-offs

| Decision | Alternative | What it costs | Evidence |
|---|---|---|---|
| **HttpOnly cookie sessions** with a 15-minute access token and a rotating refresh token, instead of a JWT in `localStorage` | Keep the JWT in the browser's script-readable storage | Needs CSRF protection and an Origin check; a same-origin deployment; a migration window with both modes (`AUTH_MODE=dual`) | 379 backend tests, 61 browser checks including a cross-site attacker page, a compose smoke test; [`COOKIE_AUTH_MIGRATION_PLAN.md`](COOKIE_AUTH_MIGRATION_PLAN.md) section 14 |
| **Refresh-token reuse detection with a 10 s grace window** | Revoke on any second use | A stolen token can be used inside the window, bounded to one rotation | Concurrent-refresh test with exactly one winner; replay after the window revokes the session |
| **Rate limits in Redis (atomic Lua), keys HMAC-hashed**, with a **fail-closed** production mode | Per-process limits | A Redis outage refuses guarded requests in production; limits across processes become exact | 17-check multi-process suite, 3 breaker tests |
| **Server-sent events** through a validating bridge | WebSockets, polling | One-directional; the bridge must validate every agent event and replace agent error text with fixed wording | SSE protocol tests; the agent can never place its own text in an error shown to the user |
| **Answer saved only after the commit; `persisted` sent last** | Send the answer first | The user waits a few milliseconds longer; a failed save is reported honestly (`persist_error`) | Persistence-failure tests |
| **Separate, isolated agent service** | One process | An extra network hop and a shared secret to manage | Agent is not reachable from the host in compose |

## 3. Defects that measurement found

These were not found by reading code. Each was found by a harness, diagnosed with evidence (including a wrong first theory), fixed, and pinned by a regression test that was shown to fail without the fix.

### 3.1 The database pool deadlocked under burst load

**Symptom.** A local load test (`perf/load_test.py`, a fixed-latency stand-in for the model, real everything else) passed at 25 concurrent researches and failed at 50: 18 of 50 succeeded; at 100, 20 of 100. The log showed
`QueuePool limit of size 10 overflow 10 reached, connection timed out, timeout 30.00`.

**First theory, wrong.** Streaming requests hold a database connection for their whole duration. Sampling `pg_stat_activity` during 15 long streams showed **zero** connections idle in a transaction. The theory was dropped.

**Actual causes (four, found one at a time).** Authentication kept one pooled connection for the whole request while the handler opened a second; synchronous dependency teardown closed sessions on the same 40-thread pool that runs requests,
so connection holders waited for threads while every thread waited for a connection; response serialisation needed a thread while the connection was still held; and a pool timeout surfaced as a bare 500.

**Fix.** Authentication uses its own short session; sessions close on a dedicated limiter; an admission gate of 16 slots (below the pool of 20) means a request never waits for a connection, and overload is shed as a fast `503` with `Retry-After`.

**Result (local, one worker each).** 100 of 100 and 150 of 150 succeeded; 243 to 250 of 250 across runs. The measured plateau of about 4.6 to 7 researches per second is one CPU core each for the backend and the agent. A first partial fix moved 50 users from 18 to 50
but left 100 users at 60, which is why the diagnosis continued. Details: [`PERFORMANCE.md`](PERFORMANCE.md) sections 1 and 2.

### 3.2 A sub-second Redis stall caused seconds of refused requests

**Symptom.** Redis timeouts appeared in one noisy load run. An independent probe showed Redis itself was fast (median 3 to 4 ms, maximum 69 ms at 250 concurrent researches), and the timeouts did not recur in four later runs, so the stall was environmental.

**The real defect** was the circuit breaker: after two failed attempts Redis was "down" for 5 s, and in production mode every guarded request is refused while it is. Freezing Redis for 0.7 s under a single user caused **191 of 217 requests to be refused,
with refusals lasting 4.7 s after Redis had recovered**. With 20 users the problem was hidden, because a call already in flight healed the breaker.

**Fix.** A half-open breaker: one probe call per 0.25 s. **Result:** 4 refusals during the stall and none after; 0 of 169 with 20 users. Fail-closed behaviour during a real outage is unchanged and tested.
The stall was easy to miss: it only shows with one user, which is the case a load test does not naturally cover. [`PERFORMANCE.md`](PERFORMANCE.md) section 3a.

### 3.3 Shutdown lost answers, then hung

**Symptom.** In the real backend image under `docker stop`, streams that outlived the stop timeout were cut and **no answer was saved** (exit 137).

**Fix, in three attempts.** (1) A bounded drain (`SHUTDOWN_GRACE_SECONDS`) and an "interrupted, please ask again" note: the notes were saved, but the process still ended by SIGKILL. A thread dump (`faulthandler` via SIGABRT) showed the main thread in `threading._shutdown`
waiting for four idle, non-daemon worker threads. (2) Stopping the workers at the end of the lifespan: still hung, because uvicorn cancels overdue streams but does not wait for them to unwind, so they created new workers after the cleanup.
(3) Waiting for in-flight streams before stopping the workers. **Result:** exit code 0 in 6.2 s; 8 of 8 interrupted streams left a note; the normal path (streams finish in time) is unchanged. [`PERFORMANCE.md`](PERFORMANCE.md) section 3.

## 4. Mistakes made and corrected

* A document claimed a 19-case benchmark; the file held 20. Found while expanding it to 29 and corrected everywhere.
* An early API throughput reading of 140 requests per second was a favourable moment; an A/B on the same machine state gave 85 to 101 for the original code and 82 to 90 for the new, within run-to-run spread.
* A test for the new Bearer-usage counter failed only in a group run: leftover in-memory counts from earlier tests. A fixture gap, not a product bug, and recorded as such.
* A single multi-instance run failed 2 of 17 checks while the Redis timeout fired and passed on the rerun; it is reported as a flaky first run, with the logs, not hidden.

## 5. Method

Measure before changing; keep the wrong theory in the notes; write the regression test first and watch it fail; mutate the fix to prove the test can fail; re-run the neighbouring suites; state exactly what the evidence does and does not show.
CI runs lint, security scans, backend, agent, frontend and Docker jobs on every push to the release branch.

## 6. What is not claimed

* **No real model has been evaluated.** The 29-question benchmark, the grading code and the plan exist ([`EVALUATION_PLAN.md`](EVALUATION_PLAN.md), [`ZERO_BUDGET_PLAN.md`](ZERO_BUDGET_PLAN.md)); no score exists. Results from the scripted model prove the software, never answer quality.
* **Not deployed**, no users, no production traffic, no cost data. The AWS design in [`DEPLOYMENT_PROPOSAL.md`](DEPLOYMENT_PROPOSAL.md) is a design document.
* Load figures are from one 4-core laptop with a fixed-latency fake model; they locate the limits of this code and do not predict production capacity.
* ARM64: see [`PERFORMANCE.md`](PERFORMANCE.md) section 5 for exactly what was and was not verified.

## 7. What would come next

Run the benchmark against a small local model and report the result with its confidence interval and the model's name; measure two backend workers; add backend latency metrics; decide the cookie-only switch from the Bearer-usage counter once real traffic exists.
