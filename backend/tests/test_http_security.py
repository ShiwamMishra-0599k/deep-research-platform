"""Security headers, body limits, CORS and information disclosure."""
import json

import pytest


def test_security_headers_on_success_and_error_responses(client, alice):
    for r in (
        client.get("/health"),
        client.get("/api/auth/me", headers=alice["headers"]),
        client.get("/api/auth/me"),  # 401
        client.post("/api/auth/register", json={}),  # 422
        client.get("/nope"),  # 404
    ):
        h = r.headers
        assert h["x-content-type-options"] == "nosniff"
        assert h["x-frame-options"] == "DENY"
        assert h["referrer-policy"] == "no-referrer"
        assert "default-src 'none'" in h["content-security-policy"]


def test_api_responses_are_not_cacheable(client, alice):
    assert client.get("/api/auth/me", headers=alice["headers"]).headers["cache-control"] == "no-store"


def test_no_interactive_docs_or_openapi_exposed(client):
    for p in ("/docs", "/redoc", "/openapi.json"):
        assert client.get(p).status_code == 404


def test_hsts_only_when_enabled(client, monkeypatch):
    assert "strict-transport-security" not in client.get("/health").headers
    from app.config import settings

    monkeypatch.setattr(settings, "enable_hsts", True)
    assert "max-age" in client.get("/health").headers["strict-transport-security"]


def test_declared_oversize_body_gets_413(client):
    r = client.post("/api/auth/login", content=b"x" * (70 * 1024), headers={"Content-Type": "application/json"})
    assert r.status_code == 413


def test_streamed_oversize_body_gets_413(client):
    def chunks():
        for _ in range(80):
            yield b"a" * 1024

    r = client.post("/api/auth/login", content=chunks(), headers={"Content-Type": "application/json"})
    assert r.status_code == 413


def test_normal_sized_body_still_works(client, alice):
    assert client.post("/api/auth/login", json={"email": "alice@example.com", "password": alice["password"]}).status_code == 200


def test_query_longer_than_4000_chars_is_rejected(client, alice):
    cid = client.post("/api/conversations", json={}, headers=alice["headers"]).json()["id"]
    r = client.post(f"/api/conversations/{cid}/query", json={"query": "q" * 4001}, headers=alice["headers"])
    assert r.status_code == 422
    assert client.post(f"/api/conversations/{cid}/query", json={"query": ""}, headers=alice["headers"]).status_code == 422


# ── CORS ────────────────────────────────────────────────────────────────────

def test_cors_allows_configured_origin_only(client):
    ok = client.options("/api/auth/login", headers={
        "Origin": "http://localhost:5173", "Access-Control-Request-Method": "POST",
        "Access-Control-Request-Headers": "authorization,content-type",
    })
    assert ok.headers["access-control-allow-origin"] == "http://localhost:5173"
    assert ok.headers["access-control-allow-credentials"] == "true"

    evil = client.options("/api/auth/login", headers={"Origin": "https://evil.example", "Access-Control-Request-Method": "POST"})
    assert "access-control-allow-origin" not in evil.headers
    simple = client.get("/health", headers={"Origin": "https://evil.example"})
    assert "access-control-allow-origin" not in simple.headers


def test_cors_never_reflects_wildcard(client):
    r = client.get("/health", headers={"Origin": "http://localhost:5173"})
    assert r.headers["access-control-allow-origin"] != "*"


def test_cors_restricts_methods_and_headers(client):
    r = client.options("/api/auth/login", headers={
        "Origin": "http://localhost:5173", "Access-Control-Request-Method": "PUT",
    })
    assert r.status_code == 400  # PUT is not an allowed method
    r = client.options("/api/auth/login", headers={
        "Origin": "http://localhost:5173", "Access-Control-Request-Method": "POST",
        "Access-Control-Request-Headers": "x-evil-header",
    })
    assert r.status_code == 400


# ── information disclosure ──────────────────────────────────────────────────

def test_unhandled_error_does_not_leak_internals(client, alice, monkeypatch):
    import app.routers.conversations as conv

    def boom(*a, **k):
        raise RuntimeError("postgres://user:hunter2@internal-db/prod exploded")

    monkeypatch.setattr(conv, "_owned_or_404", boom)
    r = client.get("/api/conversations/00000000-0000-0000-0000-000000000000", headers=alice["headers"])
    assert r.status_code == 500
    assert r.json() == {"detail": "Internal server error"}
    assert "hunter2" not in r.text and "Traceback" not in r.text


def test_validation_error_shape_has_no_input_values(client):
    r = client.post("/api/auth/login", json={"email": "a@b.co", "password": 123456789})
    assert r.status_code == 422
    body = json.dumps(r.json())
    assert "123456789" not in body and "input" not in r.json()["detail"][0]


@pytest.mark.parametrize("path", ["/api/auth/register", "/api/auth/login"])
def test_wrong_content_type_is_rejected_cleanly(client, path):
    r = client.post(path, content=b"email=a@b.co&password=x", headers={"Content-Type": "application/x-www-form-urlencoded"})
    assert r.status_code == 422
