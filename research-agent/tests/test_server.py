"""HTTP surface: auth (fail closed), request limits, no CORS, health/metrics protection."""
import json

import httpx
import pytest

from research_agent import http_handler
from research_agent.server import create_app

TOKEN = "unit-test-internal-token-1234567890"


@pytest.fixture
def keyed(settings, monkeypatch):
    monkeypatch.setattr(settings, "AGENT_API_KEY", TOKEN)
    return settings


@pytest.fixture
async def client():
    app = create_app()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://agent.test") as c:
        yield c


class _Agent:
    def astream_events(self, inputs, config=None, version=None):
        async def gen():
            yield {"event": "on_chat_model_end", "name": "m", "data": {"output": type("O", (), {"content": "hello"})()}}

        return gen()


@pytest.fixture
def fake_agent(monkeypatch):
    async def get_agent():
        return _Agent()

    monkeypatch.setattr(http_handler, "get_agent", get_agent)


H = {"X-Internal-Token": TOKEN}


# ── fail closed / auth ───────────────────────────────────────


async def test_unconfigured_key_fails_closed_503(client):
    for headers in ({}, {"X-Internal-Token": ""}, {"X-Internal-Token": "anything"}):
        r = await client.post("/research", json={"query": "hi"}, headers=headers)
        assert r.status_code == 503


async def test_missing_and_wrong_token_401(client, keyed):
    assert (await client.post("/research", json={"query": "hi"})).status_code == 401
    assert (await client.post("/research", json={"query": "hi"}, headers={"X-Internal-Token": "wrong"})).status_code == 401
    assert (await client.post("/research", json={"query": "hi"}, headers={"X-Internal-Token": TOKEN + "x"})).status_code == 401
    assert (await client.post("/research", json={"query": "hi"}, headers={"X-Internal-Token": TOKEN[:-1]})).status_code == 401
    assert (await client.post("/research", json={"query": "hi"}, headers={"Authorization": f"Bearer {TOKEN}"})).status_code == 401


async def test_auth_checked_before_body_is_parsed(client, keyed):
    r = await client.post("/research", content=b"{not json", headers={"Content-Type": "application/json"})
    assert r.status_code == 401


async def test_valid_token_streams_sse(client, keyed, fake_agent):
    async with client.stream("POST", "/research", json={"query": "hi"}, headers=H) as r:
        assert r.status_code == 200 and r.headers["content-type"].startswith("text/event-stream")
        assert r.headers["cache-control"] == "no-cache"
        body = (await r.aread()).decode()
    events = [ln[7:] for ln in body.splitlines() if ln.startswith("event: ")]
    assert events == ["checkpoint", "blocks", "done"]


async def test_health_open_but_leaks_nothing(client, keyed):
    r = await client.get("/health")
    assert r.status_code == 200 and r.json() == {"status": "ok"}


@pytest.mark.parametrize("path", ["/health/ready", "/metrics"])
async def test_ops_endpoints_require_token(client, keyed, path):
    assert (await client.get(path)).status_code == 401
    assert (await client.get(path, headers={"X-Internal-Token": "bad"})).status_code == 401


async def test_ops_endpoints_fail_closed_without_key(client):
    assert (await client.get("/health/ready")).status_code == 503
    assert (await client.get("/metrics")).status_code == 503


async def test_ready_reports_state_with_token(client, keyed, settings, monkeypatch):
    monkeypatch.setattr(settings, "OPENROUTER_API_KEY", "")
    monkeypatch.setattr(settings, "OPENAI_API_KEY", "")
    r = await client.get("/health/ready", headers=H)
    assert r.status_code == 503 and r.json()["status"] == "not_ready"
    monkeypatch.setattr(settings, "OPENROUTER_API_KEY", "k")
    r = await client.get("/health/ready", headers=H)
    # Redis not connected in this app instance (no lifespan) and no search key -> degraded, still 200
    assert r.status_code == 200 and r.json()["status"] == "degraded"
    assert "sk" not in json.dumps(r.json()) and "k" != r.json()["checks"]["llm"]


async def test_metrics_with_token(client, keyed):
    r = await client.get("/metrics", headers=H)
    assert r.status_code == 200 and "python_info" in r.text


# ── request limits and validation ────────────────────────────


async def test_oversized_body_413_via_content_length(client, keyed, settings, monkeypatch):
    monkeypatch.setattr(settings, "MAX_BODY_BYTES", 1000)
    r = await client.post("/research", content=b"x" * 5000, headers={**H, "Content-Type": "application/json"})
    assert r.status_code == 413


async def test_oversized_chunked_body_413_without_content_length(client, keyed, settings, monkeypatch):
    monkeypatch.setattr(settings, "MAX_BODY_BYTES", 1000)

    async def chunks():
        for _ in range(20):
            yield b"x" * 100

    r = await client.post("/research", content=chunks(), headers={**H, "Content-Type": "application/json"})
    assert r.status_code == 413


@pytest.mark.parametrize(
    "payload",
    [
        b"{not json",
        b"[]",
        b"{}",
        b'{"query": ""}',
        b'{"query": 5}',
        json.dumps({"query": "x" * 4001}).encode(),
        json.dumps({"query": "q", "conversation_history": [{"role": "system", "content": "x"}]}).encode(),
        json.dumps({"query": "q", "conversation_history": [{"role": "user", "content": "x"}] * 41}).encode(),
        json.dumps({"query": "q", "conversation_history": [{"role": "user", "content": "x" * 20001}]}).encode(),
    ],
)
async def test_invalid_bodies_422(client, keyed, payload):
    r = await client.post("/research", content=payload, headers={**H, "Content-Type": "application/json"})
    assert r.status_code == 422


async def test_422_never_echoes_submitted_input(client, keyed):
    secret = "SUPER-SECRET-VALUE-123"
    r = await client.post("/research", json={"query": "q", "conversation_history": [{"role": secret, "content": secret}]}, headers=H)
    assert r.status_code == 422 and secret not in r.text


# ── no browser exposure ──────────────────────────────────────


async def test_no_cors_headers_even_for_browser_origins(client, keyed, fake_agent):
    r = await client.post("/research", json={"query": "hi"}, headers={**H, "Origin": "https://evil.example"})
    assert "access-control-allow-origin" not in r.headers
    pre = await client.options(
        "/research",
        headers={"Origin": "https://evil.example", "Access-Control-Request-Method": "POST",
                 "Access-Control-Request-Headers": "x-internal-token"},
    )
    assert pre.status_code in (404, 405) and "access-control-allow-origin" not in pre.headers


async def test_api_docs_disabled(client, keyed):
    for path in ("/docs", "/redoc", "/openapi.json"):
        assert (await client.get(path)).status_code == 404


async def test_token_not_logged_or_echoed(client, keyed, caplog):
    caplog.set_level("DEBUG")
    r = await client.post("/research", json={"query": "q"}, headers={"X-Internal-Token": "wrong-secret-abc"})
    assert "wrong-secret-abc" not in r.text and "wrong-secret-abc" not in caplog.text
