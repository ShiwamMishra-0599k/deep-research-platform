"""Expired-session housekeeping.

    python -m app.cleanup            # one pass, for cron / a scheduled container task; exit 0 on success

and, unless SESSION_PURGE_INTERVAL_SECONDS=0, an in-process loop started by the API (see `main.lifespan`).

A row is deleted once its session ENDED more than SESSION_PURGE_RETENTION_DAYS ago, where "ended" is the earliest of the
absolute expiry, the idle expiry, and the revocation time. Live sessions are never touched. The retention period keeps recent
dead rows (reuse-detection evidence and `revoked_reason`) available for investigation.

Safe with several replicas: a PostgreSQL advisory lock lets only one pass run at a time (others skip), deletes are batched so no
long table lock is held, and the DELETE is idempotent anyway.
"""
import asyncio
import logging
import sys
from datetime import timedelta

from sqlalchemy import func, text

from .config import settings
from .db import SessionLocal, engine
from .models import AuthSession
from .sessions import now

log = logging.getLogger("lumen.cleanup")

_LOCK_ID = 727_274_002   # distinct from the migration lock
BATCH = 1000


def purge_ended_sessions(retention_days: int | None = None, batch: int = BATCH) -> int | None:
    """One purge pass. Returns the number of rows deleted, or None if another pass holds the lock."""
    days = settings.session_purge_retention_days if retention_days is None else retention_days
    cutoff = now() - timedelta(days=days)
    ended = func.least(
        AuthSession.absolute_expires_at, AuthSession.idle_expires_at, func.coalesce(AuthSession.revoked_at, AuthSession.absolute_expires_at)
    )
    total = 0
    # The advisory lock is session-level, so it must be taken and released on ONE dedicated connection that is held for the
    # whole pass. (Taking it on a pooled Session connection leaks it: after commit/rollback the Session may check out a
    # different connection for the unlock, leaving the lock held and every later pass skipped.)
    with engine.connect() as lock_conn:
        if not lock_conn.execute(text("SELECT pg_try_advisory_lock(:k)"), {"k": _LOCK_ID}).scalar():
            lock_conn.rollback()
            return None
        try:
            lock_conn.commit()    # end the implicit transaction; the session-level lock survives it
            while True:
                with SessionLocal() as db:
                    ids = [r[0] for r in db.query(AuthSession.id).filter(ended < cutoff).limit(batch).all()]
                    if not ids:
                        break
                    total += db.query(AuthSession).filter(AuthSession.id.in_(ids)).delete(synchronize_session=False)
                    db.commit()
                if len(ids) < batch:
                    break
        finally:
            lock_conn.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": _LOCK_ID})
            lock_conn.commit()
    return total


def run_once() -> int | None:
    n = purge_ended_sessions()
    if n is None:
        log.info("session purge skipped: another pass is running")
    else:
        log.info("session purge removed %d ended session row(s)", n)
    return n


async def purge_loop(interval_seconds: int) -> None:
    """Runs forever inside the API process; one failed pass is logged and retried next period, never fatal."""
    await asyncio.sleep(min(60, interval_seconds))   # do not compete with start-up traffic
    while True:
        try:
            await asyncio.to_thread(run_once)
        except Exception:   # noqa: BLE001 - housekeeping must never take the API down
            log.exception("session purge failed")
        await asyncio.sleep(interval_seconds)


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    try:
        run_once()
    except Exception:   # noqa: BLE001
        log.exception("session purge failed")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
