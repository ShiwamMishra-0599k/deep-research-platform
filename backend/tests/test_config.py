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
