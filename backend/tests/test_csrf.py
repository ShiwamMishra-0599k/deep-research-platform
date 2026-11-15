"""CSRF, Origin validation and CORS for cookie-authenticated requests (plan §5, §6, §11)."""
import json
import uuid

import pytest

from app.config import Settings, settings
from app.research_client import AgentEvent
from app.routers import research as research_router
from tests.cookie_helpers import NEW_CLIENT, PW, csrf_of, login, new_client, register, unique_email, with_csrf

ALLOWED = "http://localhost:5173"
STRONG_DB = "postgresql+psycopg://lumen:" + "a1b2c3d4" * 6 + "@postgres:5432/lumen"


@pytest.fixture()
def browser():
    c = new_client()
    register(c)
    return c


def conv_count(engine_rows):
    return engine_rows("SELECT count(*) FROM conversations")[0][0]


def msg_count(engine_rows):
    return engine_rows("SELECT count(*) FROM messages")[0][0]


# ── missing / wrong CSRF token on every unsafe route: 403 and no side effect ─

def test_create_without_a_csrf_header_is_403_and_creates_nothing(browser, engine_rows):
    r = browser.post("/api/conversations", json={"title": "x"})
    assert r.status_code == 403 and r.json() == {"detail": "CSRF validation failed"}
    assert conv_count(engine_rows) == 0


def test_rename_and_delete_without_csrf_do_nothing(browser, engine_rows):
    cid = browser.post("/api/conversations", json={"title": "keep"}, headers=with_csrf(browser)).json()["id"]
    assert browser.patch(f"/api/conversations/{cid}", json={"title": "hacked"}).status_code == 403
    assert browser.delete(f"/api/conversations/{cid}").status_code == 403
    assert browser.get(f"/api/conversations/{cid}").json()["title"] == "keep"


@pytest.mark.parametrize("label", ["header_not_cookie", "cookie_not_header", "mismatch", "forged_same_both", "foreign_session", "garbage", "empty_header"])
def test_invalid_csrf_variants_are_rejected_with_no_side_effect(browser, engine_rows, label):
    other = new_client()
    register(other)
    foreign = csrf_of(other)
    own = csrf_of(browser)
    forged = "AAAAAAAAAAAAAAAAAAAAAA." + "B" * 43
    if label == "header_not_cookie":
        browser.cookies.delete("lumen_csrf")
        headers = {"X-CSRF-Token": own}
    elif label == "cookie_not_header":
        headers = {}
    elif label == "mismatch":
        headers = {"X-CSRF-Token": foreign}
    elif label == "forged_same_both":
        browser.cookies.set("lumen_csrf", forged)
        headers = {"X-CSRF-Token": forged}
    elif label == "foreign_session":  # a perfectly valid token... for someone else's session, in both places
        browser.cookies.set("lumen_csrf", foreign)
        headers = {"X-CSRF-Token": foreign}
    elif label == "garbage":
        headers = {"X-CSRF-Token": "garbage"}
    else:
        headers = {"X-CSRF-Token": ""}
    r = browser.post("/api/conversations", json={"title": "x"}, headers=headers)
    assert r.status_code == 403 and r.json() == {"detail": "CSRF validation failed"}  # never says which part failed
    assert conv_count(engine_rows) == 0


def test_a_valid_token_works_and_safe_methods_need_none(browser):
    assert browser.get("/api/conversations").status_code == 200
    assert browser.get("/api/auth/me").status_code == 200
    assert browser.post("/api/conversations", json={}, headers=with_csrf(browser)).status_code == 200


def test_csrf_token_from_before_a_refresh_stops_matching_the_cookie(browser):
    old = csrf_of(browser)
    assert browser.post("/api/auth/refresh", headers=with_csrf(browser)).status_code == 200
    assert csrf_of(browser) != old
    assert browser.post("/api/conversations", json={}, headers={"X-CSRF-Token": old}).status_code == 403


def test_a_missing_csrf_cookie_means_403(browser):
    browser.cookies.delete("lumen_csrf")
    assert browser.post("/api/conversations", json={}, headers={"X-CSRF-Token": "x"}).status_code == 403


def test_bearer_requests_are_not_csrf_checked_but_cookie_requests_are():
    legacy_client = new_client()
    legacy = legacy_client.post("/api/auth/register", json={"email": unique_email(), "password": PW, "name": "B"}).json()["token"]
    plain = new_client()  # no cookies at all
    assert plain.post("/api/conversations", json={}, headers={"Authorization": f"Bearer {legacy}"}).status_code == 200
    assert legacy_client.post("/api/conversations", json={}).status_code == 403  # same user, cookie auth, no token


# ── the SSE query endpoint ──────────────────────────────────────────────────

def _fake_agent(monkeypatch):
    def fake(query, history):
        async def gen():
            yield AgentEvent("blocks", json.dumps({"status": "completed", "data": {"blocks": [{"template_id": "markdown", "data": {"content": "ok"}}]}}))
            yield AgentEvent("done", "{}")
        return gen()

    monkeypatch.setattr(research_router, "stream_research", fake)


def test_sse_query_without_csrf_is_403_and_persists_nothing(browser, engine_rows, monkeypatch):
    _fake_agent(monkeypatch)
    cid = browser.post("/api/conversations", json={}, headers=with_csrf(browser)).json()["id"]
    r = browser.post(f"/api/conversations/{cid}/query", json={"query": "hello"})
    assert r.status_code == 403 and "text/event-stream" not in r.headers.get("content-type", "")
    assert msg_count(engine_rows) == 0
    assert research_router.stream_guard.active(str(uuid.uuid4())) == 0


def test_sse_query_with_csrf_streams_and_persists(browser, engine_rows, monkeypatch):
    _fake_agent(monkeypatch)
    cid = browser.post("/api/conversations", json={}, headers=with_csrf(browser)).json()["id"]
    r = browser.post(f"/api/conversations/{cid}/query", json={"query": "hello"}, headers=with_csrf(browser))
    assert r.status_code == 200 and "event: blocks" in r.text and "event: persisted" in r.text
    assert msg_count(engine_rows) == 2


# ── Origin validation ───────────────────────────────────────────────────────

@pytest.mark.parametrize(
    "headers",
    [
        {"Origin": "http://evil.example"},
        {"Origin": "null"},
        {"Origin": "http://localhost:9999"},                    # same SITE as the app, different port: SameSite cannot stop it
        {"Origin": "https://localhost:5173"},                   # wrong scheme
        {"Origin": "http://localhost:5173.evil.example"},       # suffix trick
        {"Sec-Fetch-Site": "cross-site"},                       # no Origin header, but the browser says cross-site
    ],
)
def test_cross_origin_state_changing_requests_are_rejected_even_with_valid_cookie_and_csrf(browser, engine_rows, headers):
    r = browser.post("/api/conversations", json={"title": "x"}, headers={**with_csrf(browser), **headers})
    assert r.status_code == 403 and r.json() == {"detail": "Cross-origin request rejected"}
    assert conv_count(engine_rows) == 0


@pytest.mark.parametrize(
    "headers",
    [{"Origin": ALLOWED}, {"Sec-Fetch-Site": "same-origin"}, {"Sec-Fetch-Site": "same-site"}, {"Sec-Fetch-Site": "none"}, {}],
)
def test_same_origin_and_non_browser_requests_pass(browser, headers):
    assert browser.post("/api/conversations", json={}, headers={**with_csrf(browser), **headers}).status_code == 200


def test_login_csrf_a_forged_cross_site_login_is_blocked(engine_rows):
    victim_email = unique_email()
    register(new_client(), victim_email)
    attacker_page = new_client()
    for h in ({"Origin": "http://evil.example"}, {"Sec-Fetch-Site": "cross-site"}, {"Origin": "null"}):
        r = login(attacker_page, victim_email, **h)
        assert r.status_code == 403
    assert attacker_page.cookies.get("lumen_at") is None
    assert engine_rows("SELECT count(*) FROM auth_sessions")[0][0] == 1  # only the registration's session
    r = attacker_page.post("/api/auth/register", json={"email": unique_email(), "password": PW, "name": "X"},
                           headers={"Origin": "http://evil.example"})
    assert r.status_code == 403 and engine_rows("SELECT count(*) FROM users")[0][0] == 1


def test_origin_check_applies_to_bearer_requests_too():
    legacy = new_client().post("/api/auth/register", json={"email": unique_email(), "password": PW, "name": "B"}).json()["token"]
    r = new_client().post("/api/conversations", json={}, headers={"Authorization": f"Bearer {legacy}", "Origin": "http://evil.example"})
    assert r.status_code == 403


def test_origin_check_does_not_affect_safe_methods_or_cors_preflight():
    c = new_client()
    assert c.get("/health", headers={"Origin": "http://evil.example"}).status_code == 200
    pre = c.options("/api/conversations", headers={"Origin": ALLOWED, "Access-Control-Request-Method": "POST"})
    assert pre.status_code == 200


# ── CORS with credentials ───────────────────────────────────────────────────

def test_cors_preflight_for_an_allowed_origin_permits_credentials_and_the_csrf_headers():
    r = new_client().options("/api/conversations", headers={
        "Origin": ALLOWED, "Access-Control-Request-Method": "POST",
        "Access-Control-Request-Headers": "x-csrf-token,x-auth-mode,content-type"})
    assert r.status_code == 200
    assert r.headers["access-control-allow-origin"] == ALLOWED != "*"
    assert r.headers["access-control-allow-credentials"] == "true"
    assert "origin" in r.headers.get("vary", "").lower()
    allowed = r.headers["access-control-allow-headers"].lower()
    assert "x-csrf-token" in allowed and "x-auth-mode" in allowed


@pytest.mark.parametrize("origin", ["http://evil.example", "null", "http://localhost:9999", "https://localhost:5173"])
def test_cors_preflight_from_a_disallowed_origin_gets_no_allow_origin(origin):
    r = new_client().options("/api/conversations", headers={
        "Origin": origin, "Access-Control-Request-Method": "POST", "Access-Control-Request-Headers": "x-csrf-token"})
    # The property that blocks the browser: refusal and NO Access-Control-Allow-Origin. (Starlette also attaches
    # Access-Control-Allow-Credentials to this 400; without an allow-origin that header grants nothing.)
    assert r.status_code == 400 and "access-control-allow-origin" not in r.headers


def test_actual_responses_vary_on_origin_and_never_use_a_wildcard(browser):
    r = browser.get("/api/auth/me", headers={"Origin": ALLOWED})
    assert r.headers["access-control-allow-origin"] == ALLOWED and r.headers["access-control-allow-credentials"] == "true"
    assert "origin" in r.headers.get("vary", "").lower()
    evil = browser.get("/api/auth/me", headers={"Origin": "http://evil.example"})
    assert "access-control-allow-origin" not in evil.headers  # the browser would block script from reading it


# ── configuration safety ────────────────────────────────────────────────────

def _make(**kw):
    kw.setdefault("jwt_secret", "k9Zq" * 12)
    kw.setdefault("database_url", STRONG_DB)
    kw.setdefault("allow_insecure_dev_db", False)
    return Settings(_env_file=None, **kw)


@pytest.mark.parametrize(
    "secret,fragment",
    [("short", "at least 32"), ("x" * 31, "at least 32"), ("change-me-" + "a" * 30, "placeholder"), ("my-secret-" + "a" * 30, "placeholder"),
     ("k9Zq" * 12, "must differ from JWT_SECRET"), ("", "at least 32")],
)
def test_weak_placeholder_or_reused_csrf_secrets_refuse_to_start(secret, fragment):
    with pytest.raises(Exception, match=fragment):
        _make(csrf_secret=secret)


def test_the_csrf_secret_is_not_required_in_bearer_mode_but_is_in_dual_and_cookie():
    _make(auth_mode="bearer", csrf_secret="")
    for mode in ("dual", "cookie"):
        with pytest.raises(Exception, match="CSRF_SECRET"):
            _make(auth_mode=mode, csrf_secret="")


def test_rejected_csrf_secret_is_not_echoed():
    with pytest.raises(Exception) as e:
        _make(csrf_secret="Sup3rLeakyCsrf")
    assert "Sup3rLeakyCsrf" not in str(e.value)


def test_unknown_auth_mode_is_refused():
    with pytest.raises(Exception, match="AUTH_MODE"):
        _make(csrf_secret="Vq3z" * 12, auth_mode="both")


def test_production_requires_secure_cookies_when_sessions_are_enabled():
    prod = dict(environment="production", redis_url="redis://r:6379/1", rate_limit_fail_mode="closed", enable_hsts=True,
                cors_allowed_origins="https://app.example.com", agent_api_key="a-long-agent-key-0123456789abcdef", csrf_secret="Vq3z" * 12)
    with pytest.raises(Exception, match="COOKIE_SECURE must be true"):
        _make(**prod)
    assert _make(**prod, cookie_secure=True).cookie_names["access"] == "__Host-lumen_at"
    _make(**prod, auth_mode="bearer")  # legacy-only production does not need cookie flags


def test_cookie_names_by_mode():
    assert _make(csrf_secret="Vq3z" * 12).cookie_names == {"access": "lumen_at", "refresh": "lumen_rt", "csrf": "lumen_csrf"}
    assert _make(csrf_secret="Vq3z" * 12, cookie_secure=True).cookie_names == {
        "access": "__Host-lumen_at", "refresh": "__Secure-lumen_rt", "csrf": "__Host-lumen_csrf"}


def test_live_settings_in_the_suite_use_the_documented_defaults():
    assert (settings.access_token_minutes, settings.session_idle_hours, settings.session_absolute_days) == (15, 24, 7)
    assert settings.cookie_samesite == "lax" and settings.auth_mode == "dual" and settings.refresh_reuse_grace_seconds == 10
    assert NEW_CLIENT == {"X-Auth-Mode": "cookie"}
