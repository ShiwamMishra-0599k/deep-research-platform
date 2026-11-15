# Demo script (about 3 minutes)

Two honest ways to show Lumen. Pick one and say which it is at the start.

| Mode | Command | What the audience sees | Say this |
|---|---|---|---|
| **Scripted** (works today, no keys, no accounts) | `backend/.venv/Scripts/python.exe scripts/demo.py` | The real application with a simulated model and a simulated internet: streaming progress, tables, maps, sources, sign-in, history | "The model and the web are simulated so this runs anywhere; what you are watching is the product's behaviour, not a model's research quality." |
| **Real small model** (after you approve installing Ollama) | the same, with the agent pointed at a local Ollama endpoint (`docs/ZERO_BUDGET_PLAN.md`) | Real answers from a small local model, slower and rougher than a hosted one | "This is a 4B local model on a laptop CPU; here is how it scored on the benchmark, with the confidence interval." (only once that has been run) |

Never present the scripted mode as a model result.

## Setup (once, before recording)

1. Docker Desktop running; `backend/.venv` and `research-agent/.venv` created (README, "Local development"); `npm install` in `frontend/`.
2. Run the demo command; wait for the banner with the URL. Stop it with Ctrl+C when done (it deletes its two throwaway containers).
3. Close other heavy applications: this machine has 8 GB and the demo starts five processes.

## Walkthrough

| Time | Show | Say (all of it verifiable in the repository) |
|---|---|---|
| 0:00 | The README top: the CI badge and the "what is and is not proven" table | "Everything here is tested and documented; the table says exactly what is not proven yet: no real-model quality score, no deployment." |
| 0:20 | Register an account in the app | "Sessions are HttpOnly cookies with rotating refresh tokens and CSRF protection; the script-readable cookie is only the CSRF token." |
| 0:40 | Ask "Bitcoin price in INR". Point at the live progress lines | "Each tool call streams to the browser over server-sent events. The agent runs in a separate service the browser can never reach." |
| 1:10 | The answer: the metric cards and a follow-up chip; then ask "research the RBI repo rate" to show a table with cited sources | "The answer is typed blocks, not a blob of text, so the UI can render a table or a map. Unknown block types fall back to a safe view." |
| 1:30 | Ask "EV charging stations in Lucknow" to show the map block | "Same pipeline, different tool: Nominatim and Overpass in the real system, simulated here (the scripted model picks this tool when it sees "EV" or "charging")." |
| 1:50 | Reload the page; the conversation is still there. Log out and in | "Persisted in PostgreSQL; the answer is saved after the commit, and the app says so if saving fails." |
| 2:10 | Open `docs/CASE_STUDY.md` section 3 | "Load testing found three defects: a database pool deadlock (18 of 50 requests succeeding), a circuit breaker that turned a 0.9 second Redis stall into 4.7 seconds of refusals, and a shutdown that lost answers. Each has a regression test that fails without the fix." |
| 2:40 | `docs/PERFORMANCE.md` heading "What was not measured" | "I list what I did not measure: no real model latency, one worker per service, no production." |
| 2:55 | Close | "Next step is running the benchmark against a small local model and reporting the result with its interval." |

## Questions to expect, and the honest answers

* *How accurate are the answers?* Not measured yet. The benchmark and grading code exist; no model has been scored. (Section 6 of the case study.)
* *Is it deployed?* No. It runs locally with no budget; the AWS design is a document.
* *Does it scale?* One backend plus one agent on a 4-core laptop handled 150 simultaneous researches with a fixed-latency stand-in model; the limit found was one CPU core each. That locates the code's ceiling; it does not predict production.
* *Why is the answer text so uniform in the demo?* The model is scripted.

## Recording tips

Use a 1280 by 720 window, hide bookmarks and personal tabs, record the terminal banner first so the "scripted" disclosure is on screen, and keep the account name and e-mail generic (the demo database is deleted on exit).
