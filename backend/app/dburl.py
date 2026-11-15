"""Resolve the database URL from the environment, without importing the rest of the app's settings.

Used by the app config, `python -m app.migrate` and Alembic, so a migration job needs only database variables
(not the JWT secret etc.).

Two supported forms:

1. DATABASE_URL                      -- one URL (local development, docker compose)
2. DB_HOST [DB_PORT DB_NAME DB_USER] DB_PASSWORD
                                     -- components. This is the shape AWS Secrets Manager / RDS secrets have
                                        (JSON with username, password, host, port, dbname), so an ECS task can inject
                                        each field as its own environment variable and the password is never
                                        glued into a URL by hand (special characters are quoted correctly here).
"""
import os
from collections.abc import Mapping

from sqlalchemy.engine import URL


def resolve_database_url(env: Mapping[str, str] | None = None) -> str:
    env = os.environ if env is None else env
    url = (env.get("DATABASE_URL") or "").strip()
    if url:
        return url
    host = (env.get("DB_HOST") or "").strip()
    if not host:
        return ""
    return URL.create(
        "postgresql+psycopg",
        username=env.get("DB_USER") or "lumen",
        password=env.get("DB_PASSWORD") or None,
        host=host,
        port=int(env.get("DB_PORT") or 5432),
        database=env.get("DB_NAME") or "lumen",
    ).render_as_string(hide_password=False)
