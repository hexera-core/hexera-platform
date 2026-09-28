# Responsibility: a synchronous measurement that outlives its ceiling is handed to a worker, not abandoned.
# Boundaries: the branch only. What the conversation says with no measurement is tests/unit/agents/.
"""MEASURED on the 36-file run of the night of 2026-09-27. Three parts of 36 died on this branch -
blade_row_rotor_001, shell_and_tube_7, shell_and_tube_7_unshared - and those three were the only stalls in
the batch. `shell_and_tube_7` had measured successfully at 08:57 the same day, so the ceiling was racing the
machine's load rather than rejecting a part we cannot read.

The size threshold in front of this path is what made it invisible: 44 corpus files measure at a median of
1.11s and a p90 of 3.91s, so a file small enough for this path was assumed to be a file that measures in
seconds. These three are small enough and are not, and the assumption had no fallback behind it - one
60-second attempt on an HTTP request, then nothing, while the worker beside it has 900 seconds and no other
work.

A TIMEOUT IS THE ONE FAILURE WHERE MORE TIME IS THE WHOLE REMEDY. Every other failure here fails the same
way on a worker, so only this one is retried, and the customer waits no longer either way: the request still
returns at the ceiling.
"""
from __future__ import annotations

import asyncio

import pytest

from meshpipeline.application import geometry_measurement as gm


@pytest.fixture
def small_enough(monkeypatch):
    """A file under the synchronous threshold, so the branch under test is the one that runs."""
    monkeypatch.setattr(gm.polcfg, "GEOMETRY_MEASUREMENT_SYNC_MAX_MB", 4.0, raising=False)


def _raising(exc):
    """A stand-in for `asyncio.wait_for` that fails the way we want to test.

    It CLOSES the coroutine it was handed before raising. `on_upload` builds `asyncio.to_thread(...)` as the
    argument, so a fake that simply raises leaves it un-awaited and every test here emits a RuntimeWarning
    about it - noise that would sit in the suite forever and read as a defect in the code under test.
    """
    async def _fake(coro=None, *a, **k):
        if hasattr(coro, "close"):
            coro.close()
        raise exc
    return _fake


def _run(**kw):
    return asyncio.run(gm.on_upload("11111111-1111-1111-1111-111111111111", "owner-1",
                                    size_bytes=1024, **kw))


def test_a_measurement_that_outlives_the_ceiling_is_handed_to_a_worker(small_enough, monkeypatch):
    handed: list[tuple] = []

    monkeypatch.setattr(gm.asyncio, "wait_for", _raising(TimeoutError()))
    monkeypatch.setattr(gm, "enqueue_measurement",
                        lambda sid, owner, *, purpose: handed.append((sid, owner, purpose)) or True)
    assert _run() == "queued", "it used to return 'skipped' and enqueue nothing"
    assert len(handed) == 1, "exactly one worker takes it, not none and not two"
    assert handed[0][1] == "owner-1"
    assert handed[0][2] == gm.DEFAULT_PURPOSE, "the worker measures it for the same purpose"


def test_the_purpose_the_caller_asked_for_travels_to_the_worker(small_enough, monkeypatch):
    """Two fields of a measurement depend on the purpose, so a retry for a different one is a different
    measurement and would be a quiet substitution."""
    handed: list[tuple] = []

    monkeypatch.setattr(gm.asyncio, "wait_for", _raising(TimeoutError()))
    monkeypatch.setattr(gm, "enqueue_measurement",
                        lambda sid, owner, *, purpose: handed.append(purpose) or True)
    assert _run(purpose="external_cfd") == "queued"
    assert handed == ["external_cfd"]


def test_no_worker_available_is_still_skipped_and_never_an_exception(small_enough, monkeypatch):
    """The fail-open is unchanged where it belongs: a broker that will not take the task is an upload that
    still succeeds with no measurement behind it."""
    monkeypatch.setattr(gm.asyncio, "wait_for", _raising(TimeoutError()))
    monkeypatch.setattr(gm, "enqueue_measurement", lambda *a, **k: False)
    assert _run() == "skipped"


def test_a_failure_that_is_not_a_timeout_is_not_retried(small_enough, monkeypatch):
    """A broken STEP fails the same way on a worker, so retrying it spends a worker to reach the same row.
    Only the timeout is retried, and this is the test that keeps the branch narrow."""
    handed: list = []

    monkeypatch.setattr(gm.asyncio, "wait_for", _raising(RuntimeError("the file is not readable")))
    monkeypatch.setattr(gm, "enqueue_measurement", lambda *a, **k: handed.append(1) or True)
    assert _run() == "skipped"
    assert handed == [], "a non-timeout failure must not spend a worker"


def test_a_file_too_large_for_the_synchronous_path_still_goes_straight_to_a_worker(monkeypatch):
    """Unchanged, and asserted here so the new branch cannot be mistaken for this one."""
    monkeypatch.setattr(gm.polcfg, "GEOMETRY_MEASUREMENT_SYNC_MAX_MB", 0.0, raising=False)
    monkeypatch.setattr(gm, "enqueue_measurement", lambda *a, **k: True)
    assert asyncio.run(gm.on_upload("22222222-2222-2222-2222-222222222222", "owner-2",
                                    size_bytes=99 * 1024 * 1024)) == "queued"
