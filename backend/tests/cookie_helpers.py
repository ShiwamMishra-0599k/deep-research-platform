"""Helpers for cookie-session tests."""
import uuid

from fastapi.testclient import TestClient

from app.main import app

PW = "correct-horse-battery"
NEW_CLIENT = {"X-Auth-Mode": "cookie"}  # what the migrated frontend sends: "do not give me the legacy body token"
DEV_NAMES = {"access": "lumen_at", "refresh": "lumen_rt", "csrf": "lumen_csrf"}


def new_client() -> TestClient:
    """An independent browser: its own cookie jar."""
    return TestClient(app, raise_server_exceptions=False)


def unique_email() -> str:
    return f"c-{uuid.uuid4().hex[:10]}@example.com"


def register(client, email: str | None = None, **headers):
    email = email or unique_email()
    r = client.post("/api/auth/register", json={"email": email, "password": PW, "name": "Cookie User"},
                    headers={**NEW_CLIENT, **headers})
    assert r.status_code == 200, r.text
    return email, r


def login(client, email: str, **headers):
    return client.post("/api/auth/login", json={"email": email, "password": PW}, headers={**NEW_CLIENT, **headers})


def csrf_of(client, names=DEV_NAMES) -> str:
    return client.cookies.get(names["csrf"])


def with_csrf(client, names=DEV_NAMES) -> dict:
    return {"X-CSRF-Token": csrf_of(client, names)}


def set_cookie_attrs(response, name: str) -> dict | None:
    """Parse one Set-Cookie header into {'value':..., 'httponly': bool, 'samesite':..., 'path':..., ...}."""
    for raw in response.headers.get_list("set-cookie"):
        parts = [p.strip() for p in raw.split(";")]
        if parts[0].split("=", 1)[0] == name:
            attrs = {"value": parts[0].split("=", 1)[1]}
            for p in parts[1:]:
                k, _, v = p.partition("=")
                attrs[k.lower()] = v if v else True
            return attrs
    return None


def session_row(engine_rows, email: str):
    rows = engine_rows(
        "SELECT s.id, s.revoked_at, s.revoked_reason, s.idle_expires_at, s.absolute_expires_at, s.refresh_hash, "
        "s.prev_refresh_hash, s.rotated_at, s.user_agent, s.ip_hash FROM auth_sessions s JOIN users u ON u.id = s.user_id "
        "WHERE u.email = :e ORDER BY s.created_at", e=email)
    return rows
