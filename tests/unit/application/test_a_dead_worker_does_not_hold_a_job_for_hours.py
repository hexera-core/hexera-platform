# Responsibility: A running job whose LEASE has expired is stalled, and one that was never leased is not.
# Boundaries: the reaper's condition only; nothing here runs a job or touches a worker.
from __future__ import annotations

from meshpipeline.application.maintenance.cleanup import _stalled


def _sql() -> str:
    from datetime import UTC, datetime

    from sqlalchemy.dialects import postgresql

    return str(_stalled(datetime.now(UTC)).compile(
        dialect=postgresql.dialect(), compile_kwargs={"literal_binds": False}))


def test_an_expired_lease_is_enough_to_reap_without_waiting_for_the_hours():
    # The lease is heartbeated well inside WORKER_LEASE_SECONDS, so an expired one means no worker
    # holds the job - known in about fifteen minutes. The reaper used to wait
    # STALLED_JOB_TIMEOUT_HOURS on top, and five jobs killed by a redeploy filled
    # MAX_JOBS_PER_OWNER and refused every upload for hours while it reported "reaped 0".
    sql = _sql()
    assert "lease_expires_at" in sql, sql


def test_a_job_that_was_never_leased_is_not_read_as_expired():
    # The half that must keep working. A null lease is a job nothing ever took, not a dead one, and
    # reaping those would fail the innocent - which is worse than the wait this replaces.
    sql = _sql()
    assert "lease_expires_at IS NOT NULL" in sql, sql


def test_the_two_older_reasons_still_stand():
    # An expired lease is an ADDITION. A crashed worker that never leased, and a job never picked
    # up at all, are still reaped on the hours-based cutoff.
    sql = _sql()
    assert "started_at" in sql and "created_at" in sql, sql
