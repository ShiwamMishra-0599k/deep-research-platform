import base64
import json
import threading
import time
import uuid

import pytest
from jose import jwt as jose_jwt

from app.config import settings
from app.routers import auth as auth_router


def _forge(claims: dict, key: str | None = None, alg: str = "HS256") -> str:
    return jose_jwt.encode(claims, key or settings.jwt_secret, algorithm=alg)


def _claims(user_id: str, **over) -> dict:
    now = int(time.time())
    base = {
        "sub": user_id, "iss": settings.jwt_issuer, "aud": settings.jwt_audience,
        "iat": now, "exp": now + 3600,
    }
    base.update(over)
    return base


# ── registration ────────────────────────────────────────────────────────────

def test_register_returns_token_and_user(client):
    r = client.post("/api/auth/register", json={"email": "New@Example.com", "password": "longenough1", "name": " Ann "})
    assert r.status_code == 200
    body = r.json()
    assert body["email"] == "new@example.com"  # normalised
    assert body["name"] == "Ann"  # trimmed
    assert body["token"].count(".") == 2


def test_password_is_hashed_not_stored(client, engine_rows):
    client.post("/api/auth/register", json={"email": "h@example.com", "password": "plaintext-pw-1", "name": "H"})
    (pw_hash,) = engine_rows("SELECT password_hash FROM users WHERE email='h@example.com'")[0]
    assert pw_hash.startswith("$2") and "plaintext-pw-1" not in pw_hash


def test_duplicate_email_409_case_insensitive(client):
    payload = {"password": "longenough1", "name": "A"}
    assert client.post("/api/auth/register", json={"email": "d@example.com", **payload}).status_code == 200
    assert client.post("/api/auth/register", json={"email": "D@EXAMPLE.COM", **payload}).status_code == 409


@pytest.mark.parametrize(
    "body",
    [
        {"email": "not-an-email", "password": "longenough1", "name": "A"},
        {"email": "a@example.com", "password": "short", "name": "A"},
        {"email": "a@example.com", "password": "x" * 73, "name": "A"},
        {"email": "a@example.com", "password": "é" * 40, "name": "A"},  # 80 bytes
        {"email": "a@example.com", "password": "longenough1", "name": "   "},
        {"email": "a@example.com", "password": "longenough1", "name": "n" * 81},
        {"email": "a@example.com", "password": "longenough1"},
        {},
    ],
)
def test_register_validation_rejects(client, body):
    assert client.post("/api/auth/register", json=body).status_code == 422


def test_validation_errors_do_not_echo_the_password(client):
    r = client.post("/api/auth/register", json={"email": "bad", "password": "SuperSecretPw123", "name": "A"})
    assert r.status_code == 422
    assert "SuperSecretPw123" not in r.text


def test_concurrent_registration_of_same_email_creates_one_user(client, engine_rows):
    results: list[int] = []

    def go():
        results.append(
            client.post("/api/auth/register", json={"email": "race@example.com", "password": "longenough1", "name": "R"}).status_code
        )

    threads = [threading.Thread(target=go) for _ in range(6)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert sorted(results).count(200) == 1
    assert set(results) <= {200, 409}  # never a 500
    assert engine_rows("SELECT count(*) FROM users WHERE email='race@example.com'")[0][0] == 1


# ── login ───────────────────────────────────────────────────────────────────

def test_login_success(client, alice):
    r = client.post("/api/auth/login", json={"email": "ALICE@example.com", "password": alice["password"]})
    assert r.status_code == 200 and r.json()["id"] == alice["id"]


def test_login_wrong_password_and_unknown_user_look_identical(client, alice):
    wrong = client.post("/api/auth/login", json={"email": "alice@example.com", "password": "nope-nope-nope"})
    unknown = client.post("/api/auth/login", json={"email": "ghost@example.com", "password": "nope-nope-nope"})
    assert wrong.status_code == unknown.status_code == 401
    assert wrong.json() == unknown.json() == {"detail": "Invalid credentials"}


def test_login_throttles_per_ip(client, alice, monkeypatch):
    monkeypatch.setattr(auth_router, "login_ip_limiter", auth_router.SlidingWindowLimiter("t-login-" + uuid.uuid4().hex[:8], 3, 60))
    codes = [
        client.post("/api/auth/login", json={"email": f"x{i}@example.com", "password": "whatever-pw"}).status_code
        for i in range(5)
    ]
    assert codes[:3] == [401, 401, 401] and codes[3:] == [429, 429]
    r = client.post("/api/auth/login", json={"email": "x@example.com", "password": "whatever-pw"})
    assert r.status_code == 429 and int(r.headers["Retry-After"]) >= 1


def test_account_locks_after_repeated_failures_even_for_correct_password(client, alice):
    for _ in range(settings.login_failures_per_email):
        assert client.post("/api/auth/login", json={"email": "alice@example.com", "password": "wrong-wrong"}).status_code == 401
    r = client.post("/api/auth/login", json={"email": "alice@example.com", "password": alice["password"]})
    assert r.status_code == 429


def test_lockout_applies_to_unknown_emails_too(client):
    for _ in range(settings.login_failures_per_email):
        client.post("/api/auth/login", json={"email": "nobody@example.com", "password": "wrong-wrong"})
    r = client.post("/api/auth/login", json={"email": "nobody@example.com", "password": "wrong-wrong"})
    assert r.status_code == 429  # same behaviour as a real account: no enumeration signal


def test_successful_login_resets_failure_counter(client, alice):
    for _ in range(settings.login_failures_per_email - 1):
        client.post("/api/auth/login", json={"email": "alice@example.com", "password": "wrong-wrong"})
    assert client.post("/api/auth/login", json={"email": "alice@example.com", "password": alice["password"]}).status_code == 200
    assert client.post("/api/auth/login", json={"email": "alice@example.com", "password": "wrong-wrong"}).status_code == 401


def test_register_is_rate_limited(client, monkeypatch):
    monkeypatch.setattr(auth_router, "register_ip_limiter", auth_router.SlidingWindowLimiter("t-register-" + uuid.uuid4().hex[:8], 2, 3600))
    codes = [
        client.post("/api/auth/register", json={"email": f"r{i}@example.com", "password": "longenough1", "name": "R"}).status_code
        for i in range(4)
    ]
    assert codes == [200, 200, 429, 429]


# ── tokens ──────────────────────────────────────────────────────────────────

def test_me_with_valid_token(client, alice):
    r = client.get("/api/auth/me", headers=alice["headers"])
    assert r.status_code == 200 and r.json()["email"] == "alice@example.com"


def test_token_expiry(client, alice):
    expired = _forge(_claims(alice["id"], iat=int(time.time()) - 7200, exp=int(time.time()) - 3600))
    assert client.get("/api/auth/me", headers={"Authorization": f"Bearer {expired}"}).status_code == 401


def test_newly_issued_token_has_expected_claims_and_lifetime(alice):
    claims = jose_jwt.get_unverified_claims(alice["token"])
    assert claims["iss"] == settings.jwt_issuer and claims["aud"] == settings.jwt_audience
    assert claims["sub"] == alice["id"] and "jti" in claims
    assert claims["exp"] - claims["iat"] == settings.jwt_expiration_minutes * 60
    assert "email" not in claims and "name" not in claims  # no PII in the token


@pytest.mark.parametrize("label", ["garbage", "empty", "wrong_secret", "alg_none", "wrong_aud", "wrong_iss", "no_exp", "sub_not_uuid", "unknown_user", "tampered_payload"])
def test_invalid_tokens_are_rejected(client, alice, label):
    uid = alice["id"]
    token = {
        "garbage": "not.a.jwt",
        "empty": "",
        "wrong_secret": _forge(_claims(uid), key="another-key-" * 5),
        "alg_none": base64.urlsafe_b64encode(b'{"alg":"none","typ":"JWT"}').rstrip(b"=").decode() + "."
                    + base64.urlsafe_b64encode(json.dumps(_claims(uid)).encode()).rstrip(b"=").decode() + ".",
        "wrong_aud": _forge(_claims(uid, aud="someone-else")),
        "wrong_iss": _forge(_claims(uid, iss="evil")),
        "no_exp": _forge({k: v for k, v in _claims(uid).items() if k != "exp"}),
        "sub_not_uuid": _forge(_claims("alice@example.com")),
        "unknown_user": _forge(_claims("00000000-0000-0000-0000-000000000000")),
        "tampered_payload": alice["token"].split(".")[0] + "." + base64.urlsafe_b64encode(json.dumps(_claims("00000000-0000-0000-0000-000000000001")).encode()).rstrip(b"=").decode() + "." + alice["token"].split(".")[2],
    }[label]
    r = client.get("/api/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 401
    assert r.headers.get("www-authenticate") == "Bearer"


def test_hs256_pinned_rejects_other_hmac_algorithms(client, alice):
    assert client.get("/api/auth/me", headers={"Authorization": "Bearer " + _forge(_claims(alice["id"]), alg="HS512")}).status_code == 401


def test_token_in_query_string_is_not_accepted(client, alice):
    """Regression: `?token=` auth leaked credentials into URLs and logs."""
    assert client.get(f"/api/auth/me?token={alice['token']}").status_code == 401
    assert client.get(f"/api/conversations?token={alice['token']}").status_code == 401


@pytest.mark.parametrize("header", ["", "Bearer", "Bearer ", "Basic abc", "Token x", "bearer"])
def test_malformed_authorization_headers(client, header):
    assert client.get("/api/auth/me", headers={"Authorization": header}).status_code == 401


def test_missing_authorization_is_401(client):
    assert client.get("/api/auth/me").status_code == 401
    assert client.get("/api/conversations").status_code == 401


def test_deleted_user_token_stops_working(client, alice, engine_rows):
    engine_rows("DELETE FROM users WHERE email='alice@example.com'")
    assert client.get("/api/auth/me", headers=alice["headers"]).status_code == 401


# ── configuration safety ────────────────────────────────────────────────────

@pytest.mark.parametrize("bad", ["short", "change-me-in-prod-use-a-long-random-string-at-least-32-bytes", "x" * 31, "my-secret-value-" + "a" * 30])
def test_weak_or_placeholder_jwt_secret_refuses_to_start(bad):
    from app.config import Settings

    with pytest.raises(Exception):
        Settings(jwt_secret=bad)


def test_wildcard_cors_origin_is_refused():
    from app.config import Settings

    with pytest.raises(ValueError):
        Settings(jwt_secret="k9Zq" * 12, cors_allowed_origins="*").cors_origins  # noqa: B018
