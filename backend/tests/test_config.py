"""Configuration safety: no insecure credential can become a silent default."""
import os
import subprocess
import sys
from pathlib import Path

import pytest

from app.config import Settings

SECRET = "k9Zq" * 12
STRONG = "postgresql+psycopg://lumen:" + "a1b2c3d4" * 6 + "@postgres:5432/lumen"
BACKEND_DIR = Path(__file__).resolve().parent.parent


def make(**kw):
    kw.setdefault("jwt_secret", SECRET)
    # The suite's own environment sets ALLOW_INSECURE_DEV_DB=1 for the throwaway test database; these tests
    # exercise the validator itself, so the flag is pinned explicitly (init kwargs beat environment variables).
    kw.setdefault("allow_insecure_dev_db", False)
    kw.setdefault("csrf_secret", "Vq3z" * 12)
    return Settings(_env_file=None, **kw)


def test_strong_database_password_is_accepted():
    assert make(database_url=STRONG).database_url == STRONG


@pytest.mark.parametrize("pw", ["lumen", "postgres", "password", "admin", "root", "changeme", "123456", "LUMEN", "Postgres", "short1", ""])
def test_weak_or_default_database_passwords_are_refused(pw):
    with pytest.raises(Exception, match="weak/default database password"):
        make(database_url=f"postgresql+psycopg://lumen:{pw}@postgres:5432/lumen")


def test_missing_password_is_refused():
    with pytest.raises(Exception, match="weak/default database password"):
        make(database_url="postgresql+psycopg://lumen@postgres:5432/lumen")


def test_database_url_has_no_default_and_is_required():
    saved = os.environ.pop("DATABASE_URL", None)
    try:
        with pytest.raises(Exception, match="database_url"):
            Settings(_env_file=None, jwt_secret=SECRET)
    finally:
        if saved is not None:
            os.environ["DATABASE_URL"] = saved


def test_explicit_dev_opt_in_allows_a_weak_password_on_local_targets_only():
    ok = make(database_url="postgresql+psycopg://lumen:lumen@localhost:5433/lumen", allow_insecure_dev_db=True)
    assert ok.allow_insecure_dev_db
    make(database_url="postgresql+psycopg://lumen:lumen@postgres:5432/lumen", allow_insecure_dev_db=True)  # compose service
    with pytest.raises(Exception, match="only permitted for local databases"):
        make(database_url="postgresql+psycopg://lumen:lumen@db.prod.example.com:5432/lumen", allow_insecure_dev_db=True)


def test_the_opt_in_flag_defaults_to_off(monkeypatch):
    monkeypatch.delenv("ALLOW_INSECURE_DEV_DB", raising=False)
    assert Settings(_env_file=None, jwt_secret=SECRET, database_url=STRONG).allow_insecure_dev_db is False


def test_malformed_database_url_is_refused():
    with pytest.raises(Exception):
        make(database_url="not a url at all")


def test_the_app_process_actually_refuses_to_boot_with_the_legacy_credential():
    """End to end at process level: importing the app with lumen/lumen and no opt-in must fail loudly."""
    env = {**os.environ, "DATABASE_URL": "postgresql+psycopg://lumen:lumen@localhost:5433/lumen",
           "ALLOW_INSECURE_DEV_DB": "0", "JWT_SECRET": SECRET}
    r = subprocess.run([sys.executable, "-c", "import app.main"], cwd=BACKEND_DIR, env=env, capture_output=True, text=True)
    assert r.returncode != 0 and "weak/default database password" in r.stderr


def test_rejected_credentials_are_never_echoed_into_errors():
    """pydantic normally prints the offending input; for secrets that would leak them into logs."""
    pw = "Sup3rS3cretPw"
    with pytest.raises(Exception) as e:
        make(database_url=f"postgresql+psycopg://lumen:{pw}@db.prod.example.com:5432/lumen", allow_insecure_dev_db=True)
    assert pw not in str(e.value) and "db.prod.example.com" not in str(e.value)
    with pytest.raises(Exception) as e2:
        make(database_url=STRONG, jwt_secret="short-leaks-" + "x" * 3)
    assert "short-leaks" not in str(e2.value)


def test_rate_limit_settings_validate():
    assert make(database_url=STRONG).rate_limit_fail_mode == "local"
    with pytest.raises(Exception, match="local' or 'closed"):
        make(database_url=STRONG, rate_limit_fail_mode="open")


# ── production mode: the safe choices are mandatory ─────────────────────────

PROD = dict(
    environment="production", redis_url="redis://redis:6379/1", rate_limit_fail_mode="closed", enable_hsts=True,
    cors_allowed_origins="https://app.example.com", agent_api_key="a-long-agent-key-0123456789abcdef", database_url=STRONG,
    cookie_secure=True,  # cookie sessions (default AUTH_MODE=dual) must use Secure cookies in production
)


def test_a_complete_production_configuration_is_accepted():
    s = make(**PROD)
    assert s.environment == "production" and s.rate_limit_fail_mode == "closed"


@pytest.mark.parametrize(
    "override,fragment",
    [
        ({"rate_limit_fail_mode": "local"}, "RATE_LIMIT_FAIL_MODE must be 'closed'"),
        ({"redis_url": ""}, "REDIS_URL is required"),
        ({"enable_hsts": False}, "ENABLE_HSTS must be true"),
        ({"cors_allowed_origins": "http://app.example.com"}, "https://"),
        ({"cors_allowed_origins": "https://ok.example.com,http://localhost:5173"}, "https://"),
        ({"agent_api_key": ""}, "AGENT_API_KEY must be set"),
        ({"agent_api_key": "short"}, "AGENT_API_KEY must be set"),
        ({"allow_insecure_dev_db": True, "database_url": "postgresql+psycopg://lumen:lumen@postgres:5432/lumen"},
         "ALLOW_INSECURE_DEV_DB must not be set"),
    ],
)
def test_unsafe_production_settings_are_startup_errors(override, fragment):
    with pytest.raises(Exception, match="unsafe production configuration") as e:
        make(**{**PROD, **override})
    assert fragment in str(e.value)


def test_all_production_problems_are_reported_together():
    with pytest.raises(Exception) as e:
        make(environment="production", database_url=STRONG, redis_url="", agent_api_key="", enable_hsts=False)
    msg = str(e.value)
    assert all(x in msg for x in ("RATE_LIMIT_FAIL_MODE", "REDIS_URL", "ENABLE_HSTS", "AGENT_API_KEY"))


def test_development_keeps_the_relaxed_defaults():
    s = make(database_url=STRONG)
    assert s.environment == "development" and s.rate_limit_fail_mode == "local"


def test_environment_must_be_a_known_value():
    with pytest.raises(Exception, match="development' or 'production"):
        make(database_url=STRONG, environment="prod")
    assert make(database_url=STRONG, environment=" Development ").environment == "development"


# ── component-based database settings (AWS Secrets Manager / RDS shape) ─────

def test_database_url_can_be_built_from_components(monkeypatch):
    for k in ("DATABASE_URL",):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("DB_HOST", "mydb.cluster-abc.eu-west-1.rds.amazonaws.com")
    monkeypatch.setenv("DB_PASSWORD", "a1b2c3d4" * 6)
    monkeypatch.setenv("DB_USER", "lumen_app")
    monkeypatch.setenv("DB_NAME", "lumen_prod")
    s = Settings(_env_file=None, jwt_secret=SECRET, allow_insecure_dev_db=False)
    from sqlalchemy.engine import make_url

    u = make_url(s.database_url)
    assert (u.host, u.username, u.database, u.port) == ("mydb.cluster-abc.eu-west-1.rds.amazonaws.com", "lumen_app", "lumen_prod", 5432)
    assert u.password == "a1b2c3d4" * 6


def test_special_characters_in_a_secret_password_survive_url_building(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    nasty = "p@ss:w/ord#%?&=+ spaced_and_long_enough_0123456789"
    monkeypatch.setenv("DB_HOST", "db.internal")
    monkeypatch.setenv("DB_PASSWORD", nasty)
    s = Settings(_env_file=None, jwt_secret=SECRET, allow_insecure_dev_db=False)
    from sqlalchemy.engine import make_url

    assert make_url(s.database_url).password == nasty  # a hand-built URL would have broken on '@', ':' or '/'


def test_database_url_wins_over_components(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", STRONG)
    monkeypatch.setenv("DB_HOST", "ignored.example.com")
    monkeypatch.setenv("DB_PASSWORD", "x" * 40)
    assert Settings(_env_file=None, jwt_secret=SECRET).database_url == STRONG


def test_components_without_a_password_are_still_refused(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("DB_HOST", "db.internal")
    monkeypatch.delenv("DB_PASSWORD", raising=False)
    with pytest.raises(Exception, match="weak/default database password"):
        Settings(_env_file=None, jwt_secret=SECRET, allow_insecure_dev_db=False)


def test_nothing_configured_is_a_clear_error(monkeypatch):
    for k in ("DATABASE_URL", "DB_HOST", "DB_PASSWORD"):
        monkeypatch.delenv(k, raising=False)
    with pytest.raises(Exception, match="database_url is required"):
        Settings(_env_file=None, jwt_secret=SECRET, allow_insecure_dev_db=False)


def test_migrations_resolve_the_url_without_the_apps_secrets(monkeypatch):
    """`python -m app.migrate` must work with only database variables (no JWT_SECRET etc.)."""
    from app.dburl import resolve_database_url

    assert resolve_database_url({"DB_HOST": "h", "DB_PASSWORD": "pw", "DB_USER": "u", "DB_NAME": "n", "DB_PORT": "5433"}) == "postgresql+psycopg://u:pw@h:5433/n"
    assert resolve_database_url({"DATABASE_URL": "x://y"}) == "x://y"
    assert resolve_database_url({}) == ""
