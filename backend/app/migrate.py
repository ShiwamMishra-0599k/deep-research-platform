"""Run database migrations:  python -m app.migrate

Safe to run on every start and from several replicas at once:
- takes a PostgreSQL advisory lock so migrations never run concurrently;
- adopts a database created by the old `create_all` startup hook by stamping the baseline
  revision (no DDL, no data touched);
- refuses to guess if the schema is only partly present.
"""
import logging
import sys
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text

from .dburl import resolve_database_url

log = logging.getLogger("lumen.migrate")

_LOCK_ID = 727_274_001
_BASELINE = "0001"
_CORE_TABLES = {"users", "conversations", "messages"}


def _alembic_config(connection) -> Config:
    cfg = Config(str(Path(__file__).resolve().parent.parent / "alembic.ini"))
    cfg.set_main_option("script_location", str(Path(__file__).resolve().parent.parent / "alembic"))
    cfg.attributes["connection"] = connection
    return cfg


def run_migrations(database_url: str | None = None) -> None:
    url = database_url or resolve_database_url()
    if not url:
        raise RuntimeError("DATABASE_URL (or DB_HOST + DB_PASSWORD) is not set")

    engine = create_engine(url, connect_args={"connect_timeout": 10})
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT pg_advisory_lock(:id)"), {"id": _LOCK_ID})
            conn.commit()
            try:
                tables = set(inspect(conn).get_table_names())
                cfg = _alembic_config(conn)

                if "alembic_version" not in tables:
                    present = _CORE_TABLES & tables
                    if present == _CORE_TABLES:
                        log.warning("Existing pre-Alembic schema found; stamping baseline %s", _BASELINE)
                        command.stamp(cfg, _BASELINE)
                        conn.commit()
                    elif present:
                        raise RuntimeError(
                            f"Partial schema (found {sorted(present)}, missing "
                            f"{sorted(_CORE_TABLES - present)}). Refusing to guess; fix manually."
                        )

                command.upgrade(cfg, "head")
                conn.commit()
            finally:
                conn.execute(text("SELECT pg_advisory_unlock(:id)"), {"id": _LOCK_ID})
                conn.commit()
    finally:
        engine.dispose()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    try:
        run_migrations()
    except Exception as e:  # noqa: BLE001
        log.error("migration failed: %s", e)
        sys.exit(1)
    log.info("database is up to date")
