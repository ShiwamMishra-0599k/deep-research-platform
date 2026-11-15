"""stream_research: SSE framing, events, errors, interruption, cancellation, timeout."""
import asyncio
import json
import re
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from research_agent import http_handler
from research_agent.http_handler import (
    GENERIC_ERROR,
    TIMEOUT_ERROR,
    UNAVAILABLE_ERROR,
    HistoryEntry,
    ResearchRequest,
    stream_research,
)

FRAME = re.compile(r"^event: ([a-z_]+)\ndata: (.*)\n\n$", re.DOTALL)


def parse(frames):
    out = []
    for f in frames:
        m = FRAME.match(f)
        assert m, f"bad SSE framing: {f!r}"
        assert "\n" not in m.group(2), "data must be a single line"
        out.append((m.group(1), json.loads(m.group(2))))
    return out


async def collect(req):
    return [f async for f in stream_research(req)]


def msg(text):
    return SimpleNamespace(content=text)


class FakeAgent:
    """Stands in for the LangGraph agent. `script` is a list of events or callables."""

    def __init__(self, script, *, exc=None, hang=False):
        self.script, self.exc, self.hang = script, exc, hang
        self.closed = False
        self.started = asyncio.Event()

    def astream_events(self, inputs, config=None, version=None):
        self.inputs = inputs
        agent = self

        async def gen():
            try:
                for ev in agent.script:
                    yield ev
                agent.started.set()
                if agent.exc:
                    raise agent.exc
                if agent.hang:
                    await asyncio.sleep(60)
            finally:
                agent.closed = True

        return gen()


def install(monkeypatch, agent):
    async def get_agent():
        return agent

    monkeypatch.setattr(http_handler, "get_agent", get_agent)
    return agent


def tool_start(name, **inp):
    return {"event": "on_tool_start", "name": name, "data": {"input": inp}}


def tool_end(name, out, **inp):
    return {"event": "on_tool_end", "name": name, "data": {"input": inp, "output": msg(out)}}


def model_end(text):
    return {"event": "on_chat_model_end", "name": "m", "data": {"output": msg(text)}}


REQ = ResearchRequest(query="bitcoin price in INR")


async def test_full_happy_path_event_sequence_and_blocks(monkeypatch):
    agent = install(monkeypatch, FakeAgent([
        tool_start("get_crypto_price", coin_id="bitcoin"),
        tool_end("get_crypto_price", "CRYPTO|bitcoin|7140000.5|85210.0|+2.31%\nBitcoin: ...", coin_id="bitcoin"),
        tool_start("web_search", query="btc news"),
        tool_end("web_search", "1. **t**\n   Snippet: s\n   Source: https://e.com/a", query="btc news"),
        model_end("Bitcoin is at ₹71 lakh."),
    ]))
    events = parse(await collect(REQ))
    names = [e for e, _ in events]
    assert names[0] == "checkpoint" and names[-2:] == ["blocks", "done"]
    assert names.count("checkpoint") == 1 + 4  # initial + 2 tool starts + 2 tool ends
    tools_seen = [d["tool"] for e, d in events if e == "checkpoint" and "tool" in d]
    assert tools_seen == ["get_crypto_price", "get_crypto_price", "web_search", "web_search"]
    blocks = next(d for e, d in events if e == "blocks")["data"]
    assert [b["template_id"] for b in blocks["blocks"]] == ["markdown", "insight-cards"]
    assert blocks["sources"] == ["https://e.com/a"] and blocks["query"] == REQ.query
    assert agent.closed  # upstream stream released


async def test_exactly_one_terminal_event(monkeypatch):
    install(monkeypatch, FakeAgent([model_end("answer")]))
    names = [e for e, _ in parse(await collect(REQ))]
    assert sum(n in ("blocks", "clarification", "error") for n in names) == 1


async def test_clarification_path(monkeypatch):
    install(monkeypatch, FakeAgent([model_end("[CLARIFICATION] Which city are you searching in?")]))
    events = parse(await collect(REQ))
    assert events[-2] == ("clarification", {"status": "completed", "content": "Which city are you searching in?"})
    assert events[-1][0] == "done" and not any(e == "blocks" for e, _ in events)


async def test_think_tags_stripped(monkeypatch):
    install(monkeypatch, FakeAgent([model_end("<think>secret reasoning</think>Final answer")]))
    blocks = next(d for e, d in parse(await collect(REQ)) if e == "blocks")["data"]["blocks"]
    assert blocks[0]["data"]["content"] == "Final answer"


async def test_last_model_message_wins_and_list_content(monkeypatch):
    install(monkeypatch, FakeAgent([
        model_end("draft"),
        {"event": "on_chat_model_end", "name": "m", "data": {"output": msg([{"type": "text", "text": "final"}, {"type": "x"}])}},
    ]))
    blocks = next(d for e, d in parse(await collect(REQ)) if e == "blocks")["data"]["blocks"]
    assert blocks[0]["data"]["content"] == "final"


async def test_stream_ends_without_answer_uses_fallback_text(monkeypatch):
    """Interruption: the agent stops emitting before any model answer."""
    install(monkeypatch, FakeAgent([tool_start("web_search", query="q")]))
    events = parse(await collect(REQ))
    blocks = next(d for e, d in events if e == "blocks")["data"]["blocks"]
    assert "couldn't find" in blocks[0]["data"]["content"] and events[-1][0] == "done"


async def test_midstream_exception_emits_generic_error_without_leaking(monkeypatch):
    agent = install(monkeypatch, FakeAgent(
        [tool_start("web_search", query="q")], exc=RuntimeError("Authorization: Bearer sk-SECRET traceback /srv/app.py")))
    frames = await collect(REQ)
    events = parse(frames)
    assert events[-1] == ("error", {"status": "failed", "content": GENERIC_ERROR})
    assert "sk-SECRET" not in "".join(frames) and "traceback" not in "".join(frames)
    assert agent.closed and not any(e == "done" for e, _ in events)


async def test_agent_init_failure_is_generic(monkeypatch):
    async def boom():
        raise RuntimeError("No LLM API key configured sk-xyz")

    monkeypatch.setattr(http_handler, "get_agent", boom)
    frames = await collect(REQ)
    events = parse(frames)
    assert events[-1] == ("error", {"status": "failed", "content": UNAVAILABLE_ERROR})
    assert "sk-xyz" not in "".join(frames)


async def test_block_formatting_failure_is_generic(monkeypatch):
    install(monkeypatch, FakeAgent([model_end("ok")]))

    def explode(*a, **k):
        raise ValueError("internal detail /etc/passwd")

    monkeypatch.setattr(http_handler, "format_blocks", explode)
    frames = await collect(REQ)
    assert parse(frames)[-1] == ("error", {"status": "failed", "content": GENERIC_ERROR})
    assert "/etc/passwd" not in "".join(frames)


async def test_followups_failure_does_not_break_response(monkeypatch):
    install(monkeypatch, FakeAgent([model_end("answer")]))

    async def bad(*a, **k):
        raise RuntimeError("openai down")

    monkeypatch.setattr(http_handler, "generate_follow_ups", bad)
    events = parse(await collect(REQ))
    assert events[-2][0] == "blocks" and events[-2][1]["data"]["follow_ups"] == []


async def test_followups_included_when_available(monkeypatch):
    from research_agent.blocks.schemas import FollowUp

    install(monkeypatch, FakeAgent([model_end("answer")]))

    async def ok(*a, **k):
        return [FollowUp(label="More", query="Tell me more", category="deeper")]

    monkeypatch.setattr(http_handler, "generate_follow_ups", ok)
    blocks = next(d for e, d in parse(await collect(REQ)) if e == "blocks")["data"]
    assert blocks["follow_ups"][0]["label"] == "More"


async def test_newlines_in_user_text_cannot_break_framing(monkeypatch):
    install(monkeypatch, FakeAgent([tool_start("web_search", query="a\n\nevent: evil\ndata: {}\n\n")]))
    events = parse(await collect(ResearchRequest(query="x\r\nevent: error")))  # parse() asserts single-line data
    assert all(e in ("checkpoint", "blocks", "done") for e, _ in events)


async def test_history_roles_mapped_and_date_context_added(monkeypatch):
    agent = install(monkeypatch, FakeAgent([model_end("ok")]))
    req = ResearchRequest(query="now", conversation_history=[
        HistoryEntry(role="user", content="earlier q"), HistoryEntry(role="assistant", content="earlier a")])
    await collect(req)
    kinds = [type(m).__name__ for m in agent.inputs["messages"]]
    assert kinds == ["HumanMessage", "AIMessage", "HumanMessage"]
    assert "Today's date:" in agent.inputs["messages"][-1].content


async def test_client_disconnect_mid_await_closes_agent_stream(monkeypatch):
    """Cancellation while the agent is working must close the upstream stream."""
    agent = install(monkeypatch, FakeAgent([tool_start("web_search", query="q")], hang=True))

    async def consume():
        async for _ in stream_research(REQ):
            pass

    task = asyncio.create_task(consume())
    await asyncio.wait_for(agent.started.wait(), 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert agent.closed


async def test_consumer_aclose_at_yield_closes_agent_stream(monkeypatch):
    """Client disconnect surfaces as GeneratorExit at a yield point."""
    agent = install(monkeypatch, FakeAgent([tool_start("web_search", query="q"), model_end("x")], hang=True))
    gen = stream_research(REQ)
    assert (await gen.__anext__()).startswith("event: checkpoint")     # initial
    assert "Searching the web" in await gen.__anext__()                # tool start, suspended at yield
    await gen.aclose()
    assert agent.closed


async def test_overall_timeout_emits_timeout_error(monkeypatch, settings):
    monkeypatch.setattr(settings, "REQUEST_TIMEOUT_SECONDS", 0.2)
    agent = install(monkeypatch, FakeAgent([], hang=True))
    events = parse(await asyncio.wait_for(collect(REQ), 5))
    assert events[-1] == ("error", {"status": "failed", "content": TIMEOUT_ERROR}) and agent.closed


async def test_concurrent_streams_do_not_cross_contaminate(monkeypatch):
    agents = [FakeAgent([model_end(f"answer {i}")]) for i in range(5)]
    it = iter(agents)

    async def get_agent():
        a = next(it)
        await asyncio.sleep(0)
        return a

    monkeypatch.setattr(http_handler, "get_agent", get_agent)
    results = await asyncio.gather(*[collect(ResearchRequest(query=f"q{i}")) for i in range(5)])
    contents = sorted(next(d for e, d in parse(r) if e == "blocks")["data"]["blocks"][0]["data"]["content"] for r in results)
    assert contents == [f"answer {i}" for i in range(5)]


# ── request model limits ─────────────────────────────────────


def test_request_limits():
    ResearchRequest(query="x" * 4000)
    with pytest.raises(ValidationError):
        ResearchRequest(query="")
    with pytest.raises(ValidationError):
        ResearchRequest(query="x" * 4001)
    with pytest.raises(ValidationError):
        ResearchRequest(query="q", conversation_history=[{"role": "user", "content": "x"}] * 41)
    ResearchRequest(query="q", conversation_history=[{"role": "user", "content": "x"}] * 40)
    with pytest.raises(ValidationError):
        ResearchRequest(query="q", conversation_history=[{"role": "user", "content": "x" * 20001}])
    for role in ("system", "tool", "USER", ""):
        with pytest.raises(ValidationError):
            ResearchRequest(query="q", conversation_history=[{"role": role, "content": "x"}])
