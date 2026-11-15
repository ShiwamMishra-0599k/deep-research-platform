"""Behaviour when dependencies fail: users get a clear error, never a hang or a stack trace."""
import time

import pytest
from sqlalchemy import create_engine

import app.db as dbmod
import app.main as mainmod


@pytest.fixture()
def database_down(monkeypatch):
    """Point every DB access at a closed port (nothing is listening on 127.0.0.1:1)."""
    dead = create_engine(
        "postgresql+psycopg://x:y@127.0.0.1:1/none", connect_args={"connect_timeout": 2}, pool_pre_ping=False
    )
    monkeypatch.setattr(dbmod.SessionLocal, "kw", {**dbmod.SessionLocal.kw, "bind": dead})
    monkeypatch.setattr(mainmod, "engine", dead)
    import app.routers.research as research

    monkeypatch.setattr(research, "SessionLocal", dbmod.SessionLocal)
    yield
    dead.dispose()


def test_login_with_database_down_is_503_and_clean(client, database_down):
    start = time.time()
    r = client.post("/api/auth/login", json={"email": "a@example.com", "password": "whatever-pw"})
    assert r.status_code == 503
    assert r.json() == {"detail": "Service temporarily unavailable"}
    assert "127.0.0.1" not in r.text and "psycopg" not in r.text.lower()
    assert time.time() - start < 10  # fails fast, does not hang


def test_authenticated_routes_with_database_down(client, alice, database_down):
    assert client.get("/api/conversations", headers=alice["headers"]).status_code == 503
    assert client.get("/api/auth/me", headers=alice["headers"]).status_code == 503


def test_query_with_database_down_is_503_and_releases_slot(client, alice, database_down, monkeypatch):
    import app.routers.research as research

    r = client.post("/api/conversations/00000000-0000-0000-0000-000000000000/query", json={"query": "x"}, headers=alice["headers"])
    # auth itself needs the DB, so this is rejected as unavailable before any stream starts
    assert r.status_code == 503
    assert research.stream_guard.active(alice["id"]) == 0


def test_readiness_reports_database_failure(client, database_down):
    r = client.get("/health/ready")
    assert r.status_code == 503 and r.json()["database"] == "error"
    assert client.get("/health").status_code == 200  # liveness is independent of the database


def test_readiness_ok_when_database_up(client):
    body = client.get("/health/ready").json()
    assert body["status"] == "ready" and body["database"] == "ok" and body["rate_limit"] in ("redis", "memory")
