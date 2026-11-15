"""research_client: the SSE parser and its failure handling, against a mocked agent transport."""
import httpx
import pytest

from app import research_client as rc
from app.config import settings


def _patch_transport(monkeypatch, handler):
    real = httpx.AsyncClient

    def factory(*a, **kw):
        kw["transport"] = httpx.MockTransport(handler)
        return real(*a, **kw)

    monkeypatch.setattr(rc.httpx, "AsyncClient", factory)


async def _collect(query="q", history=None):
    return [(e.name, e.data) async for e in rc.stream_research(query, history or [])]


def _sse_response(body: bytes, status=200):
    return httpx.Response(status, content=body, headers={"content-type": "text/event-stream"})


async def test_parses_multiple_events_and_ignores_comments(monkeypatch):
    body = b': keepalive\nevent: checkpoint\ndata: {"a":1}\n\nevent: blocks\ndata: {"b":2}\n\nevent: done\ndata: {}\n\n'
    _patch_transport(monkeypatch, lambda req: _sse_response(body))
    assert await _collect() == [("checkpoint", '{"a":1}'), ("blocks", '{"b":2}'), ("done", "{}")]


async def test_multiline_data_is_joined_and_crlf_tolerated(monkeypatch):
    body = b"event: blocks\r\ndata: line1\r\ndata: line2\r\n\r\n"
    _patch_transport(monkeypatch, lambda req: _sse_response(body))
    assert await _collect() == [("blocks", "line1\nline2")]


async def test_final_event_without_trailing_blank_line_is_still_delivered(monkeypatch):
    _patch_transport(monkeypatch, lambda req: _sse_response(b'event: blocks\ndata: {"x":1}\n'))
    assert await _collect() == [("blocks", '{"x":1}')]


async def test_data_without_event_name_is_ignored(monkeypatch):
    _patch_transport(monkeypatch, lambda req: _sse_response(b'data: {"x":1}\n\nevent: done\ndata: {}\n\n'))
    assert await _collect() == [("done", "{}")]


async def test_sends_internal_token_and_body(monkeypatch):
    seen = {}

    def handler(req: httpx.Request):
        seen["token"] = req.headers.get("x-internal-token")
        seen["body"] = req.content
        seen["url"] = str(req.url)
        return _sse_response(b"event: done\ndata: {}\n\n")

    _patch_transport(monkeypatch, handler)
    await _collect("hello", [{"role": "user", "content": "hi"}])
    assert seen["token"] == settings.agent_api_key and seen["url"].endswith("/research")
    assert b'"query":"hello"' in seen["body"].replace(b" ", b"") and b"conversation_history" in seen["body"]


@pytest.mark.parametrize("status", [401, 403, 404, 429, 500, 503])
async def test_non_2xx_is_agent_unavailable_without_leaking_body(monkeypatch, status):
    _patch_transport(monkeypatch, lambda req: httpx.Response(status, text="internal: OPENROUTER_API_KEY=sk-leak"))
    with pytest.raises(rc.AgentUnavailable) as e:
        await _collect()
    assert "sk-leak" not in str(e.value) and str(status) in str(e.value)


@pytest.mark.parametrize("exc", [httpx.ConnectError("refused"), httpx.ConnectTimeout("t"), httpx.ReadTimeout("t"), httpx.RemoteProtocolError("p"), httpx.ReadError("r")])
async def test_network_failures_are_agent_unavailable(monkeypatch, exc):
    def handler(req):
        raise exc

    _patch_transport(monkeypatch, handler)
    with pytest.raises(rc.AgentUnavailable):
        await _collect()


async def test_oversized_event_is_a_protocol_error(monkeypatch):
    monkeypatch.setattr(rc, "MAX_EVENT_BYTES", 1000)
    _patch_transport(monkeypatch, lambda req: _sse_response(b"event: blocks\ndata: " + b"x" * 5000 + b"\n\n"))
    with pytest.raises(rc.AgentProtocolError):
        await _collect()


async def test_redirects_from_agent_are_not_followed(monkeypatch):
    _patch_transport(monkeypatch, lambda req: httpx.Response(302, headers={"location": "http://169.254.169.254/"}))
    with pytest.raises(rc.AgentUnavailable):
        await _collect()


async def test_unreachable_agent_real_socket(monkeypatch):
    """No mocks: a closed local port must raise AgentUnavailable quickly."""
    monkeypatch.setattr(settings, "research_agent_url", "http://127.0.0.1:1")
    with pytest.raises(rc.AgentUnavailable):
        await _collect()
