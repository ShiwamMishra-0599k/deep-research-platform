import asyncio
import weakref
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import anyio
import anyio.to_thread
from fastapi.concurrency import run_in_threadpool
from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from .config import settings

POOL_SIZE = 10
MAX_OVERFLOW = 10
# Requests allowed inside a database phase at once. Deliberately BELOW the pool (20), leaving headroom for the few ungated users
# (readiness probe, the cleanup job). Because no more than this many requests can need a connection at the same time, a request
# never has to WAIT for one: the hold-and-wait deadlock described under get_db cannot form, and overload becomes a fast, clean 503.
DB_GATE_SLOTS = POOL_SIZE + MAX_OVERFLOW - 4

engine = create_engine(
    settings.database_url,
    pool_pre_ping=True,
    pool_size=POOL_SIZE,
    max_overflow=MAX_OVERFLOW,
    pool_recycle=1800,
    future=True,
    # Fail fast instead of hanging when PostgreSQL is unreachable or a query runs away.
    connect_args={"connect_timeout": 5, "options": "-c statement_timeout=15000"},
)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


class DbBusy(Exception):
    """No database slot became free in time: the service is overloaded. Mapped to 503 + Retry-After."""


_gates: "weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Semaphore]" = weakref.WeakKeyDictionary()


def _gate() -> asyncio.Semaphore:
    loop = asyncio.get_running_loop()      # one semaphore per event loop (the app has one; tests create several)
    g = _gates.get(loop)
    if g is None:
        g = _gates[loop] = asyncio.Semaphore(DB_GATE_SLOTS)
    return g


@asynccontextmanager
async def db_gate(wait: float | None = None):
    """Admission to a database phase. Waits asynchronously (holding no thread and no connection) for a free slot."""
    g = _gate()
    try:
        await asyncio.wait_for(g.acquire(), settings.db_gate_wait_seconds if wait is None else wait)
    except TimeoutError:
        raise DbBusy from None
    try:
        yield
    finally:
        g.release()


async def db_call(fn, *args, wait: float | None = None):
    """Run a blocking function that uses SessionLocal in a worker thread, inside the gate."""
    async with db_gate(wait):
        return await run_in_threadpool(fn, *args)


_close_limiter = None


def _limiter():
    """A thread limiter used ONLY to close sessions, created on first use (it needs a running event loop)."""
    global _close_limiter
    if _close_limiter is None:
        _close_limiter = anyio.CapacityLimiter(8)
    return _close_limiter


async def get_db() -> AsyncIterator[Session]:
    """Request-scoped session for sync handlers (which run in worker threads).

    A request holds a gate slot for the whole life of its session, so at most DB_GATE_SLOTS sessions exist at once. Without that,
    under a burst every one of the 40 worker threads could be blocked waiting for a pooled connection while the requests that HOLD
    connections waited for a thread (to run the handler's return, response serialisation, or the close): a hold-and-wait deadlock
    that ended only when the 30 s pool timeout fired and requests failed with 500. Found by perf/load_test.py at 100 concurrent
    requests; see tests/test_db_session_release.py. The close itself runs on a dedicated limiter so releasing never queues behind
    request threads either.
    """
    async with db_gate():
        db = SessionLocal()
        try:
            yield db
        finally:
            await anyio.to_thread.run_sync(db.close, limiter=_limiter())
