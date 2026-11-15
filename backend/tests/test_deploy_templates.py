"""The AWS deployment template must stay consistent with what the application actually reads."""
import json
from pathlib import Path

import pytest
from sqlalchemy.engine import make_url

from app.config import Settings

ROOT = Path(__file__).resolve().parent.parent.parent
TASKDEF = ROOT / "deploy" / "aws" / "ecs-task-definition.example.json"
DB_COMPONENTS = {"DB_HOST", "DB_PORT", "DB_NAME", "DB_USER", "DB_PASSWORD"}
SECRET_NAMES = {"JWT_SECRET", "CSRF_SECRET", "AGENT_API_KEY", "DB_PASSWORD", "REDIS_URL", "OPENROUTER_API_KEY", "EXA_API_KEY",
                "TAVILY_API_KEY", "OPENAI_API_KEY", "DB_USER", "DB_HOST"}


@pytest.fixture(scope="module")
def container():
    return json.loads(TASKDEF.read_text(encoding="utf-8"))["containerDefinitions"][0]


def test_every_name_in_the_template_is_something_the_application_reads(container):
    known = {f.upper() for f in Settings.model_fields} | DB_COMPONENTS
    names = {e["name"] for e in container["environment"]} | {s["name"] for s in container["secrets"]}
    assert names <= known, f"template sets variables the backend ignores: {sorted(names - known)}"


def test_no_secret_is_ever_a_plaintext_environment_value(container):
    plain = {e["name"] for e in container["environment"]}
    assert not (plain & SECRET_NAMES), f"secrets must use valueFrom, not environment: {sorted(plain & SECRET_NAMES)}"
    for s in container["secrets"]:
        assert s["valueFrom"].startswith("arn:aws:secretsmanager:") and "<" in s["valueFrom"]  # placeholder ARN, no literal value


def test_template_contains_no_literal_credentials():
    text = TASKDEF.read_text(encoding="utf-8")
    for needle in ("AKIA", "sk-or-", "BEGIN PRIVATE", "password\":", "PASSWORD\": \""):
        assert needle not in text


def test_the_template_produces_a_valid_production_configuration(container, monkeypatch):
    """Substitute dummy secret values for the injected variables and let the real validators judge the result."""
    for k in ("DATABASE_URL", "ALLOW_INSECURE_DEV_DB", "REDIS_URL", "RATE_LIMIT_FAIL_MODE", "CORS_ALLOWED_ORIGINS", "ENABLE_HSTS",
              "TRUSTED_PROXY_CIDRS", "AUTH_MODE", "COOKIE_SECURE", "CSRF_SECRET"):
        monkeypatch.delenv(k, raising=False)
    env = {e["name"]: e["value"].replace("<PUBLIC_HOST>", "app.example.com") for e in container["environment"]}
    secrets = {
        "JWT_SECRET": "j" * 8 + "Zq9" * 12, "CSRF_SECRET": "c" * 8 + "Xk7" * 12, "AGENT_API_KEY": "agent-key-" + "a1" * 12,
        "DB_USER": "lumen_app", "DB_PASSWORD": "p@ss:w/ord#%?&=+ spaced_and_long_enough_0123456789",
        "DB_HOST": "db.internal.example", "DB_PORT": "5432", "DB_NAME": "lumen_prod",
        "REDIS_URL": "rediss://:token@cache.example:6379/0",
    }
    assert {s["name"] for s in container["secrets"]} == set(secrets), "template injects a different secret set than this test models"
    for k, v in {**env, **secrets}.items():
        monkeypatch.setenv(k, v)
    s = Settings(_env_file=None)
    assert s.environment == "production" and s.rate_limit_fail_mode == "closed" and s.enable_hsts
    u = make_url(s.database_url)
    assert u.host == "db.internal.example" and u.password == secrets["DB_PASSWORD"] and u.database == "lumen_prod"


def test_the_template_fails_validation_if_a_safety_setting_is_removed(container, monkeypatch):
    for k in ("DATABASE_URL", "ALLOW_INSECURE_DEV_DB"):
        monkeypatch.delenv(k, raising=False)
    env = {e["name"]: e["value"].replace("<PUBLIC_HOST>", "app.example.com") for e in container["environment"]}
    env.pop("RATE_LIMIT_FAIL_MODE")  # forgetting fail-closed must be a startup error, not a silent default
    for k, v in {**env, "JWT_SECRET": "j" * 8 + "Zq9" * 12, "CSRF_SECRET": "c" * 8 + "Xk7" * 12, "AGENT_API_KEY": "agent-key-" + "a1" * 12,
                 "DB_HOST": "db", "DB_PASSWORD": "a1b2c3d4" * 6, "REDIS_URL": "rediss://x@y:6379/0"}.items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("RATE_LIMIT_FAIL_MODE", raising=False)
    with pytest.raises(Exception, match="RATE_LIMIT_FAIL_MODE must be 'closed'"):
        Settings(_env_file=None)
