"""Cookie sessions: flags, modes, refresh rotation, reuse detection, expiry, logout, upgrade, limits, hygiene."""
import hashlib
import logging
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

from app import ratelimit, sessions
from app.config import settings
from app.main import app
from app.ratelimit import RedisBackend, SlidingWindowLimiter
from app.routers import auth as auth_router
from tests.cookie_helpers import (
    DEV_NAMES,
    NEW_CLIENT,
    PW,
    csrf_of,
    login,
    new_client,
    register,
    session_row,
    set_cookie_attrs,
    unique_email,
    with_csrf,
)

SECURE_NAMES = {"access": "__Host-lumen_at", "refresh": "__Secure-lumen_rt", "csrf": "__Host-lumen_csrf"}


@pytest.fixture()
def browser():
    return new_client()


def refresh(client, **kw):
    return client.post("/api/auth/refresh", headers=with_csrf(client), **kw)


# ── cookie flags ────────────────────────────────────────────────────────────

@pytest.mark.parametrize("action", ["register", "login"])
def test_cookie_flags_in_development(browser, action):
    email, r = register(browser)
    if action == "login":
        browser.cookies.clear()
        r = login(browser, email)
    at, rt, cs = (set_cookie_attrs(r, DEV_NAMES[k]) for k in ("access", "refresh", "csrf"))
    assert at and rt and cs
    assert at["httponly"] is True and rt["httponly"] is True and "httponly" not in cs  # SPA must read the CSRF cookie only
    assert at["samesite"].lower() == "lax" and cs["samesite"].lower() == "lax" and rt["samesite"].lower() == "strict"
    assert at["path"] == "/" and cs["path"] == "/" and rt["path"] == "/api/auth"
    assert int(at["max-age"]) == settings.access_token_minutes * 60 == 900
    assert 0 < int(rt["max-age"]) <= settings.session_idle_hours * 3600 and abs(int(rt["max-age"]) - int(cs["max-age"])) <= 2
    for c in (at, rt, cs):
        assert "domain" not in c and "secure" not in c  # host-only; Secure only when COOKIE_SECURE=true


def test_secure_mode_uses_prefixed_names_and_the_secure_flag(monkeypatch):
    monkeypatch.setattr(settings, "cookie_secure", True)
    https = TestClient(app, base_url="https://testserver", raise_server_exceptions=False)
    _, r = register(https)
    at, rt, cs = (set_cookie_attrs(r, SECURE_NAMES[k]) for k in ("access", "refresh", "csrf"))
    assert at and rt and cs, r.headers.get_list("set-cookie")
    for c in (at, rt, cs):
        assert c["secure"] is True and "domain" not in c  # required for the __Host-/__Secure- prefixes
    assert at["path"] == "/" and cs["path"] == "/"  # __Host- demands Path=/
    assert at["httponly"] is True and rt["httponly"] is True and "httponly" not in cs
    assert https.get("/api/auth/me").status_code == 200  # and the prefixed cookies authenticate


def test_refresh_cookie_is_only_sent_to_the_auth_path(browser):
    register(browser)
    other = browser.get("/api/conversations")
    assert "lumen_rt" not in other.request.headers.get("cookie", "") and "lumen_at" in other.request.headers["cookie"]
    r = refresh(browser)
    assert "lumen_rt" in r.request.headers["cookie"]


def test_samesite_none_is_not_supported():
    from app.config import Settings

    with pytest.raises(Exception, match="COOKIE_SAMESITE"):
        Settings(_env_file=None, jwt_secret="k9Zq" * 12, csrf_secret="Vq3z" * 12, cookie_samesite="none",
                 database_url="postgresql+psycopg://lumen:" + "a1b2c3d4" * 6 + "@postgres/lumen")


# ── response body / legacy token rules ──────────────────────────────────────

def test_dual_mode_old_clients_still_get_the_legacy_body_token(browser):
    r = browser.post("/api/auth/register", json={"email": unique_email(), "password": PW, "name": "Old"})
    assert r.json()["token"].count(".") == 2  # no X-Auth-Mode header: an unmigrated frontend
    assert set_cookie_attrs(r, "lumen_at") is not None  # cookies are issued too, harmlessly


def test_new_clients_get_no_token_in_the_body(browser):
    _, r = register(browser)
    assert "token" not in r.json() and set(r.json()) == {"id", "email", "name"}


def test_cookie_mode_never_returns_a_body_token(browser, monkeypatch):
    monkeypatch.setattr(settings, "auth_mode", "cookie")
    r = browser.post("/api/auth/register", json={"email": unique_email(), "password": PW, "name": "C"})  # even without the header
    assert "token" not in r.json() and set_cookie_attrs(r, "lumen_at")


def test_bearer_mode_sets_no_cookies_and_returns_the_token(browser, monkeypatch):
    monkeypatch.setattr(settings, "auth_mode", "bearer")
    r = browser.post("/api/auth/register", json={"email": unique_email(), "password": PW, "name": "B"}, headers=NEW_CLIENT)
    assert r.json()["token"] and r.headers.get_list("set-cookie") == []


# ── precedence between Bearer and cookies ───────────────────────────────────

def test_cookie_authenticates_me_and_conversations(browser):
    _, r = register(browser)
    assert browser.get("/api/auth/me").json()["id"] == r.json()["id"]
    c = browser.post("/api/conversations", json={}, headers=with_csrf(browser))
    assert c.status_code == 200
    assert [x["id"] for x in browser.get("/api/conversations").json()] == [c.json()["id"]]


def test_cookie_mode_ignores_the_authorization_header(monkeypatch):
    monkeypatch.setattr(settings, "auth_mode", "dual")
    legacy = new_client().post("/api/auth/register", json={"email": unique_email(), "password": PW, "name": "L"}).json()["token"]
    monkeypatch.setattr(settings, "auth_mode", "cookie")
    assert new_client().get("/api/auth/me", headers={"Authorization": f"Bearer {legacy}"}).status_code == 401


def test_bearer_mode_ignores_cookies(browser, monkeypatch):
    register(browser)  # dual: gets cookies
    monkeypatch.setattr(settings, "auth_mode", "bearer")
    assert browser.get("/api/auth/me").status_code == 401


def test_dual_mode_accepts_both(browser):
    legacy_client = new_client()
    legacy = legacy_client.post("/api/auth/register", json={"email": unique_email(), "password": PW, "name": "D"}).json()["token"]
    assert new_client().get("/api/auth/me", headers={"Authorization": f"Bearer {legacy}"}).status_code == 200
    register(browser)
    assert browser.get("/api/auth/me").status_code == 200


@pytest.mark.parametrize("header", ["Bearer not.a.jwt", "Bearer", "Bearer "])
def test_a_bad_bearer_never_falls_back_to_a_valid_cookie(browser, header):
    register(browser)
    assert browser.get("/api/auth/me", headers={"Authorization": header}).status_code == 401


def test_query_string_token_still_rejected_with_cookies_off(monkeypatch):
    legacy = new_client().post("/api/auth/register", json={"email": unique_email(), "password": PW, "name": "Q"}).json()["token"]
    assert new_client().get(f"/api/auth/me?token={legacy}").status_code == 401


# ── refresh rotation ────────────────────────────────────────────────────────

def test_refresh_rotates_the_token_and_issues_new_cookies(browser, engine_rows):
    email, _ = register(browser)
    rt1, csrf1 = browser.cookies.get("lumen_rt"), csrf_of(browser)
    r = refresh(browser)
    assert r.status_code == 200 and set(r.json()) == {"id", "email", "name"}
    rt2 = browser.cookies.get("lumen_rt")
    assert rt2 and rt2 != rt1 and csrf_of(browser) != csrf1
    assert set_cookie_attrs(r, "lumen_at") and set_cookie_attrs(r, "lumen_rt") and set_cookie_attrs(r, "lumen_csrf")
    row = session_row(engine_rows, email)[0]
    assert row.prev_refresh_hash == hashlib.sha256(rt1.encode()).digest() and row.refresh_hash == hashlib.sha256(rt2.encode()).digest()
    assert browser.get("/api/auth/me").status_code == 200


def test_the_new_refresh_token_keeps_working_across_many_rotations(browser):
    register(browser)
    for _ in range(4):
        assert refresh(browser).status_code == 200
        time.sleep(0.05)
    assert browser.get("/api/auth/me").status_code == 200


def test_replaying_a_rotated_token_inside_the_grace_window_is_409_and_does_not_revoke(browser, engine_rows):
    email, _ = register(browser)
    old_rt = browser.cookies.get("lumen_rt")
    assert refresh(browser).status_code == 200
    thief = new_client()
    thief.cookies.set("lumen_rt", old_rt, path="/api/auth")
    thief.cookies.set("lumen_csrf", csrf_of(browser))
    r = thief.post("/api/auth/refresh", headers=with_csrf(thief))
    assert r.status_code == 409 and r.json() == {"detail": "Refresh in progress"}
    assert session_row(engine_rows, email)[0].revoked_at is None
    assert refresh(browser).status_code == 200  # the legitimate (current) token is unaffected


def test_reuse_detection_revokes_the_whole_session_immediately(browser, engine_rows, monkeypatch):
    monkeypatch.setattr(settings, "refresh_reuse_grace_seconds", 0)
    email, _ = register(browser)
    old_rt = browser.cookies.get("lumen_rt")
    assert refresh(browser).status_code == 200
    time.sleep(0.05)
    attacker = new_client()
    attacker.cookies.set("lumen_rt", old_rt, path="/api/auth")
    r = attacker.post("/api/auth/refresh")
    assert r.status_code == 401
    row = session_row(engine_rows, email)[0]
    assert row.revoked_at is not None and row.revoked_reason == "reuse_detected"
    assert browser.get("/api/auth/me").status_code == 401          # the victim's access cookie dies at once
    assert refresh(browser).status_code == 401                      # and so does the current refresh token
    cleared = set_cookie_attrs(r, "lumen_rt")
    assert cleared and cleared.get("max-age") == "0"                # the browser is told to drop the dead cookies


def test_reuse_detection_is_logged_without_token_material(browser, monkeypatch, caplog):
    monkeypatch.setattr(settings, "refresh_reuse_grace_seconds", 0)
    register(browser)
    old = browser.cookies.get("lumen_rt")
    refresh(browser)
    time.sleep(0.05)
    caplog.set_level(logging.DEBUG)
    a = new_client()
    a.cookies.set("lumen_rt", old, path="/api/auth")
    a.post("/api/auth/refresh")
    assert "reuse detected" in caplog.text and old not in caplog.text


def test_concurrent_refresh_with_the_same_token_has_exactly_one_winner(browser, engine_rows):
    email, _ = register(browser)
    rt, cs = browser.cookies.get("lumen_rt"), csrf_of(browser)
    barrier = threading.Barrier(8)

    def go(_):
        c = new_client()
        c.cookies.set("lumen_rt", rt, path="/api/auth")
        c.cookies.set("lumen_csrf", cs)
        barrier.wait()
        return c.post("/api/auth/refresh", headers={"X-CSRF-Token": cs}).status_code

    with ThreadPoolExecutor(8) as ex:
        codes = list(ex.map(go, range(8)))
    assert codes.count(200) == 1 and all(c in (200, 409) for c in codes), codes
    row = session_row(engine_rows, email)[0]
    assert row.revoked_at is None and row.prev_refresh_hash == hashlib.sha256(rt.encode()).digest()  # one rotation, no revoke


def test_refresh_without_a_cookie_or_with_garbage_is_401_and_clears_cookies(browser):
    assert browser.post("/api/auth/refresh").status_code == 401
    c = new_client()
    c.cookies.set("lumen_rt", "garbage-token", path="/api/auth")
    r = c.post("/api/auth/refresh")
    assert r.status_code == 401 and set_cookie_attrs(r, "lumen_rt")["max-age"] == "0"


def test_refresh_requires_csrf(browser, engine_rows):
    email, _ = register(browser)
    before = session_row(engine_rows, email)[0].refresh_hash
    assert browser.post("/api/auth/refresh").status_code == 403
    assert session_row(engine_rows, email)[0].refresh_hash == before  # nothing rotated


# ── expiry ──────────────────────────────────────────────────────────────────

def test_idle_expiry_ends_the_session(browser, engine_rows):
    email, _ = register(browser)
    engine_rows("UPDATE auth_sessions SET idle_expires_at = now() - interval '1 second'")
    assert browser.get("/api/auth/me").status_code == 401
    r = refresh(browser)
    assert r.status_code == 401 and set_cookie_attrs(r, "lumen_at")["max-age"] == "0"
    assert login(browser, email).status_code == 200  # a fresh login works


def test_absolute_expiry_ends_the_session_even_when_active(browser, engine_rows):
    register(browser)
    engine_rows("UPDATE auth_sessions SET absolute_expires_at = now() - interval '1 second'")
    assert browser.get("/api/auth/me").status_code == 401
    assert refresh(browser).status_code == 401


def test_access_token_expiry_is_recovered_by_refresh(browser, monkeypatch):
    monkeypatch.setattr(settings, "access_token_minutes", -1)  # an access JWT that is already expired
    register(browser)
    assert browser.get("/api/auth/me").status_code == 401
    monkeypatch.setattr(settings, "access_token_minutes", 15)
    assert refresh(browser).status_code == 200
    assert browser.get("/api/auth/me").status_code == 200


def test_refresh_slides_idle_expiry_but_never_past_the_absolute_cap(browser, engine_rows):
    email, _ = register(browser)
    engine_rows("UPDATE auth_sessions SET idle_expires_at = now() + interval '1 minute', "
                "absolute_expires_at = now() + interval '2 hours'")
    assert refresh(browser).status_code == 200
    row = session_row(engine_rows, email)[0]
    assert row.idle_expires_at > row.rotated_at and row.idle_expires_at <= row.absolute_expires_at
    assert (row.absolute_expires_at - row.idle_expires_at).total_seconds() < 1  # capped at 2 h, not 24 h
    cs = set_cookie_attrs(refresh(browser), "lumen_rt")
    assert 7000 < int(cs["max-age"]) <= 7200  # cookie lifetime follows the cap


def test_session_lifetimes_follow_the_configured_policy(browser, engine_rows):
    email, _ = register(browser)
    row = session_row(engine_rows, email)[0]
    created = engine_rows("SELECT created_at FROM auth_sessions")[0][0]
    assert abs((row.idle_expires_at - created).total_seconds() - 24 * 3600) < 5
    assert abs((row.absolute_expires_at - created).total_seconds() - 7 * 24 * 3600) < 5


# ── logout ──────────────────────────────────────────────────────────────────

def test_logout_revokes_and_old_cookies_are_dead(browser, engine_rows):
    email, _ = register(browser)
    old_at, old_rt = browser.cookies.get("lumen_at"), browser.cookies.get("lumen_rt")
    r = browser.post("/api/auth/logout", headers=with_csrf(browser))
    assert r.status_code == 204
    assert all(set_cookie_attrs(r, n)["max-age"] == "0" for n in DEV_NAMES.values())
    row = session_row(engine_rows, email)[0]
    assert row.revoked_at is not None and row.revoked_reason == "logout"
    replay = new_client()
    replay.cookies.set("lumen_at", old_at)
    assert replay.get("/api/auth/me").status_code == 401           # replay of the access cookie
    replay.cookies.set("lumen_rt", old_rt, path="/api/auth")
    assert replay.post("/api/auth/refresh").status_code == 401     # replay of the refresh cookie


def test_logout_requires_csrf_when_a_session_exists(browser, engine_rows):
    email, _ = register(browser)
    assert browser.post("/api/auth/logout").status_code == 403
    assert session_row(engine_rows, email)[0].revoked_at is None


def test_logout_works_with_only_the_refresh_cookie_when_the_access_token_expired(browser, engine_rows, monkeypatch):
    monkeypatch.setattr(settings, "access_token_minutes", -1)
    email, _ = register(browser)
    monkeypatch.setattr(settings, "access_token_minutes", 15)
    assert browser.get("/api/auth/me").status_code == 401
    assert browser.post("/api/auth/logout", headers=with_csrf(browser)).status_code == 204
    assert session_row(engine_rows, email)[0].revoked_at is not None


def test_logout_is_idempotent_and_never_errors(browser):
    assert browser.post("/api/auth/logout").status_code == 204             # nothing at all
    legacy = new_client().post("/api/auth/register", json={"email": unique_email(), "password": PW, "name": "L"}).json()["token"]
    c = new_client()
    assert c.post("/api/auth/logout", headers={"Authorization": f"Bearer {legacy}"}).status_code == 204
    register(browser)
    assert browser.post("/api/auth/logout", headers=with_csrf(browser)).status_code == 204
    assert browser.post("/api/auth/logout").status_code == 204             # again: cookies are gone


def test_logout_all_revokes_every_session_of_the_user(engine_rows):
    a, b = new_client(), new_client()
    email, _ = register(a)
    assert login(b, email).status_code == 200
    assert a.post("/api/auth/logout-all", headers=with_csrf(a)).status_code == 204
    assert all(r.revoked_at is not None and r.revoked_reason == "logout_all" for r in session_row(engine_rows, email))
    assert a.get("/api/auth/me").status_code == 401 and b.get("/api/auth/me").status_code == 401


def test_logout_all_requires_csrf_for_cookie_auth(browser, engine_rows):
    email, _ = register(browser)
    assert browser.post("/api/auth/logout-all").status_code == 403
    assert session_row(engine_rows, email)[0].revoked_at is None


def test_other_sessions_survive_a_single_logout(engine_rows):
    a, b = new_client(), new_client()
    email, _ = register(a)
    login(b, email)
    a.post("/api/auth/logout", headers=with_csrf(a))
    assert b.get("/api/auth/me").status_code == 200


def test_deleting_the_user_kills_the_session(browser, engine_rows):
    email, _ = register(browser)
    engine_rows("DELETE FROM users WHERE email = :e", e=email)
    assert browser.get("/api/auth/me").status_code == 401
    assert engine_rows("SELECT count(*) FROM auth_sessions")[0][0] == 0  # cascaded


# ── authorization is unchanged with cookies ─────────────────────────────────

def test_cookie_sessions_cannot_touch_another_users_conversation():
    a, b = new_client(), new_client()
    register(a), register(b)
    cid = a.post("/api/conversations", json={"title": "private"}, headers=with_csrf(a)).json()["id"]
    assert b.get(f"/api/conversations/{cid}").status_code == 404
    assert b.get(f"/api/conversations/{cid}/messages").status_code == 404
    assert b.patch(f"/api/conversations/{cid}", json={"title": "x"}, headers=with_csrf(b)).status_code == 404
    assert b.delete(f"/api/conversations/{cid}", headers=with_csrf(b)).status_code == 404
    assert a.get(f"/api/conversations/{cid}").json()["title"] == "private"


def test_a_session_for_one_user_cannot_be_used_with_another_users_token(browser, engine_rows):
    """sub must match the session's owner."""
    from app.security import issue_access_token

    a, b = new_client(), new_client()
    register(a), register(b)
    a_sid = uuid.UUID(engine_rows("SELECT s.id FROM auth_sessions s JOIN users u ON u.id=s.user_id ORDER BY u.created_at LIMIT 1")[0][0].hex)
    b_id = uuid.UUID(engine_rows("SELECT id FROM users ORDER BY created_at DESC LIMIT 1")[0][0].hex)
    forged = issue_access_token(b_id, a_sid)  # b's identity bound to a's session
    c = new_client()
    c.cookies.set("lumen_at", forged)
    assert c.get("/api/auth/me").status_code == 401


# ── legacy → cookie upgrade (dual window) ───────────────────────────────────

def test_upgrade_converts_a_legacy_bearer_session_without_signing_out(browser):
    legacy = new_client().post("/api/auth/register", json={"email": unique_email(), "password": PW, "name": "U"}).json()["token"]
    r = browser.post("/api/auth/session/upgrade", headers={"Authorization": f"Bearer {legacy}", **NEW_CLIENT})
    assert r.status_code == 200 and set(r.json()) == {"id", "email", "name"}
    assert set_cookie_attrs(r, "lumen_at") and set_cookie_attrs(r, "lumen_rt") and set_cookie_attrs(r, "lumen_csrf")
    assert browser.get("/api/auth/me").status_code == 200          # cookie session works with no header
    assert browser.post("/api/conversations", json={}, headers=with_csrf(browser)).status_code == 200


def test_upgrade_needs_a_valid_bearer_token_and_only_exists_in_dual_mode(browser, monkeypatch):
    assert browser.post("/api/auth/session/upgrade").status_code == 401
    assert browser.post("/api/auth/session/upgrade", headers={"Authorization": "Bearer junk"}).status_code == 401
    register(browser)  # already a cookie session
    assert browser.post("/api/auth/session/upgrade", headers=with_csrf(browser)).status_code == 400
    for mode in ("cookie", "bearer"):
        monkeypatch.setattr(settings, "auth_mode", mode)
        assert new_client().post("/api/auth/session/upgrade").status_code == 404


def test_upgrade_is_rate_limited_per_ip(monkeypatch):
    monkeypatch.setattr(auth_router, "upgrade_ip_limiter", SlidingWindowLimiter("up-" + uuid.uuid4().hex[:8], 2, 60))
    legacy = new_client().post("/api/auth/register", json={"email": unique_email(), "password": PW, "name": "U"}).json()["token"]
    codes = [new_client().post("/api/auth/session/upgrade", headers={"Authorization": f"Bearer {legacy}"}).status_code for _ in range(4)]
    assert codes == [200, 200, 429, 429]


# ── mode gating ─────────────────────────────────────────────────────────────

def test_refresh_does_not_exist_in_bearer_mode(browser, monkeypatch):
    register(browser)
    monkeypatch.setattr(settings, "auth_mode", "bearer")
    assert refresh(browser).status_code == 404


# ── rate limits on refresh ──────────────────────────────────────────────────

def test_refresh_is_limited_per_ip_with_retry_after(browser, monkeypatch):
    register(browser)
    monkeypatch.setattr(auth_router, "refresh_ip_limiter", SlidingWindowLimiter("rf-" + uuid.uuid4().hex[:8], 2, 60))
    codes = []
    for _ in range(4):
        r = refresh(browser)
        codes.append(r.status_code)
        time.sleep(0.05)
    assert codes[:2] == [200, 200] and codes[2:] == [429, 429] and int(r.headers["Retry-After"]) >= 1


def test_refresh_is_limited_per_session(browser, monkeypatch):
    register(browser)
    monkeypatch.setattr(auth_router, "refresh_session_limiter", SlidingWindowLimiter("rs-" + uuid.uuid4().hex[:8], 1, 60))
    assert refresh(browser).status_code == 200
    r = refresh(browser)
    assert r.status_code == 429 and int(r.headers["Retry-After"]) >= 1


def test_refresh_fails_closed_when_redis_is_down_in_closed_mode(browser, monkeypatch):
    register(browser)
    monkeypatch.setattr(settings, "rate_limit_fail_mode", "closed")
    dead = RedisBackend("redis://127.0.0.1:1/0", 0.2, 60, settings.rate_limit_prefix)
    previous = ratelimit.set_backend(dead)
    try:
        r = refresh(browser)
    finally:
        ratelimit.set_backend(previous)
    assert r.status_code == 503 and r.headers["Retry-After"] == "5"


# ── storage and log hygiene ─────────────────────────────────────────────────

def test_only_hashes_and_a_hashed_ip_are_stored(browser, engine_rows):
    email, _ = register(browser, **{"User-Agent": "Mozilla/5.0 " + "x" * 400})
    rt = browser.cookies.get("lumen_rt")
    row = session_row(engine_rows, email)[0]
    assert bytes(row.refresh_hash) == hashlib.sha256(rt.encode()).digest() and len(row.refresh_hash) == 32
    assert len(row.user_agent) == 200
    assert row.ip_hash is not None and len(row.ip_hash) == 32 and b"127.0.0.1" not in bytes(row.ip_hash)
    dump = " ".join(str(c) for r in engine_rows("SELECT * FROM auth_sessions") for c in r)
    assert rt not in dump and browser.cookies.get("lumen_at") not in dump


def test_secrets_never_appear_in_logs(browser, caplog):
    caplog.set_level(logging.DEBUG)
    register(browser)
    secrets_ = [browser.cookies.get("lumen_rt"), browser.cookies.get("lumen_at"), csrf_of(browser)]
    refresh(browser)
    browser.post("/api/conversations", json={})  # CSRF failure is logged
    browser.post("/api/auth/logout", headers=with_csrf(browser))
    log = caplog.text
    assert "CSRF validation failed" in log
    assert all(s and s not in log for s in secrets_)


def test_purge_expired_sessions_deletes_only_long_dead_sessions(engine_rows):
    from app.db import SessionLocal

    a, b = new_client(), new_client()
    register(a), register(b)
    engine_rows("UPDATE auth_sessions SET absolute_expires_at = now() - interval '8 days' "
                "WHERE id = (SELECT id FROM auth_sessions ORDER BY created_at LIMIT 1)")
    with SessionLocal() as db:
        assert sessions.purge_expired_sessions(db) == 1
    assert engine_rows("SELECT count(*) FROM auth_sessions")[0][0] == 1


def test_csrf_tokens_are_session_bound_and_verify_in_constant_time_helpers():
    sid1, sid2 = uuid.uuid4(), uuid.uuid4()
    t = sessions.make_csrf_token(sid1)
    assert sessions.csrf_valid(t, t, sid1)
    assert not sessions.csrf_valid(t, t, sid2)            # signed for another session
    assert not sessions.csrf_valid(t, t + "x", sid1)      # header != cookie
    assert not sessions.csrf_valid("", "", sid1) and not sessions.csrf_valid(None, t, sid1) and not sessions.csrf_valid(t, None, sid1)
    forged = "AAAAAAAAAAAAAAAAAAAAAA." + "B" * 43
    assert not sessions.csrf_valid(forged, forged, sid1)  # right shape, wrong signature
    assert not sessions.csrf_valid("nodot", "nodot", sid1)
    assert sessions.make_csrf_token(sid1) != sessions.make_csrf_token(sid1)  # fresh randomness each time
