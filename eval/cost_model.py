"""Budget calculator for the real-provider evaluation. Spends nothing, calls nothing.

    python eval/cost_model.py [--inr-per-usd 94.25] [--llm-in 2 --llm-out 10] [--search-per-1k 7]

EVERY number below is either
  * a provider list price reported by THIRD-PARTY sites on 2026-11-15 and NOT yet confirmed on the provider's own pricing page, or
  * an ASSUMPTION about how many tokens and searches one research task uses. No token usage has been measured: the agent does not
    record it yet. Replace the assumptions with measured values from the pilot run (EVALUATION_PLAN.md, step 3) and re-run.

Prices must be re-read on these official pages before any key is used:
  OpenRouter  https://openrouter.ai/pricing  and  https://openrouter.ai/docs/faq     (credit purchase fee, BYOK terms)
  OpenAI      https://developers.openai.com/api/docs/pricing                          (model rates)
  Anthropic   https://platform.claude.com  (pricing)                                  (Claude rates, if used directly or via OpenRouter)
  Exa         https://exa.ai/pricing                                                  (search, contents, free tier)
  Tavily      https://docs.tavily.com/guides/api-credits                              (credits per search, plans)
"""
import argparse

# (low, base, high) per research task. ASSUMPTIONS, not measurements. The agent loop re-sends its growing context on every step,
# so input tokens dominate and scale with the number of tool calls.
TASK = {
    "input_tokens": (30_000, 80_000, 200_000),
    "output_tokens": (2_000, 4_000, 8_000),
    "searches": (3, 5, 10),
}
JUDGE_TOKENS = (6_000, 1_000)   # per graded answer if an LLM judge is used instead of / alongside a human (assumption)
OPENROUTER_CREDIT_FEE = 0.055   # reported by third-party guides; $0.80 minimum per purchase; verify
TAVILY_USD_PER_CREDIT = 0.008   # pay-as-you-go, reported by third-party sites; advanced search = 2 credits, basic = 1 (per Tavily docs excerpt)
TAVILY_FREE_CREDITS = 1000      # per month, reported; verify


def llm_cost(i, o, p_in, p_out, fee=0.0):
    return (i * p_in + o * p_out) / 1_000_000 * (1 + fee)


def task_cost(level, p_in, p_out, fee, search):
    i, o, s = TASK["input_tokens"][level], TASK["output_tokens"][level], TASK["searches"][level]
    llm = llm_cost(i, o, p_in, p_out, fee)
    return llm, s * search


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--inr-per-usd", type=float, default=94.25, help="ECB reference 2026-11-13; moves")
    ap.add_argument("--llm-in", type=float, default=2.0, help="USD per 1M input tokens (reported for a Sonnet-class model; UNVERIFIED)")
    ap.add_argument("--llm-out", type=float, default=10.0, help="USD per 1M output tokens (UNVERIFIED)")
    ap.add_argument("--search-per-1k", type=float, default=7.0, help="Exa search with contents, USD per 1000 requests (reported; UNVERIFIED)")
    ap.add_argument("--cases", type=int, default=19, help="size of eval/quality_set.json")
    a = ap.parse_args()
    fx = a.inr_per_usd

    print(f"Inputs: LLM ${a.llm_in}/${a.llm_out} per 1M tokens in/out (UNVERIFIED), search ${a.search_per_1k}/1000 (UNVERIFIED), INR {fx}/USD")
    print("Tokens and searches per task are ASSUMPTIONS (low/base/high): "
          f"{TASK['input_tokens']} in, {TASK['output_tokens']} out, {TASK['searches']} searches.\n")
    search_each = a.search_per_1k / 1000
    plans = [
        ("Pilot: 5 cases x 1 run", 5),
        (f"Full set: {a.cases} cases x 1 run", a.cases),
        (f"Full set x 3 runs (variance): {a.cases * 3} runs", a.cases * 3),
        (f"Two configurations x {a.cases} cases (comparison)", a.cases * 2),
    ]
    print(f"{"Plan":<62}{"low":>22}{"base":>22}{"high":>22}")
    for name, runs in plans:
        row = []
        for lvl in (0, 1, 2):
            llm, srch = task_cost(lvl, a.llm_in, a.llm_out, OPENROUTER_CREDIT_FEE, search_each)
            usd = runs * (llm + srch)
            row.append(f"${usd:,.2f}/INR {usd * fx:,.0f}")
        print(f"{name:<62}{row[0]:>22}{row[1]:>22}{row[2]:>22}")
    j = llm_cost(*JUDGE_TOKENS, a.llm_in, a.llm_out)
    print(f"\nOptional LLM judge, {a.cases} answers: ${a.cases * j:,.2f} (INR {a.cases * j * fx:,.0f}) with the same price assumption. "
          "A human grader costs time, not API spend.")
    print("Excluded: taxes (GST) and card/forex fees, any model you may choose that costs more than the default above, retries, "
          "failed runs that still consumed tokens, and the Tavily/Exa plan fees if the free tier is exceeded.")
    print(f"Tavily: a full single run at the base assumption uses about {a.cases * TASK['searches'][1] * 2} advanced-search credits "
          f"against a reported free allowance of {TAVILY_FREE_CREDITS}/month.")


if __name__ == "__main__":
    main()
