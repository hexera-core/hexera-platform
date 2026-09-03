# Nothing may extend execution ownership past the whole-pipeline deadline - neither the
# periodic heartbeat nor the publish-seam fence recovery (which reuses heartbeat as its one
# safe primitive). A worker wedged past the deadline is a zombie; renewal would let it hold
# the job forever.
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from meshpipeline.persistence.lease import ExecutionOwnership, LeaseRepository


class _Result:
    def __init__(self, row):
        self._row = row

    def scalar_one_or_none(self):
        return self._row


def _fixture(deadline_delta_s: int):
    now = datetime.now(UTC)
    token = uuid.uuid4()
    job = uuid.uuid4()
    row = SimpleNamespace(
        id=job, status="running", execution_generation=1, active_worker_token=token,
        lease_heartbeat_at=now, lease_expires_at=now + timedelta(seconds=900),
        pipeline_deadline_at=now + timedelta(seconds=deadline_delta_s))
    own = ExecutionOwnership(job_id=job, execution_generation=1, worker_token=token,
                             backend="celery", pipeline_deadline_at=row.pipeline_deadline_at)
    db = SimpleNamespace(execute=AsyncMock(return_value=_Result(row)), flush=AsyncMock())
    return db, own, row


@pytest.mark.asyncio
async def test_heartbeat_refuses_past_the_pipeline_deadline():
    db, own, row = _fixture(deadline_delta_s=-60)
    assert await LeaseRepository().heartbeat(db, own) is False
    db.flush.assert_not_awaited()            # refusal extends nothing


@pytest.mark.asyncio
async def test_heartbeat_still_renews_before_the_deadline():
    db, own, row = _fixture(deadline_delta_s=3600)
    before = row.lease_expires_at
    # the fence-mirror refresh hits Redis and is allowed to fail as a warning - safety is
    # the PostgreSQL row, and this test asserts exactly that row's renewal
    assert await LeaseRepository().heartbeat(db, own) is True
    assert row.lease_expires_at > before
    db.flush.assert_awaited()
