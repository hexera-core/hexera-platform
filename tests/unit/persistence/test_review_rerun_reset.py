# Responsibility: Verify a parked job's re-run claims its own row cleanly, gets its own deadline,
#                 and is charged for its two execution spans only - never for the parked wait.
# Boundaries: the real claim and the real price over one row; no database.
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import meshpipeline.settings.policy as polcfg
from meshpipeline.application.metering_service import credits_for_job
from meshpipeline.persistence.lease import ClaimResult, LeaseRepository
from meshpipeline.persistence.models import JobStatus
from meshpipeline.persistence.repositories.job_repository import review_rerun_reset

T0 = datetime(2026, 10, 17, 9, 0, tzinfo=UTC)
FIRST_SPAN = timedelta(minutes=20)
SIX_HOURS = 6 * 3600


class _Result:
    def __init__(self, row): self._row = row
    def scalar_one_or_none(self): return self._row


def _parked_row():
    """A job exactly as its park left it: the first execution's lease, id and deadline still on it."""
    return SimpleNamespace(
        id=uuid.uuid4(), status=JobStatus.pending_review, execution_generation=1,
        execution_claim_epoch=1, active_worker_token=uuid.uuid4(),
        lease_expires_at=T0 + FIRST_SPAN + timedelta(minutes=15),
        pipeline_execution_id="proc-worker-1", owning_backend="celery",
        started_at=T0, ended_at=T0 + FIRST_SPAN,
        pipeline_deadline_at=T0 + timedelta(seconds=SIX_HOURS),
        lease_acquired_at=T0, lease_heartbeat_at=T0 + FIRST_SPAN)


def _run_it(row, now):
    """The run route's transaction: the status CAS, then the reset beside it."""
    row.status = JobStatus.pending
    for key, value in review_rerun_reset(started_at=row.started_at, ended_at=row.ended_at,
                                         now=now).items():
        setattr(row, key, value)


async def _claim(row, now):
    db = SimpleNamespace(execute=AsyncMock(return_value=_Result(row)), flush=AsyncMock())
    # the same worker process that ran the first execution: the same execution id
    return await LeaseRepository().claim_execution(
        db, row.id, worker_token=uuid.uuid4(), backend="celery",
        backend_execution_id="proc-worker-1", now=now, lease_seconds=900,
        pipeline_total_seconds=SIX_HOURS)


async def test_without_the_reset_an_immediate_rerun_collides_with_the_parks_own_lease():
    row = _parked_row()
    row.status = JobStatus.pending                         # the CAS alone, as it used to be
    result, _ = await _claim(row, T0 + FIRST_SPAN + timedelta(minutes=1))
    assert result is ClaimResult.active_lease_conflict


async def test_a_rerun_claims_cleanly_immediately_after_run_it():
    row = _parked_row()
    now = T0 + FIRST_SPAN + timedelta(minutes=1)           # answered inside the lease window
    _run_it(row, now)
    result, own = await _claim(row, now)
    assert result is ClaimResult.acquired_new_generation, result
    # a NEW generation, so a fresh checkpoint thread - never the finished one of the parked run
    assert own.execution_generation == 2


async def test_a_rerun_after_the_original_deadline_still_has_its_own_budget():
    row = _parked_row()
    now = T0 + timedelta(hours=9)                          # three hours past the first deadline
    _run_it(row, now)
    result, own = await _claim(row, now)
    assert result is ClaimResult.acquired_new_generation
    assert own.pipeline_deadline_at == now + timedelta(seconds=SIX_HOURS)
    db = SimpleNamespace(execute=AsyncMock(return_value=_Result(row)), flush=AsyncMock())
    assert await LeaseRepository().heartbeat(db, own, now=now + timedelta(minutes=5),
                                             lease_seconds=900) is True


@pytest.mark.parametrize("waited", [timedelta(minutes=3), timedelta(hours=30)])
async def test_park_review_rerun_is_charged_for_the_two_execution_spans_only(monkeypatch, waited):
    monkeypatch.setattr(polcfg, "JOB_BASE_CREDITS", 5)
    monkeypatch.setattr(polcfg, "CREDITS_PER_MESH_MINUTE", 2)
    row = _parked_row()
    rerun_at = row.ended_at + waited
    _run_it(row, rerun_at)
    await _claim(row, rerun_at)
    second_span = timedelta(minutes=10)
    row.ended_at = rerun_at + second_span                  # the re-run's real ending
    charged = credits_for_job(started_at=row.started_at, ended_at=row.ended_at)
    assert charged == 5 + 2 * 30, "the parked wait was billed as mesh time"


def test_the_reset_leaves_no_release_mark_for_the_reaper():
    values = review_rerun_reset(started_at=T0, ended_at=T0 + FIRST_SPAN,
                                now=T0 + timedelta(hours=1))
    assert values["lease_expires_at"] is None and values["active_worker_token"] is None
    assert values["pipeline_deadline_at"] is None and values["pipeline_execution_id"] is None
    assert values["ended_at"] is None
