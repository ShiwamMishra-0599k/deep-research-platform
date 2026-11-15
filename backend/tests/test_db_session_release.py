"""Releasing a database session must never queue behind request threads (hold-and-wait deadlock under burst load)."""
import asyncio

import anyio
import anyio.to_thread

from app import db as dbmod


def test_session_is_closed_even_when_every_default_worker_thread_is_busy():
    async def go():
        default = anyio.to_thread.current_default_thread_limiter()
        borrowers = []
        while True:                                    # exhaust the pool that runs request handlers
            b = object()
            try:
                default.acquire_on_behalf_of_nowait(b)
                borrowers.append(b)
            except anyio.WouldBlock:
                break
        taken = len(borrowers)
        try:
            gen = dbmod.get_db()
            session = await gen.__anext__()
            closed = []
            real_close = session.close
            session.close = lambda: (closed.append(1), real_close())[1]
            try:
                await asyncio.wait_for(gen.__anext__(), timeout=3)   # runs the teardown
            except StopAsyncIteration:
                pass
            return closed, taken
        finally:
            for b in borrowers:
                default.release_on_behalf_of(b)

    closed, taken = asyncio.run(go())
    assert taken >= 1 and closed == [1]


def test_get_db_yields_a_working_session_and_closes_it(engine_rows):
    from sqlalchemy import text

    async def go():
        gen = dbmod.get_db()
        s = await gen.__anext__()
        assert s.execute(text("select 1")).scalar() == 1
        assert s.in_transaction()
        await gen.aclose()
        return s

    s = asyncio.run(go())
    assert not s.in_transaction()                      # closed: its connection went back to the pool
