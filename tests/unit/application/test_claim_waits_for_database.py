"""A database that will not take the claim yet (full, starting) is waited out, not a failed job.

dev-areen 25 Oct 00:06 UTC: a redeploy filled every Postgres connection slot and two jobs failed in
0.1 s as `internal` on asyncpg's TooManyConnectionsError, before they had done anything.
"""
from __future__ import annotations

import asyncio
import logging

import pytest

from meshpipeline.application import execution_fence as fence
from meshpipeline.errors import FailureClass, classify_exception


class TooManyConnectionsError(Exception):
    """Stands in for asyncpg.exceptions.TooManyConnectionsError (matched by name)."""


class CannotConnectNowError(Exception):
    pass


@pytest.mark.parametrize("exc", [
    TooManyConnectionsError('remaining connection slots are reserved for roles with privileges of '
                            'the "pg_use_reserved_connections" role'),
    CannotConnectNowError("the database system is starting up"),
    RuntimeError("FATAL:  sorry, too many clients already"),
    RuntimeError('connection failed: FATAL:  remaining connection slots are reserved for roles'),
])
def test_a_full_or_starting_database_is_dependency_down_not_internal(exc):
    assert classify_exception(exc, "postgres").failure_class is FailureClass.DEPENDENCY_DOWN


def test_an_ordinary_bug_is_still_internal():
    assert classify_exception(KeyError("x"), "pipeline").failure_class is FailureClass.INTERNAL


def _run(coro):
    return asyncio.run(coro)


class _Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


def _patient(monkeypatch, outcomes, *, total_s=None):
    calls, slept = [], []
    clock = _Clock()

    async def fake_claim(*_a, **_k):
        calls.append(1)
        o = outcomes.pop(0)
        if isinstance(o, BaseException):
            raise o
        return o

    async def fake_sleep(s):
        slept.append(s)
        clock.t += s

    monkeypatch.setattr(fence, "claim_delivery", fake_claim)
    result = fence.claim_delivery_patiently(
        None, None, "job-1", jlog=logging.getLogger("t"), backend="celery",
        backend_execution_id="x", sleep=fake_sleep, clock=clock, total_s=total_s)
    return result, calls, slept


def test_the_claim_waits_out_a_full_database_and_then_runs(monkeypatch):
    ok = object()
    full = TooManyConnectionsError("remaining connection slots are reserved")
    coro, calls, slept = _patient(monkeypatch, [full, full, full, ok])
    assert _run(coro) is ok
    assert len(calls) == 4
    assert slept == [5.0, 10.0, 20.0]          # growing wait, never a failed job


def test_the_wait_steps_are_capped(monkeypatch):
    ok = object()
    full = TooManyConnectionsError("full")
    coro, _calls, slept = _patient(monkeypatch, [full] * 6 + [ok])
    assert _run(coro) is ok
    assert slept == [5.0, 10.0, 20.0, 40.0, 60.0, 60.0]


def test_past_the_window_the_error_stands(monkeypatch):
    full = TooManyConnectionsError("full")
    coro, calls, slept = _patient(monkeypatch, [full] * 50, total_s=100.0)
    with pytest.raises(TooManyConnectionsError):
        _run(coro)
    assert sum(slept) <= 100.0
    assert len(calls) == len(slept) + 1


def test_a_real_bug_is_not_retried(monkeypatch):
    coro, calls, slept = _patient(monkeypatch, [KeyError("bug")])
    with pytest.raises(KeyError):
        _run(coro)
    assert calls == [1] and slept == []


def test_the_run_claims_through_the_patient_path():
    import inspect

    from meshpipeline.application import pipeline_run
    src = inspect.getsource(pipeline_run._run_async)
    assert "claim_delivery_patiently(" in src
    # the delivery counter is taken ONCE per delivery, before the claim - never inside the wait
    assert "_incr_delivery_count" not in inspect.getsource(fence.claim_delivery_patiently)
