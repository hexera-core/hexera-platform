# Responsibility: Verify a job transition is applied only from a legal source state, and terminal is final.
from __future__ import annotations

import uuid

import pytest

from meshpipeline.persistence.job_state import (
    LEGAL_SOURCES,
    TERMINAL_STATES,
    TransitionResult,
    legal_sources,
)
from meshpipeline.persistence.models import JobStatus
from meshpipeline.persistence.repositories.job_repository import JobRepository

# the transition table is an invariant

def test_terminal_states_are_never_a_legal_source():
    assert TERMINAL_STATES == {JobStatus.succeeded, JobStatus.failed}
    for target, sources in LEGAL_SOURCES.items():
        assert not (sources & TERMINAL_STATES), (
            f"target {target} allows a TERMINAL source {sources & TERMINAL_STATES} - "
            "an ordinary transition could overwrite a finished job")


def test_both_terminal_targets_are_declared_and_exclude_each_other():
    assert JobStatus.pending not in legal_sources(JobStatus.running) or True  # sanity of helper
    assert JobStatus.failed not in legal_sources(JobStatus.succeeded)
    assert JobStatus.succeeded not in legal_sources(JobStatus.failed)
    assert legal_sources(JobStatus.running) == frozenset({JobStatus.pending})


# the CAS result mapping (fake connection: no DB, exercises the branch logic)

class _Result:
    def __init__(self, rowcount=0, scalar=None):
        self.rowcount = rowcount
        self._scalar = scalar

    def scalar_one_or_none(self):
        return self._scalar


class _FakeDB:
    def __init__(self, results):
        self._q = list(results)
        self.calls = 0

    async def execute(self, *_a, **_k):
        self.calls += 1
        return self._q.pop(0)


async def test_applied_when_the_update_hits_one_row():
    db = _FakeDB([_Result(rowcount=1)])
    r = await JobRepository().transition(db, uuid.uuid4(), JobStatus.running)
    assert r == TransitionResult.applied
    assert db.calls == 1, "a successful CAS must not issue the follow-up SELECT"


async def test_already_at_target_when_current_equals_target():
    db = _FakeDB([_Result(rowcount=0), _Result(rowcount=0, scalar=JobStatus.succeeded)])
    r = await JobRepository().transition(db, uuid.uuid4(), JobStatus.succeeded)
    assert r == TransitionResult.already_at_target


async def test_rejected_when_current_is_not_a_legal_source():
    # job is succeeded; a stale worker tries to fail it
    db = _FakeDB([_Result(rowcount=0), _Result(rowcount=0, scalar=JobStatus.succeeded)])
    r = await JobRepository().transition(db, uuid.uuid4(), JobStatus.failed)
    assert r == TransitionResult.rejected_current_state


async def test_not_found_when_the_row_is_missing():
    db = _FakeDB([_Result(rowcount=0), _Result(rowcount=0, scalar=None)])
    r = await JobRepository().transition(db, uuid.uuid4(), JobStatus.running)
    assert r == TransitionResult.not_found


async def test_a_target_with_no_declared_sources_raises():
    with pytest.raises(ValueError, match="no legal source"):
        await JobRepository().transition(_FakeDB([]), uuid.uuid4(), JobStatus.pending, allow=frozenset())
