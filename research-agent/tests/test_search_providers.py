"""The real provider adapters (Exa, Tavily, DuckDuckGo) against fakes: no network, no keys."""
import sys
import types

import httpx
import pytest

from research_agent.agent import tools


async def test_tavily_parses_and_ignores_garbage_rows(install_http, settings, monkeypatch):
    monkeypatch.setattr(settings, "TAVILY_API_KEY", "tvly-test-not-real")
    seen = {}

    def handler(request):
        seen["body"] = request.content.decode()
        return httpx.Response(200, json={"results": [
            {"title": "T", "content": "C", "url": "https://a.example/1"}, "junk", {"title": None},
        ]})

    install_http(handler)
    rows = await tools._search_tavily("q")
    assert rows[0] == ("T", "C", "https://a.example/1") and len(rows) == 2


async def test_tavily_non_dict_payload_raises_so_chain_falls_through(install_http):
    install_http(lambda r: httpx.Response(200, json=["not", "a", "dict"]))
    with pytest.raises(ValueError):
        await tools._search_tavily("q")


async def test_tavily_http_error_raises(install_http):
    install_http(lambda r: httpx.Response(429))
    with pytest.raises(httpx.HTTPStatusError):
        await tools._search_tavily("q")


async def test_exa_adapter_with_fake_sdk(monkeypatch, settings):
    monkeypatch.setattr(settings, "EXA_API_KEY", "exa-test-not-real")

    class R:
        def __init__(self, t, x, u):
            self.title, self.text, self.url = t, x, u

    class FakeExa:
        def __init__(self, api_key):
            assert api_key == "exa-test-not-real"

        def search_and_contents(self, query, text, num_results):
            return types.SimpleNamespace(results=[R("T", "body", "https://e.example/p"), R(None, None, "")])

    monkeypatch.setitem(sys.modules, "exa_py", types.SimpleNamespace(Exa=FakeExa))
    rows = await tools._search_exa("q")
    assert rows == [("T", "body", "https://e.example/p"), ("", "", "")]


async def test_ddg_adapter_runs_off_the_event_loop_with_fake_sdk(monkeypatch):
    class FakeDDGS:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def text(self, query, max_results):
            return [{"title": "T", "body": "B", "href": "https://d.example/x"}, {"title": "no href"}]

    monkeypatch.setitem(sys.modules, "duckduckgo_search", types.SimpleNamespace(DDGS=FakeDDGS))
    rows = await tools._search_ddg("q")
    assert rows[0] == ("T", "B", "https://d.example/x") and rows[1][2] == ""
    # and the formatter then drops the row that has no usable URL
    assert tools._format_search_results(rows).count("Source:") == 1


def test_format_search_results_dedupes_by_url_and_requires_http():
    rows = [("a", "s", "https://x.example/1"), ("b", "s", "https://x.example/1"), ("c", "s", "ftp://x.example/2")]
    out = tools._format_search_results(rows)
    assert out.count("Source:") == 1 and "ftp://" not in out
