"""Backend test fixtures.

Tests run against a REAL, isolated PostgreSQL (default: the throwaway container on
127.0.0.1:55432, database `lumen_test`). Set TEST_DATABASE_URL to use another one.
Safety: the schema is only ever dropped when the host is local and the database name
ends in `_test`; the application's real database can never be wiped by the suite.

    docker run -d --name lumen-test-pg -e POSTGRES_DB=lumen_test -e POSTGRES_USER=lumen_test \
        -e POSTGRES_PASSWORD=lumen_test -p 127.0.0.1:55432:5432 postgres:16-alpine
"""
import os
import sys
import uuid
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

TEST_DB_URL = os.environ.get(
    "TEST_DATABASE_URL", "postgresql+psycopg://lumen_test:lumen_test@127.0.0.1:55432/lumen_test"
)
_url = make_url(TEST_DB_URL)
assert _url.host in ("127.0.0.1", "localhost", "::1"), "tests may only use a local database"
assert (_url.database or "").endswith("_test"), "test database name must end in _test"

# Must be set BEFORE the app (and its settings) are imported.
os.environ["DATABASE_URL"] = TEST_DB_URL
os.environ["ALLOW_INSECURE_DEV_DB"] = "1"  # the throwaway test container uses a deliberately weak, local-only password
os.environ["JWT_SECRET"] = "k9Zq" * 12  # 48 chars, no placeholder markers
os.environ["AGENT_API_KEY"] = "test-agent-key-0123456789abcdef"
os.environ["CORS_ALLOWED_ORIGINS"] = "http://localhost:5173"
TEST_REDIS_URL = os.environ.get("TEST_REDIS_URL", "redis://127.0.0.1:56379/3")
os.environ["REDIS_URL"] = TEST_REDIS_URL
# Generous timeouts for the suite: tests assert EXACT counts and must not be knocked onto the fallback by CI jitter.
# (Tests that exercise outages/timeouts build their own backends with explicit tiny timeouts.)
os.environ["REDIS_TIMEOUT_SECONDS"] = "2.0"
os.environ["REDIS_RETRY_SECONDS"] = "1.0"  # the whole suite runs against real Redis when it is reachable
os.environ["LOGIN_PER_IP_PER_MINUTE"] = "1000"  # individual tests lower this explicitly
os.environ["QUERIES_PER_USER_PER_MINUTE"] = "1000"

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def _admin_engine():
    return create_engine(TEST_DB_URL, isolation_level="AUTOCOMMIT")


def reset_database_schema() -> None:
    eng = _admin_engine()
    with eng.connect() as c:
        c.execute(text("DROP SCHEMA public CASCADE"))
        c.execute(text("CREATE SCHEMA public"))
    eng.dispose()


@pytest.fixture(scope="session", autouse=True)
def _database():
    try:
        eng = _admin_engine()
        with eng.connect() as c:
            c.execute(text("SELECT 1"))
        eng.dispose()
    except Exception as e:  # noqa: BLE001
        pytest.exit(f"Test PostgreSQL not reachable at {_url.render_as_string(hide_password=True)}: {e}", 2)

    from app.migrate import run_migrations

    reset_database_schema()
    run_migrations(TEST_DB_URL)  # also exercises the real migrations on every run
    yield


@pytest.fixture(autouse=True)
def _clean_state():
    """Empty tables and reset in-process limiters between tests."""
    eng = _admin_engine()
    with eng.connect() as c:
        tables = {r[0] for r in c.execute(text("SELECT tablename FROM pg_tables WHERE schemaname='public'"))}
        if "users" in tables:
            c.execute(text("TRUNCATE users CASCADE"))
    eng.dispose()

    from app import deps, ratelimit
    from app.routers import auth, research

    for limiter in (auth.login_ip_limiter, auth.register_ip_limiter, research.query_limiter, deps.api_user_limiter):
        limiter.reset()
    auth.login_lockout.reset()
    research.stream_guard.reset()
    research.global_stream_guard.reset()
    ratelimit.get_backend()._down_until = 0.0  # a previous test's simulated outage must not leak
    yield


@pytest.fixture()
def client():
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app, raise_server_exceptions=False) as c:
        yield c


def _unique_email() -> str:
    return f"user-{uuid.uuid4().hex[:10]}@example.com"


@pytest.fixture()
def make_user(client):
    """Register a user; returns dict(token, id, email, headers)."""

    def _make(email: str | None = None, password: str = "correct-horse-battery", name: str = "Test User"):
        email = email or _unique_email()
        r = client.post("/api/auth/register", json={"email": email, "password": password, "name": name})
        assert r.status_code == 200, r.text
        data = r.json()
        return {
            "token": data["token"],
            "id": data["id"],
            "email": data["email"],
            "password": password,
            "headers": {"Authorization": f"Bearer {data['token']}"},
        }

    return _make


@pytest.fixture()
def alice(make_user):
    return make_user("alice@example.com")


@pytest.fixture()
def bob(make_user):
    return make_user("bob@example.com")


@pytest.fixture()
def engine_rows():
    """Run raw SQL against the test DB; returns rows for SELECTs."""

    def _run(sql: str, **params):
        eng = _admin_engine()
        try:
            with eng.connect() as c:
                res = c.execute(text(sql), params)
                return list(res.fetchall()) if res.returns_rows else []
        finally:
            eng.dispose()

    return _run
