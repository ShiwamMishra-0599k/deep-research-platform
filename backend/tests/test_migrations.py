"""Alembic migrations against a real PostgreSQL: fresh install, legacy adoption, safety, drift."""
import uuid

import pytest
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import create_engine, inspect, text

from alembic import command
from app.migrate import _alembic_config, run_migrations
from tests.conftest import TEST_DB_URL, reset_database_schema


@pytest.fixture()
def fresh_schema():
    """Give the test an empty schema, and restore the fully migrated one afterwards."""
    reset_database_schema()
    yield
    reset_database_schema()
    run_migrations(TEST_DB_URL)


def _engine():
    return create_engine(TEST_DB_URL, isolation_level="AUTOCOMMIT")


def _upgrade_to(rev: str):
    eng = _engine()
    with eng.connect() as conn:
        command.upgrade(_alembic_config(conn), rev)
    eng.dispose()


def _version() -> str | None:
    eng = _engine()
    with eng.connect() as c:
        try:
            return c.execute(text("SELECT version_num FROM alembic_version")).scalar()
        except Exception:  # noqa: BLE001
            return None
        finally:
            eng.dispose()


def _seed_legacy_data(eng):
    uid, cid = str(uuid.uuid4()), str(uuid.uuid4())
    with eng.connect() as c:
        c.execute(text("INSERT INTO users (id,email,password_hash,name,created_at) VALUES (:i,'old@example.com','$2b$12$hash','Old',now())"), {"i": uid})
        c.execute(text("INSERT INTO conversations (id,user_id,title,created_at,updated_at) VALUES (:c,:u,'Legacy chat',now(),now())"), {"c": cid, "u": uid})
        c.execute(text("INSERT INTO messages (id,conversation_id,role,content,blocks,created_at) VALUES (:m,:c,'USER','legacy question','[{\"template_id\":\"markdown\"}]',now())"), {"m": str(uuid.uuid4()), "c": cid})
    return uid, cid


def test_fresh_database_migrates_to_head_with_expected_schema(fresh_schema):
    run_migrations(TEST_DB_URL)
    assert _version() == "0003"
    insp = inspect(_engine())
    assert {"users", "conversations", "messages", "alembic_version"} <= set(insp.get_table_names())
    msg_idx = {i["name"] for i in insp.get_indexes("messages")}
    assert {"ix_messages_conversation_id", "ix_messages_conversation_created"} <= msg_idx
    assert "ix_conversations_user_updated" in {i["name"] for i in insp.get_indexes("conversations")}
    assert "ck_messages_role" in {c["name"] for c in insp.get_check_constraints("messages")}
    assert any(i["unique"] and i["column_names"] == ["email"] for i in insp.get_indexes("users"))


def test_foreign_keys_cascade_on_delete(fresh_schema):
    run_migrations(TEST_DB_URL)
    eng = _engine()
    uid, cid = _seed_legacy_data(eng)
    with eng.connect() as c:
        c.execute(text("DELETE FROM users WHERE id=:i"), {"i": uid})
        assert c.execute(text("SELECT count(*) FROM conversations")).scalar() == 0
        assert c.execute(text("SELECT count(*) FROM messages")).scalar() == 0


def test_migrations_are_idempotent(fresh_schema):
    run_migrations(TEST_DB_URL)
    run_migrations(TEST_DB_URL)
    assert _version() == "0003"


def test_models_and_migrations_have_no_drift(fresh_schema):
    """If someone edits models.py without a migration, this fails."""
    run_migrations(TEST_DB_URL)
    from app.models import Base

    eng = _engine()
    with eng.connect() as conn:
        diff = compare_metadata(MigrationContext.configure(conn, opts={"compare_type": True}), Base.metadata)
    assert diff == [], f"schema drift: {diff}"


def test_legacy_create_all_database_is_adopted_without_data_loss(fresh_schema):
    """A DB from the old create_all startup hook (tables exist, no alembic_version) keeps all its data."""
    _upgrade_to("0001")                      # identical to what create_all produced
    eng = _engine()
    uid, cid = _seed_legacy_data(eng)
    with eng.connect() as c:
        c.execute(text("DROP TABLE alembic_version"))   # simulate: Alembic never ran here
    assert _version() is None

    run_migrations(TEST_DB_URL)

    assert _version() == "0003"
    with eng.connect() as c:
        assert c.execute(text("SELECT email FROM users WHERE id=:i"), {"i": uid}).scalar() == "old@example.com"
        assert c.execute(text("SELECT title FROM conversations WHERE id=:c"), {"c": cid}).scalar() == "Legacy chat"
        assert c.execute(text("SELECT content FROM messages")).scalar() == "legacy question"
        assert c.execute(text("SELECT blocks->0->>'template_id' FROM messages")).scalar() == "markdown"


def test_partial_schema_is_refused_not_guessed(fresh_schema):
    eng = _engine()
    with eng.connect() as c:
        c.execute(text("CREATE TABLE users (id uuid primary key)"))
    with pytest.raises(RuntimeError, match="Partial schema"):
        run_migrations(TEST_DB_URL)
    with eng.connect() as c:  # nothing was altered
        assert c.execute(text("SELECT count(*) FROM information_schema.tables WHERE table_name IN ('conversations','messages')")).scalar() == 0


def test_failed_migration_rolls_back_and_keeps_data(fresh_schema):
    """0002 adds a CHECK; existing bad data must make it fail cleanly, not corrupt anything."""
    _upgrade_to("0001")
    eng = _engine()
    uid, cid = _seed_legacy_data(eng)
    with eng.connect() as c:
        c.execute(text("INSERT INTO messages (id,conversation_id,role,content,created_at) VALUES (:m,:c,'SYSTEM','odd',now())"), {"m": str(uuid.uuid4()), "c": cid})

    with pytest.raises(Exception):
        run_migrations(TEST_DB_URL)

    assert _version() == "0001"                       # still at the old revision
    with eng.connect() as c:
        assert c.execute(text("SELECT count(*) FROM messages")).scalar() == 2   # data intact
        idx = {r[0] for r in c.execute(text("SELECT indexname FROM pg_indexes WHERE tablename='messages'"))}
        assert "ix_messages_conversation_created" not in idx                      # DDL rolled back too


def test_downgrade_to_base_and_back(fresh_schema):
    run_migrations(TEST_DB_URL)
    eng = _engine()
    with eng.connect() as conn:
        cfg = _alembic_config(conn)
        command.downgrade(cfg, "0001")
        assert "ck_messages_role" not in {c["name"] for c in inspect(conn).get_check_constraints("messages")}
        command.downgrade(cfg, "base")
        assert "users" not in inspect(conn).get_table_names()
    run_migrations(TEST_DB_URL)
    assert _version() == "0003"


def test_concurrent_migration_runs_are_serialised(fresh_schema):
    import threading

    errors: list[Exception] = []

    def go():
        try:
            run_migrations(TEST_DB_URL)
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    ts = [threading.Thread(target=go) for _ in range(4)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert errors == [] and _version() == "0003"


def test_0003_creates_and_drops_only_the_sessions_table(fresh_schema):
    run_migrations(TEST_DB_URL)
    insp = inspect(_engine())
    assert "auth_sessions" in insp.get_table_names()
    cols = {c["name"] for c in insp.get_columns("auth_sessions")}
    assert {"id", "user_id", "refresh_hash", "prev_refresh_hash", "idle_expires_at", "absolute_expires_at",
            "revoked_at", "revoked_reason", "ip_hash", "user_agent"} <= cols
    assert "uq_auth_sessions_refresh_hash" in {u["name"] for u in insp.get_unique_constraints("auth_sessions")}
    assert any(fk["referred_table"] == "users" and fk["options"].get("ondelete") == "CASCADE" for fk in insp.get_foreign_keys("auth_sessions"))
    eng = _engine()
    uid, cid = _seed_legacy_data(eng)
    with eng.connect() as conn:
        command.downgrade(_alembic_config(conn), "0002")
        t = set(inspect(conn).get_table_names())
        assert "auth_sessions" not in t and {"users", "conversations", "messages"} <= t   # user data untouched
        assert conn.execute(text("SELECT count(*) FROM messages")).scalar() == 1
    run_migrations(TEST_DB_URL)
    assert _version() == "0003"


def test_legacy_database_at_0002_upgrades_to_0003_keeping_data(fresh_schema):
    _upgrade_to("0002")
    eng = _engine()
    uid, cid = _seed_legacy_data(eng)
    run_migrations(TEST_DB_URL)
    assert _version() == "0003"
    with eng.connect() as c:
        assert c.execute(text("SELECT email FROM users WHERE id=:i"), {"i": uid}).scalar() == "old@example.com"
        assert c.execute(text("SELECT count(*) FROM auth_sessions")).scalar() == 0
