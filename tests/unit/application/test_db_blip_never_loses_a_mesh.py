# Responsibility: Verify a database blip costs a retry, never a finished mesh: transient database
# errors are recognised, checkpoint writes and reads are retried on a fresh connection and - if the
# database stays away - skipped so the run finishes in memory, a fence refusal is never retried or
# skipped, and the one terminal commit is retried too.
# Boundaries: a stood-in saver, session and commit; no database (the integration tier kills a real
# checkpointer connection: tests/integration/test_checkpointer_survives_db_restart.py).
from __future__ import annotations

import asyncio

import pytest

from meshpipeline.application import db_retry


class OperationalError(Exception):
    """Named like psycopg's."""


@pytest.fixture(autouse=True)
def _no_waiting(monkeypatch):
    monkeypatch.setattr(db_retry, "BACKOFF_S", (0.0, 0.0, 0.0))


@pytest.mark.parametrize("exc,transient", [
    (OperationalError("the connection is closed"), True),
    (RuntimeError("server closed the connection unexpectedly"), True),
    (RuntimeError("FATAL: remaining connection slots are reserved"), True),
    (ValueError("invalid input syntax"), False),
    (KeyError("x"), False),
])
def test_what_counts_as_a_database_blip(exc, transient):
    assert db_retry.is_transient_db_error(exc) is transient


def test_a_wrapped_driver_error_counts_too():
    outer = RuntimeError("DBAPIError")
    outer.orig = OperationalError("the connection is closed")
    assert db_retry.is_transient_db_error(outer)


def _flaky(fails: int, exc: BaseException, value="ok"):
    calls = {"n": 0}

    async def step():
        calls["n"] += 1
        if calls["n"] <= fails:
            raise exc
        return value
    return step, calls


def test_a_blip_is_retried_until_it_passes():
    step, calls = _flaky(2, OperationalError("the connection is closed"))
    assert asyncio.run(db_retry.retry_transient(step, what="t")) == "ok" and calls["n"] == 3


def test_a_real_error_is_raised_at_once_and_an_outage_after_the_retries():
    step, calls = _flaky(5, ValueError("bad sql"))
    with pytest.raises(ValueError):
        asyncio.run(db_retry.retry_transient(step, what="t"))
    assert calls["n"] == 1
    step, calls = _flaky(99, OperationalError("the connection is closed"))
    with pytest.raises(OperationalError):
        asyncio.run(db_retry.retry_transient(step, what="t"))
    assert calls["n"] == 1 + len(db_retry.BACKOFF_S)


# THE CHECKPOINTER

class _Inner:
    serde = None
    config_specs: list = []

    def __init__(self, fails: int, exc: BaseException | None = None):
        self.fails, self.exc, self.calls = fails, exc or OperationalError("the connection is closed"), []

    async def _maybe_fail(self, what):
        self.calls.append(what)
        if len([c for c in self.calls if c == what]) <= self.fails:
            raise self.exc

    async def aput(self, config, checkpoint, metadata, new_versions):
        await self._maybe_fail("aput")
        return {"configurable": {**config["configurable"], "checkpoint_id": checkpoint["id"]}, "saved": True}

    async def aput_writes(self, config, writes, task_id, task_path=""):
        await self._maybe_fail("aput_writes")
        return "written"

    async def aget_tuple(self, config):
        await self._maybe_fail("aget_tuple")
        return "tuple"


def _fenced(inner):
    from meshpipeline.application.fenced_checkpointer import FencedCheckpointer
    return FencedCheckpointer(inner, job_id="job-kart", execution_generation=1)


_CONFIG = {"configurable": {"thread_id": "job-kart:s15:g1", "checkpoint_ns": ""}}


def test_a_checkpoint_write_that_meets_a_dead_connection_is_written_on_retry():
    inner = _Inner(fails=2)
    out = asyncio.run(_fenced(inner).aput(_CONFIG, {"id": "cp-9"}, {}, {}))
    assert out["saved"] and inner.calls.count("aput") == 3
    assert asyncio.run(_fenced(_Inner(fails=1)).aput_writes(_CONFIG, [], "t1")) == "written"
    assert asyncio.run(_fenced(_Inner(fails=1)).aget_tuple(_CONFIG)) == "tuple"


def test_a_database_that_stays_away_skips_the_checkpoint_and_the_run_goes_on():
    """The kart_chassis case: the write after a reviewer PASS must not fail the job."""
    inner = _Inner(fails=99)
    out = asyncio.run(_fenced(inner).aput(_CONFIG, {"id": "cp-9"}, {}, {}))
    assert out == {"configurable": {"thread_id": "job-kart:s15:g1", "checkpoint_ns": "", "checkpoint_id": "cp-9"}}
    assert asyncio.run(_fenced(_Inner(fails=99)).aput_writes(_CONFIG, [], "t1")) is None


def test_a_real_error_in_a_checkpoint_write_is_never_skipped():
    with pytest.raises(ValueError):
        asyncio.run(_fenced(_Inner(fails=1, exc=ValueError("bad"))).aput(_CONFIG, {"id": "x"}, {}, {}))


def test_a_fence_refusal_is_never_retried_or_skipped(monkeypatch):
    from meshpipeline.application import execution_fence

    class Fenced(Exception):
        pass

    async def _refuse(where, **_k):
        raise Fenced(where)

    monkeypatch.setattr(execution_fence, "assert_current_owner", _refuse)
    inner = _Inner(fails=0)
    with pytest.raises(Fenced):
        asyncio.run(_fenced(inner).aput(_CONFIG, {"id": "x"}, {}, {}))
    assert inner.calls == []


# THE TERMINAL COMMIT

def test_the_terminal_commit_is_retried_through_a_blip(monkeypatch):
    from types import SimpleNamespace

    from meshpipeline.application import terminal_finalize as tf

    tries = {"n": 0}

    async def _atomic(db, **_k):
        tries["n"] += 1
        if tries["n"] == 1:
            raise OperationalError("server closed the connection unexpectedly")
        return SimpleNamespace(fenced=True, fell_back_to_failed=False)

    class _Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def commit(self):
            return None

    async def _types(*a, **k):
        return []

    monkeypatch.setattr(tf, "finalize_terminal_atomic", _atomic)
    monkeypatch.setattr(tf, "read_delivered_types", _types)
    monkeypatch.setattr(tf, "build_terminal_result",
                        lambda *a, **k: SimpleNamespace(result=SimpleNamespace(to_dict=lambda: {}),
                                                        closing_message="done"))
    assembly = SimpleNamespace(job_id="job-kart", status="succeeded", failed_reason=None, review_plan=None)
    pub = asyncio.run(tf.assemble_and_finalize(lambda: _Session(), assembly, ownership=None, lease_repo=None,
                                               job_repo=None, jlog=SimpleNamespace(warning=lambda *a, **k: None)))
    assert tries["n"] == 2 and pub.fenced
