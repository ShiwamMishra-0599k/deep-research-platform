# Observability: what Lumen records about each research

Lumen counts what a research did. It does **not** record prompts, answers, URLs, tool outputs, user identifiers or secrets in metrics or in the usage log line.

## Per-research usage report

The research agent's final `done` event carries a `usage` object (agent service, SSE):

```json
{"status": "completed",
 "usage": {"llm_calls": 2, "tool_calls": 1, "token_source": "provider",
           "input_tokens": 2360, "output_tokens": 88, "estimated_cost_usd": 0.0}}
```

* `token_source` says where the token numbers came from. **`provider`**: every successful LLM call reported usage. **`partial`**: some did. **`unavailable`**: none did (some local servers
  do not report usage on streamed responses). When it is `unavailable` the token fields and the cost are **`null`**, never zero and never an estimate from text length.
* `estimated_cost_usd` is `tokens x price per million`, with `LLM_PRICE_INPUT_PER_M` and `LLM_PRICE_OUTPUT_PER_M` (default **0**, which is correct for a free local model). Set the
  prices only if you pay a provider. It is a convenience figure, not an invoice.
* The backend does not forward `done` data to the browser (it persists the answer and sends its own events); the usage report is for logs, metrics and the evaluation harness, which talks to the agent directly.
* The agent requests `stream_usage` from the provider so streamed responses include token counts where the provider supports it.

## Prometheus metrics (agent, `GET /metrics`, internal token required)

| Metric | Type | Labels |
|---|---|---|
| `lumen_research_total` | counter | `outcome`: completed, clarification, timeout, error, cancelled, unavailable |
| `lumen_research_duration_seconds` | histogram | none |
| `lumen_llm_calls_total` | counter | `result`: ok, error |
| `lumen_llm_tokens_total` | counter | `direction`: input, output (provider-reported only) |
| `lumen_tool_calls_total` | counter | `tool`: the eight known tools, everything else is `other` (bounded cardinality) |
| `lumen_estimated_cost_usd_total` | counter | none; stays 0 while prices are 0 |

Each research also writes one log line with counts only: `research outcome=completed seconds=3.1 llm_calls=2 tool_calls=1 tokens_in=2360 tokens_out=88 token_source=provider`.

## In the evaluation harness

`eval/run_eval.py` stores each case's usage and `summarise` adds totals over only the cases whose provider reported tokens, plus how many could not be counted. A **hermetic** run (scripted LLM) produces
**synthetic** token counts (about 4 characters per token); they exist to prove the plumbing, not to describe any model.

## Not covered

* The optional follow-up suggestions call goes straight to the OpenAI API only when `OPENAI_API_KEY` is set; its tokens are not yet counted. It is skipped in the zero-budget setup.
* No per-user or per-conversation cost attribution, no dashboards or alerts: a deployment would add those.
* Backend-side request latency metrics do not exist yet (the load test measures them from outside).
