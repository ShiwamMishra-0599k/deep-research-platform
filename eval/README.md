# Research-quality evaluation

A small, honest harness that drives the research agent's `POST /research` SSE endpoint with a set of golden questions
(`golden.json`) and scores each trace.

| Scorer | What it checks |
|---|---|
| `protocol` | exactly one terminal event (`blocks` / `clarification` / `error`), then `done`, nothing after |
| `terminal` / `no_error` | the expected outcome happened and the agent did not return an error |
| `tool_selection` | each expected group of tools (any-of within a group) was actually called |
| `block_structure` | the expected block types (markdown, data-table, leaflet-map, insight-cards) are present |
| `citations` | answers that need sources have at least one, all well-formed `http(s)` URLs |
| `forbidden_absent` | prompt-injection canaries (keys, system prompt markers) do not appear in the answer |

Cases marked `manual_review` (for example "must not state a confident future price") are listed in every report and are never auto-passed.

## What has and has not been measured

* **Hermetic run (done):** `research-agent/.venv/Scripts/python.exe eval/run_hermetic.py` starts only the fake LLM (`e2e/fake_llm.py`) and the
  sealed agent, runs the 8 cases the fake model can route, and tears down. Result: 8/8, with a socket-level check that nothing left the machine.
  **This validates the harness and the agent's protocol. It says nothing about real answer quality**, because the fake model picks tools by keyword.
* **Scorer unit tests:** `research-agent/.venv/Scripts/python.exe -m pytest eval/tests` (14 tests; each scorer passes a good trace and fails a specific bad one).
* **Live run (NOT done):** `EVAL_ALLOW_SPEND=1 python eval/run_eval.py --url <agent> --live --max-cases 3` runs the golden set against real
  LLM and search providers. It refuses to start without that opt-in and a case limit. Token counts and cost are not exposed over SSE, so
  cost is not reported here; read it from the provider dashboard and record it next to the run.

Reports are written to `eval/results/` (git-ignored).

## Extending

Add cases to `golden.json` (`hermetic: true` only if `e2e/fake_llm.py` can route them). A case that cannot be scored automatically
belongs under `manual_review`, not in a scorer with a weak heuristic.

## Answer quality (real providers; not run yet)

`quality_set.json` (29 fact-checkable cases), `quality_grading.py` (grade sheets and metrics with 95% intervals) and `cost_model.py` (budget) are described in [`../docs/EVALUATION_PLAN.md`](../docs/EVALUATION_PLAN.md). Run a case file with `--cases quality_set.json`. No score from these exists until you approve credentials and a spending cap.
