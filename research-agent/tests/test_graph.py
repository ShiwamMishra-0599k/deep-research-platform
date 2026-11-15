"""LLM wiring and agent construction (no network: dummy keys, nothing is invoked)."""
import pytest

from research_agent.agent import graph


@pytest.fixture(autouse=True)
def fresh_agent(monkeypatch):
    monkeypatch.setattr(graph, "_agent", None)


def test_no_llm_key_fails_with_clear_error(monkeypatch):
    monkeypatch.setattr(graph.settings, "OPENROUTER_API_KEY", "")
    monkeypatch.setattr(graph.settings, "OPENAI_API_KEY", "")
    with pytest.raises(RuntimeError, match="No LLM API key"):
        graph._build_model()


def test_openrouter_preferred_over_openai(monkeypatch):
    monkeypatch.setattr(graph.settings, "OPENROUTER_API_KEY", "or-test-not-real")
    monkeypatch.setattr(graph.settings, "OPENAI_API_KEY", "oa-test-not-real")
    m = graph._build_model()
    assert "openrouter.ai" in str(m.openai_api_base) and m.model_name == graph.settings.OPENROUTER_MODEL
    assert m.temperature == graph.settings.LLM_TEMPERATURE


def test_openai_fallback_when_no_openrouter(monkeypatch):
    monkeypatch.setattr(graph.settings, "OPENROUTER_API_KEY", "")
    monkeypatch.setattr(graph.settings, "OPENAI_API_KEY", "oa-test-not-real")
    assert graph._build_model().model_name == graph.settings.OPENAI_MODEL


async def test_agent_builds_once_with_all_eight_tools(monkeypatch):
    monkeypatch.setattr(graph.settings, "OPENROUTER_API_KEY", "or-test-not-real")
    captured = {}
    import deepagents

    real = deepagents.create_deep_agent

    def spy(**kw):
        captured.update(kw)
        return real(**kw)

    monkeypatch.setattr(deepagents, "create_deep_agent", spy)
    a1 = await graph.get_agent()
    a2 = await graph.get_agent()
    assert a1 is a2                                     # singleton
    assert sorted(t.name for t in captured["tools"]) == sorted([
        "web_search", "read_webpage", "wiki_search", "nearby_places",
        "latest_news", "get_stock_price", "get_forex_rate", "get_crypto_price",
    ])
    assert "untrusted" in captured["system_prompt"].lower()


async def test_agent_init_failure_propagates_without_caching(monkeypatch):
    monkeypatch.setattr(graph.settings, "OPENROUTER_API_KEY", "")
    monkeypatch.setattr(graph.settings, "OPENAI_API_KEY", "")
    with pytest.raises(RuntimeError):
        await graph.get_agent()
    assert graph._agent is None
