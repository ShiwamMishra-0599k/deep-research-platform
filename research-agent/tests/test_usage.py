"""Usage accounting: tokens come only from the provider, never from a guess; counts are per request; metrics have bounded labels."""
import asyncio

from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, LLMResult
from prometheus_client import REGISTRY

from research_agent.infrastructure.usage import Usage, UsageCallback

from .test_sse_handler import REQ, FakeAgent, collect, install, model_end, parse, tool_end, tool_start


def llm_result(inp=None, out=None, *, via="message"):
    if via == "message":
        meta = {"input_tokens": inp, "output_tokens": out, "total_tokens": (inp or 0) + (out or 0)} if inp is not None else None
        return LLMResult(generations=[[ChatGeneration(message=AIMessage(content="x", usage_metadata=meta))]])
    return LLMResult(generations=[[ChatGeneration(message=AIMessage(content="x"))]], llm_output={"token_usage": {"prompt_tokens": inp, "completion_tokens": out}})


def sample(name, **labels):
    return REGISTRY.get_sample_value(name, labels) or 0.0


# ── the callback and the snapshot ───────────────────────────────────────────

def test_callback_reads_provider_usage_from_the_message_and_from_llm_output():
    u = Usage()
    cb = UsageCallback(u)
    cb.on_llm_end(llm_result(100, 20))
    cb.on_llm_end(llm_result(50, 5, via="llm_output"))
    assert (u.llm_calls, u.input_tokens, u.output_tokens) == (2, 150, 25)
    assert u.token_source == "provider"


def test_no_usage_reported_means_unavailable_and_null_not_zero_or_a_guess():
    u = Usage()
    UsageCallback(u).on_llm_end(llm_result())            # a response with no usage at all
    snap = u.snapshot(3.0, 15.0)
    assert u.llm_calls == 1 and snap["token_source"] == "unavailable"
    assert snap["input_tokens"] is None and snap["output_tokens"] is None and snap["estimated_cost_usd"] is None


def test_partial_usage_is_labelled_partial():
    u = Usage()
    cb = UsageCallback(u)
    cb.on_llm_end(llm_result(10, 2))
    cb.on_llm_end(llm_result())
    assert u.token_source == "partial"


def test_failed_calls_do_not_count_as_missing_usage():
    u = Usage()
    cb = UsageCallback(u)
    cb.on_llm_end(llm_result(10, 2))
    cb.on_llm_error(RuntimeError("provider 500"))
    assert u.llm_calls == 2 and u.llm_errors == 1 and u.token_source == "provider"


def test_cost_is_tokens_times_price_and_zero_by_default():
    u = Usage()
    UsageCallback(u).on_llm_end(llm_result(2_000_000, 500_000))
    assert u.snapshot()["estimated_cost_usd"] == 0.0                        # unset prices: free
    assert u.snapshot(2.0, 10.0)["estimated_cost_usd"] == 2 * 2.0 + 0.5 * 10.0   # 4 + 5 = 9


def test_a_callback_failure_can_never_break_a_research():
    assert UsageCallback.raise_error is False


def test_unknown_tool_names_cannot_create_new_metric_labels():
    before = sample("lumen_tool_calls_total", tool="other")
    u = Usage()
    u.record_tool("web_search")
    u.record_tool("totally_made_up_tool_" + "x" * 50)
    assert sample("lumen_tool_calls_total", tool="other") == before + 1
    assert sample("lumen_tool_calls_total", tool="totally_made_up_tool_" + "x" * 50) == 0.0


# ── through the real stream handler ─────────────────────────────────────────

class UsageAgent(FakeAgent):
    """Fires the LangChain callbacks the way the real graph does, between scripted events."""

    def __init__(self, script, llm_results):
        super().__init__(script)
        self.llm_results = llm_results

    def astream_events(self, inputs, config=None, version=None):
        inner = super().astream_events(inputs, config, version)
        cbs = (config or {}).get("callbacks", [])
        results = self.llm_results

        async def gen():
            async for ev in inner:
                yield ev
                if ev["event"] == "on_tool_end" or ev["event"] == "on_chat_model_end":
                    if results:
                        for cb in cbs:
                            cb.on_llm_end(results.pop(0))

        return gen()


async def test_done_event_carries_this_requests_usage(monkeypatch):
    install(monkeypatch, UsageAgent(
        [tool_start("web_search", query="q"), tool_end("web_search", "1. **t**\n   Snippet: s\n   Source: https://e.com/a", query="q"),
         model_end("An answer.")],
        [llm_result(1000, 50), llm_result(1500, 120)],
    ))
    done = [d for e, d in parse(await collect(REQ)) if e == "done"][0]
    assert done["status"] == "completed"
    assert done["usage"] == {"llm_calls": 2, "tool_calls": 1, "token_source": "provider",
                             "input_tokens": 2500, "output_tokens": 170, "estimated_cost_usd": 0.0}


async def test_prices_from_settings_produce_an_estimate(monkeypatch, settings):
    monkeypatch.setattr(settings, "LLM_PRICE_INPUT_PER_M", 1.0)
    monkeypatch.setattr(settings, "LLM_PRICE_OUTPUT_PER_M", 4.0)
    install(monkeypatch, UsageAgent([model_end("ok")], [llm_result(1_000_000, 250_000)]))
    done = [d for e, d in parse(await collect(REQ)) if e == "done"][0]
    assert done["usage"]["estimated_cost_usd"] == 2.0


async def test_provider_without_usage_is_reported_honestly(monkeypatch):
    install(monkeypatch, UsageAgent([model_end("ok")], [llm_result()]))
    done = [d for e, d in parse(await collect(REQ)) if e == "done"][0]
    assert done["usage"]["token_source"] == "unavailable" and done["usage"]["input_tokens"] is None


async def test_concurrent_requests_do_not_mix_counts(monkeypatch):
    agents = [UsageAgent([model_end(f"a{i}")], [llm_result(100 * (i + 1), 10 * (i + 1))]) for i in range(4)]
    it = iter(agents)

    async def get_agent():
        return next(it)

    from research_agent import http_handler
    monkeypatch.setattr(http_handler, "get_agent", get_agent)
    runs = await asyncio.gather(*(collect(REQ) for _ in range(4)))
    got = sorted(next(d for e, d in parse(r) if e == "done")["usage"]["input_tokens"] for r in runs)
    assert got == [100, 200, 300, 400]


async def test_outcome_metrics_and_error_path_do_not_leak_content(monkeypatch, caplog):
    install(monkeypatch, FakeAgent([model_end("fine")], exc=RuntimeError("secret-internal-detail")))
    before = sample("lumen_research_total", outcome="error")
    with caplog.at_level("INFO", logger="research_agent.http_handler"):
        events = parse(await collect(REQ))
    assert events[-1][0] == "error"
    assert sample("lumen_research_total", outcome="error") == before + 1
    line = next(r.getMessage() for r in caplog.records if r.getMessage().startswith("research outcome="))
    assert "outcome=error" in line and REQ.query not in line and "secret-internal-detail" not in line
