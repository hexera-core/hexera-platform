# The loop-crossing bug: asyncpg binds pooled connections to the loop that opened them, so a
# process-global engine cached across celery task loops handed task N+1 connections whose
# futures belonged to task N's closed loop ("connection is closed" mid-job). The cache is now
# keyed by the loop object; these tests pin that contract.
import asyncio

import pytest

import meshpipeline.persistence.session as sess


@pytest.fixture(autouse=True)
def _clean_slate():
    sess.reset_session_state()
    yield
    sess.reset_session_state()


def test_second_loop_gets_its_own_live_engine():
    # create_async_engine connects lazily - no database is touched
    async def grab():
        return sess._get_engine()

    e1 = asyncio.run(grab())
    e2 = asyncio.run(grab())
    assert e1 is not e2


def test_same_loop_reuses_one_engine():
    async def grab_twice():
        return sess._get_engine(), sess._get_engine()

    e1, e2 = asyncio.run(grab_twice())
    assert e1 is e2


def test_closed_loop_entry_is_never_served():
    # Keep the loop OBJECT alive after closing it, so a WeakKeyDictionary entry for it
    # survives; a fresh loop must still get its own engine and the dead entry is swept.
    loop = asyncio.new_event_loop()
    try:
        e1 = loop.run_until_complete(_grab())
    finally:
        loop.close()
    assert loop.is_closed()

    e2 = asyncio.run(_grab())
    assert e2 is not e1
    with sess._engines_lock:
        assert loop not in sess._engines     # the dead entry was swept, exactly once


async def _grab():
    return sess._get_engine()


def test_no_running_loop_is_a_loud_error():
    # A sync caller with no running loop gets the RuntimeError, never a fallback engine - a
    # cross-loop engine handed out silently is exactly the bug this module now prevents.
    with pytest.raises(RuntimeError):
        sess._get_engine()


def test_session_factory_is_bound_to_the_loop_engine():
    async def grab():
        return sess._get_engine(), sess._get_session_factory()

    e1, f1 = asyncio.run(grab())
    e2, f2 = asyncio.run(grab())
    assert f1.kw["bind"] is e1 and f2.kw["bind"] is e2 and f1 is not f2


def test_dispose_engine_clears_only_the_current_loop_entry():
    async def grab_and_dispose():
        engine = sess._get_engine()
        await sess.dispose_engine()
        return engine

    e1 = asyncio.run(grab_and_dispose())

    async def grab():
        return sess._get_engine()

    e2 = asyncio.run(grab())
    assert e1 is not e2
