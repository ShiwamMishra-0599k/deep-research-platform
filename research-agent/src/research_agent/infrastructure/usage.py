"""Per-research usage accounting: LLM calls, provider-reported tokens, tool calls, estimated cost.

Why this exists: until now nobody could say how many tokens a research used. This module only COUNTS what the provider reports; it never
estimates tokens from text. If a provider does not report usage (some local servers, or a streaming response without it), the report
says so (`token_source`) instead of inventing a number.

Cost is `tokens x price per million` with prices from settings. Both prices default to 0, which is the correct figure for a free local
model and for the zero-budget plan; set them only if you actually pay a provider. The estimate is a convenience, not an invoice.

Nothing here records prompts, answers, URLs or user identifiers: counters and a few labels only.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from langchain_core.callbacks import BaseCallbackHandler
from prometheus_client import Counter, Histogram

RESEARCH_TOTAL = Counter("lumen_research_total", "Research requests by outcome", ["outcome"])
RESEARCH_SECONDS = Histogram(
    "lumen_research_duration_seconds", "Wall-clock seconds per research",
    buckets=(1, 2, 5, 10, 20, 40, 80, 160, 320),
)
LLM_CALLS_TOTAL = Counter("lumen_llm_calls_total", "LLM calls finished, by result", ["result"])
LLM_TOKENS_TOTAL = Counter("lumen_llm_tokens_total", "Tokens reported by the provider", ["direction"])
TOOL_CALLS_TOTAL = Counter("lumen_tool_calls_total", "Tool invocations", ["tool"])
COST_USD_TOTAL = Counter("lumen_estimated_cost_usd_total", "Estimated provider cost in USD (0 when prices are unset)")

# Tool names become metric label values: allow only ones we know, so a model cannot create unbounded label cardinality.
KNOWN_TOOLS = frozenset({
    "web_search", "read_webpage", "wiki_search", "nearby_places", "latest_news",
    "get_stock_price", "get_forex_rate", "get_crypto_price",
})


@dataclass
class Usage:
    llm_calls: int = 0
    llm_errors: int = 0
    calls_with_usage: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    tool_calls: int = 0

    def record_tool(self, name: str) -> None:
        self.tool_calls += 1
        TOOL_CALLS_TOTAL.labels(tool=name if name in KNOWN_TOOLS else "other").inc()

    def record_llm(self, input_tokens: int | None, output_tokens: int | None) -> None:
        self.llm_calls += 1
        LLM_CALLS_TOTAL.labels(result="ok").inc()
        if input_tokens is not None or output_tokens is not None:
            i, o = max(int(input_tokens or 0), 0), max(int(output_tokens or 0), 0)
            self.calls_with_usage += 1
            self.input_tokens += i
            self.output_tokens += o
            LLM_TOKENS_TOTAL.labels(direction="input").inc(i)
            LLM_TOKENS_TOTAL.labels(direction="output").inc(o)

    def record_llm_error(self) -> None:
        self.llm_calls += 1
        self.llm_errors += 1
        LLM_CALLS_TOTAL.labels(result="error").inc()

    @property
    def token_source(self) -> str:
        """'provider' = every successful call reported usage; 'partial' = some did; 'unavailable' = none did (or no calls)."""
        ok_calls = self.llm_calls - self.llm_errors
        if ok_calls <= 0 or self.calls_with_usage == 0:
            return "unavailable"
        return "provider" if self.calls_with_usage >= ok_calls else "partial"

    def estimated_cost_usd(self, price_in_per_m: float, price_out_per_m: float) -> float:
        return (self.input_tokens * price_in_per_m + self.output_tokens * price_out_per_m) / 1_000_000

    def snapshot(self, price_in_per_m: float = 0.0, price_out_per_m: float = 0.0) -> dict[str, Any]:
        src = self.token_source
        have = src != "unavailable"
        return {
            "llm_calls": self.llm_calls,
            "tool_calls": self.tool_calls,
            "token_source": src,
            # Counts are only meaningful when the provider reported them; otherwise null, never a guess.
            "input_tokens": self.input_tokens if have else None,
            "output_tokens": self.output_tokens if have else None,
            "estimated_cost_usd": round(self.estimated_cost_usd(price_in_per_m, price_out_per_m), 6) if have else None,
        }


class UsageCallback(BaseCallbackHandler):
    """LangChain callback bound to ONE research request (one Usage), so concurrent requests cannot mix counts."""

    raise_error = False   # accounting must never break a research

    def __init__(self, usage: Usage):
        self.usage = usage

    @staticmethod
    def _tokens(response: Any) -> tuple[int | None, int | None]:
        for gens in getattr(response, "generations", None) or []:
            for gen in gens:
                meta = getattr(getattr(gen, "message", None), "usage_metadata", None)
                if meta and ("input_tokens" in meta or "output_tokens" in meta):
                    return meta.get("input_tokens"), meta.get("output_tokens")
        out = getattr(response, "llm_output", None) or {}
        tu = out.get("token_usage") or out.get("usage") or {}
        if tu:
            return tu.get("prompt_tokens", tu.get("input_tokens")), tu.get("completion_tokens", tu.get("output_tokens"))
        return None, None

    def on_llm_end(self, response: Any, **kwargs: Any) -> None:
        i, o = self._tokens(response)
        self.usage.record_llm(i, o)

    def on_llm_error(self, error: BaseException, **kwargs: Any) -> None:
        self.usage.record_llm_error()


def observe_research(outcome: str, seconds: float, usage: Usage, price_in: float, price_out: float) -> None:
    RESEARCH_TOTAL.labels(outcome=outcome).inc()
    RESEARCH_SECONDS.observe(seconds)
    cost = usage.estimated_cost_usd(price_in, price_out)
    if cost > 0:
        COST_USD_TOTAL.inc(cost)
