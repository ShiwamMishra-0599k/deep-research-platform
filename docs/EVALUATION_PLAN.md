# Research-quality evaluation plan (prepared; nothing has been run)

Status: **plan only.** No provider key has been used, no paid request has been made, and **no quality, token, latency-against-real-providers or cost
number exists yet.** Everything below that looks like a number is either a published list price reported by a third party and not yet checked
against the provider's own page, or an assumption that the pilot run will replace. Results from the fake LLM (`eval/run_hermetic.py`) test protocol and
tool routing only and say nothing about answer quality.

## 1. What is already built

| Piece | What it does | Verified how |
|---|---|---|
| `eval/golden.json` + `scoring.py` + `run_eval.py` | 16 cases; checks the stream protocol, terminal event, tool selection, block structure, citation presence, forbidden strings | 8 hermetic cases pass against the fake LLM; 14 scorer unit tests |
| `eval/quality_set.json` | **19 answer-quality cases** with atomic `key_facts`, `must_not_claim` statements and a `kind` (factual, multi-hop, false premise, unanswerable, conflicting evidence, time-sensitive, Hindi) | Structure checked by tests. The facts are author-written from long-settled public knowledge; **a human must spot-check each against a primary source before the first paid run** |
| `eval/quality_grading.py` | Builds a blank grade sheet from a run and aggregates graded sheets into the metrics below, with 95% Wilson intervals | 5 unit tests, including a hand-counted aggregate |
| `eval/cost_model.py` | Budget range from price and token assumptions, in INR and USD | Runs; the inputs are the unverified part |
| Spend gate | `--live` refuses without `EVAL_ALLOW_SPEND=1` and `--max-cases` | Refusal confirmed earlier |

Run a case file with `--cases quality_set.json`. Results now keep `answer_text` and `source_urls` for the grader (`eval/results/` is git-ignored).

## 2. Provider comparison

Prices below were reported by third-party sites on 2026-11-15. Several sources disagreed with each other, and OpenAI's own page did not load in
search. **Treat every price as unverified until read from the official page listed in `eval/cost_model.py`.**

### LLM route: OpenRouter or OpenAI

| | OpenRouter | OpenAI direct |
|---|---|---|
| What it is | One API and key for many vendors' models, including Anthropic and OpenAI | OpenAI models only |
| Cost beyond token prices | Reported 5.5% fee (minimum $0.80) on each credit purchase; token prices passed through. Bring-your-own-key terms were reported inconsistently | None beyond token prices |
| Spend control | Credits are prepaid; the balance is a hard ceiling. Per-key limits are offered (verify) | Project budgets and limits exist (verify exact behaviour); billing is usually postpaid or prepaid by plan |
| Code fit | **Default today** (`OPENROUTER_*`, `ChatOpenAI` pointed at the OpenRouter base URL); the agent already has an OpenAI fallback | Works through the same client |
| Model reported in the repo's example env | `anthropic/claude-sonnet-4-6`. **This name may be out of date**; reports in November 2026 describe a newer Sonnet generation at about $2 per million input and $10 per million output tokens | `gpt-4o` in the example env, also likely dated; reports on newer OpenAI models conflict (flagship figures from $5/$30 to $10/$50) |
| Main risk | Extra hop and fee; provider routing can change latency | Single vendor; no fallback if OpenAI degrades |

**Recommendation:** run the evaluation through **OpenRouter** on prepaid credit. It reuses the production code path, lets one key test an Anthropic-class and an
OpenAI-class model on identical prompts, and the prepaid balance is a real spend ceiling. Compare **two** models head to head on the same cases (one per
vendor), and pick the one with the better quality per rupee, not the cheapest. The OpenAI route stays configured as the production fallback.

### Search route: Exa or Tavily

| | Exa | Tavily |
|---|---|---|
| Reported price | About $7 per 1,000 searches with contents for up to 10 results; deep modes $12 to $15 per 1,000; extra results and summaries billed separately | Credit based: basic search 1 credit, advanced 2 (per Tavily's docs excerpt); reported $0.008 per credit pay-as-you-go, about $16 per 1,000 advanced searches |
| Free allowance | Reported to exist; size and terms unclear | Reported 1,000 credits per month, no card |
| Character | Neural/semantic retrieval, strong for finding specific documents; returns page contents in the same call | Search tuned for LLM agents, with an advanced depth mode and an answer-oriented output |
| Code fit | **Primary today** (`EXA_API_KEY`), Tavily fallback, then DuckDuckGo | Fallback today (`TAVILY_SEARCH_DEPTH=advanced`) |

**Recommendation:** do **not** choose from price lists. Run the same cases with Exa primary and with Tavily primary and compare the quality metrics (source credibility
tier, citation support, completeness). For the evaluation itself, Tavily's free allowance should cover a full run (about 190 advanced credits at the base
assumption against a reported 1,000 per month), so the search cost for that arm may be zero; confirm the allowance on Tavily's page first. Keep the
fallback chain in production either way, because search outages are a real failure mode (already tested with fake outages).

## 3. Methodology

1. **Freeze the inputs.** Fix the case file, the model identifiers, the search settings and the agent commit (record the git SHA in the run report).
   Never change prompts between the arms being compared.
2. **Spot-check the reference facts** in `quality_set.json` against primary sources, and record the day's values for the two `record_at_run_time` cases.
3. **Pilot first: 5 cases, one run.** Measure the real tokens and search calls per task (see section 6), compare with the assumptions, and **stop** if the real
   per-task cost is more than 2x the base assumption. Re-run `eval/cost_model.py` with measured values before the next step.
4. **Full set, one arm at a time, then a comparison.** Repeat each arm 3 times only if the budget allows; one run says nothing about run-to-run variance.
5. **Grade blind.** A grader should not know which arm produced an answer. Two graders on a subset give an agreement figure; with one grader, say so.
6. **Report with uncertainty.** With 19 cases every proportion has a wide interval (a score of 16/19 has a 95% interval of about 62% to 95%). Do not rank arms
   whose intervals overlap, and do not present a point estimate alone.
7. **Keep fake-LLM and real-provider results in separate tables, labelled.**

## 4. Scoring criteria (what each grader field means)

| Metric | Definition | How graded |
|---|---|---|
| **Factual accuracy / completeness** (`fact_recall`) | Share of the case's `key_facts` that the answer states correctly. Stating the opposite counts as a miss and as a forbidden claim | Human: one true/false per key fact |
| **Hallucination** | (a) answers asserting any `must_not_claim` statement; (b) `unsupported_claim_rate` = claims no cited source supports / all factual claims; (c) false-premise answered as if true; (d) unanswerable question answered with an invented figure | Human: count claims; (c) and (d) via `premise_handled` |
| **Citation correctness** | `citation_resolves` (the link opens) and `citation_supports_claim` (the page actually supports the claim it is attached to). A working link that does not support the claim is a failure | Human opens each link; this is the slowest part, about 5 to 10 per case |
| **Source credibility** | Tier 1 primary/official (regulator, standards body, the paper itself), tier 2 reputable secondary (established news, encyclopedia, major vendor docs), tier 3 weak or unknown (content farms, forums, unattributed). Only resolving links are tiered | Human, using the tier definitions; domain lists can pre-fill but must be reviewed |
| **Relevance** | 0 off-topic, 1 partly answers, 2 answers the question asked | Human |
| **Handling conflicting evidence** | Pass only if the answer presents both sides or states the evidence is mixed, with no false certainty (`calibrated`) | Human |
| **Reasoning and synthesis** | Multi-hop cases: the combined conclusion is right, not only each fact. Scored through `key_facts` that state the combined result (for example "Rajasthan is larger") | Human |
| **Error handling / graceful degradation** | Terminal events, no leaked errors or stack traces, sensible message when a tool fails. **Free:** use the hermetic fault-injection runs; real-provider failures are not provoked | `scoring.py` checks plus the existing failure suite (39 tests) |
| **Latency** | End-to-end seconds and time to first progress event; p50 and p95 | Automatic (`run_eval.py`) |
| **Tokens and cost per task** | Input/output tokens, searches, and rupees per task | **Not measured today** (section 6) |

An LLM judge can fill the sheet faster and cheaply (about INR 39 for 19 answers on the assumptions) but it inherits the model's blind spots on exactly the
hallucination and citation checks. If used, run it **in addition** to a human grading of at least a third of the cases and report the agreement.

## 5. Budget (proposal for your approval; nothing is spent until you approve)

Computed by `python eval/cost_model.py` (INR 94.25 per USD; token and search counts are assumptions; list prices unverified):

| Step | Low | Base | High |
|---|---|---|---|
| Pilot: 5 cases, 1 run | INR 50 | INR 116 | INR 272 |
| Full set: 19 cases, 1 run, one configuration | INR 189 | INR 441 | INR 1,032 |
| Two configurations x 19 cases (comparison) | INR 377 | INR 881 | INR 2,064 |
| Repeating the full set 3 times, one configuration | INR 566 | INR 1,322 | INR 3,097 |

**Proposed cap: INR 3,500 (about USD 37) in total**, as prepaid OpenRouter credit with no automatic top-up. That covers the pilot, one full run per
configuration for two models, and the search arm comparison at the base assumption with headroom for the high assumption on part of it. It does
**not** cover repeated runs of everything at the high assumption, GST or card/forex charges, or an expensive flagship model. If the pilot shows a higher cost, I will stop and
propose a new number rather than continue.

## 6. Gaps to close before the numbers mean anything

* **Token and cost measurement does not exist.** The agent does not record token usage. Options: read the OpenRouter usage fields returned with each completion
  (needs a small agent change that logs counts only, never content), or compare the key's credit balance before and after each run (coarse, per run not per case). I will implement the first
  before the pilot; it is free to build and test against the fake LLM.
* **Reference facts need a human spot-check** (section 3, step 2).
* **Real-provider failure behaviour** (rate limits, timeouts, quota exhaustion) is only covered by simulated outages.
* The quality set has 19 cases. It can show gross problems and compare arms that differ a lot. It cannot rank similar systems.

## 7. Safe execution (when you approve)

1. You create the OpenRouter and search accounts, pre-pay at most the approved cap, and create a key **with a per-key limit if offered**. You do not paste keys into chat.
2. Keys go into the agent's local `.env` (git-ignored) or the shell environment. Never in a commit, a log or a result file.
3. Start the agent locally (not deployed): `EVAL_ALLOW_SPEND=1 python eval/run_eval.py --url http://127.0.0.1:<port> --live --cases quality_set.json --max-cases 5`.
4. Check the pilot's measured cost against section 5 before the full run; only then raise `--max-cases`.
5. After each run, revoke or rotate the key if you will not use it again, and check the provider dashboard against the harness's own estimate.
6. Grade with `python eval/quality_grading.py sheet --results <run>.json`, fill the sheet, then `report`.
