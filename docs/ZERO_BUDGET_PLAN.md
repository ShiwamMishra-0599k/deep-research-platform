# Zero-budget plan (supersedes the paid-evaluation budget)

**Rule: INR 0.** No API credits, no subscriptions, no payment details, no cloud resources. The proposed INR 3,500 evaluation budget in earlier documents is **cancelled**;
`eval/cost_model.py` is kept only as an optional what-if calculator and is not part of this plan. Anything that needs an account with a third party is marked **optional** and
needs your explicit go-ahead; nothing here requires a card.

The goal is a project you can defend in an interview. That means the evidence must be real, labelled for what it is, and reproducible by anyone on a laptop without keys.

## 1. Three kinds of evidence (never mixed in one table)

| Tier | What it proves | What it cannot prove | Needs | Status |
|---|---|---|---|---|
| **A. Deterministic software tests** (fake or scripted LLM, sealed fake internet) | The software is correct: protocol, auth, persistence, tool routing, failure handling, concurrency | **Anything about answer quality.** A scripted model "passes" by construction | Nothing | Done: 364 backend, 379 agent, 253 frontend, 61 browser, 39 failure, 17 multi-instance |
| **B. Component evaluations without an LLM** (real code, real or frozen data) | A specific component works on real inputs: retrieval ranking, citation-to-claim support checks, source tiering, unanswerable/conflict detection rules | The quality of any model's final answer | Nothing (pure Python, frozen local corpus) | Partly planned (section 4) |
| **C. Model-quality evaluation** (a real LLM answers the benchmark) | How well the whole system answers, with which model, at what latency | Anything about other models or production | A free model: local (Ollama) or a free-tier API | **Not run.** Needs a download you must approve (section 3) |

Reports must say which tier produced each number. A scripted-LLM result is never described as "model quality".

## 2. Free options, investigated

All figures below come from third-party pages read on 2026-11-15 and **must be re-checked before relying on them**.

| Option | Cost | Fit for Lumen | Limits and risks |
|---|---|---|---|
| **Ollama + a small local model** (for example a Qwen3 4B class model) | Free software and free weights; a download of roughly 1 GB for Ollama and 2 to 3 GB for the model (**needs your approval**) | The agent talks to an OpenAI-compatible endpoint, and Ollama exposes one, so only environment variables change. Reports say Qwen3 is the most dependable small model for tool calling | This machine: i3-1115G4, 4 cores, integrated graphics, 8 GB RAM often under 1 GB free. Expect slow CPU generation, long prompts costing minutes, and tool-call mistakes that a 4B model makes more than a frontier model. A full run is hours, not minutes. **The result will measure a small local model, and must be reported as exactly that** |
| OpenRouter `:free` models | No card; needs an account you create | Same endpoint style | Reported 20 requests per minute and about 50 per day for accounts that never bought credits. One research needs about 5 to 10 calls, so roughly 5 to 10 researches a day. Slow to collect 28 cases; quotas change often |
| Tavily free tier (optional search) | Reported 1,000 credits per month with no card | Already supported by the agent (fallback tool) | Account required; quota small |
| DuckDuckGo search | No key | **Already in the agent's fallback chain** | Unofficial and rate-limited; results vary run to run |
| Wikipedia / Wikidata / public RSS | No key | **Already used by the agent** (wiki, news tools) | Public, polite rate limits; fine for factual questions |
| SearXNG (self-hosted) | Free software | Possible extra search backend | Needs another container and memory this machine lacks; not planned |
| Local embeddings (a small sentence-transformer) | Free; model download of about 100 MB (needs approval) | Would enable semantic retrieval | Lumen has no vector retrieval today. BM25 (pure Python, no download) is the baseline planned for tier B |
| Brave Search API | Reported to need a card now | n/a | Excluded by the no-payment-details rule |

**Recommendation:** tier A and B run today with no downloads. For tier C, use **Ollama with one small model on this machine for a pilot of 3 cases**, then decide from the measured speed
and reliability whether the full set is practical (probably as an overnight run). The OpenRouter free route is an optional second opinion if you create an account yourself.

## 3. Decisions that need you (none is taken without you)

1. **Approve installing Ollama and downloading one model (about 3 to 4 GB total, free).** Without this, tier C stays unmeasured. I would pin the model name and record its digest.
2. **Optional:** create a free OpenRouter and/or Tavily account yourself. I never see or handle payment; keys would stay in a local, git-ignored file. Skip it if you prefer.

## 4. The benchmark (29 questions) and its scoring

The set (`eval/quality_set.json`, version 2) has 29 cases: stable factual (14), multi-hop (2), false premise (3), unanswerable (3), conflicting evidence (4), time-sensitive (2, reference recorded on the day), multilingual (1). An earlier document said 19; the file actually held 20 before this expansion.
Scoring is the existing rubric (`docs/EVALUATION_PLAN.md` section 4): key-fact recall, forbidden claims, unsupported claims, citation resolves and supports, source tier,
premise handling, calibration, relevance, latency, tokens. Reference facts are author-written from settled public knowledge and **need your spot-check** before any number is published.
With 29 cases every proportion has a wide 95% interval (roughly +/- 13 to 17 points), so the report compares configurations only when intervals do not overlap and never ranks close results.

**Tier B, no-LLM component evaluations I can build and run now:**
* Citation verification: given a claim and a source text, decide support. A deterministic baseline (lexical and BM25 overlap) is scored on a hand-labelled set of supported, unsupported and contradicted pairs, giving real precision and recall for that component.
* Retrieval: BM25 over a small frozen corpus of public-domain text with known answers, scored with recall at k.
* Unanswerable and false-premise detection rules over scripted responses, to test the grader and the harness themselves, never a model.

## 5. Revised milestone plan

| # | Milestone | Cost | Can finish entirely locally? |
|---|---|---|---|
| 1 | Re-plan evaluation to zero budget (this document), expand benchmark to 29 | 0 | Yes |
| 2 | Observability: tokens, LLM calls, search calls and an estimated cost (price defaults to 0) per research, in logs, `/metrics` and the final event | 0 | Yes (against the scripted LLM; real counts appear when a real model runs) |
| 3 | Legacy Bearer usage counter (precondition for the cookie-only switch) | 0 | Yes |
| 4 | Graceful shutdown with a bounded drain and a clean "interrupted" outcome; agent SIGTERM test | 0 | Yes (Linux containers on this machine) |
| 5 | Investigate load-test `ReadError`s and Redis timeouts; fix or document | 0 | Yes |
| 6 | ARM64: a native build job in CI (public repositories get free arm64 runners; to be confirmed by the first run) | 0 | Needs GitHub, no account changes |
| 7 | Multi-worker scaling test and a soak test with memory-growth measurement | 0 | Yes (bounded by this machine) |
| 8 | Tier B component evaluations (citation checker, BM25) | 0 | Yes |
| 9 | Portfolio gap review and fixes: API reference, architecture diagram, demo script, "run it in 5 minutes without keys" path, honest limitations | 0 | Yes |
| 10 | Tier C pilot on a free local model | 0 | **Only after you approve the download** |
| 11 | Optional free-tier API run | 0 | Only if you create the account |
| - | AWS deployment | - | **Out of scope under this rule**; `docs/DEPLOYMENT_PROPOSAL.md` stays as a design document, clearly not deployed |

## 6. What will not be claimed

No production use, no users, no deployment, no real-model quality score until tier C has actually run, no cost savings figures, and no benchmark result for any model that was not run
on this machine. Local-model results will be labelled with the model, quantisation, hardware and date.
