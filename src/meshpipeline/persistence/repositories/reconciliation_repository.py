# Responsibility: Track artifacts that could not be recorded, so they can be resolved later.
# Boundaries: bookkeeping for the reconciliation sweep.
from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, or_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from meshpipeline.persistence.models import (
    ArtifactReconciliation,
    ArtifactType,
    ReconciliationState,
)
from meshpipeline.persistence.repositories import tenant_scope


class ReconciliationRepository:

    async def record_orphan(self, db: AsyncSession, *, owner_id: str, job_id: uuid.UUID,
                            delivery_attempt: int, logical_key: str, artifact_type: ArtifactType,
                            object_key: str, object_checksum: str | None, object_size: int,
                            failure_category: str = "row_write_failed",
                            detail: str | None = None, organization_id: str = "") -> None:
        # Written from the pipeline's own reconciliation path (pipeline_run._persist_orphans),
        # never from a request holding a Principal - organization_id defaults to "" there, exactly
        # as it does for every caller that predates the organisation.
        stmt = (
            pg_insert(ArtifactReconciliation)
            .values(id=uuid.uuid4(), job_id=job_id,
                    **tenant_scope.stamp(owner_id=owner_id, organization_id=organization_id),
                    delivery_attempt=delivery_attempt, logical_key=logical_key,
                    artifact_type=artifact_type, object_key=object_key,
                    object_checksum=object_checksum, object_size=object_size,
                    failure_category=failure_category, state=ReconciliationState.pending,
                    detail=detail)
            .on_conflict_do_nothing(constraint="uq_reconcile_job_attempt_object")
        )
        await db.execute(stmt)

    async def claim_pending(self, db: AsyncSession, *, limit: int = 50) -> list[ArtifactReconciliation]:
        # DUE WORK ONLY. `next_attempt_at IS NULL` is new work, immediately eligible; anything a
        # previous sweep rescheduled waits until its time. The comparison is made by the DATABASE
        # (`func.now()`), not by the worker process: several workers sweep concurrently, their
        # clocks are not the same clock, and the schedule has to survive a restart of any of them.
        #
        # SKIP LOCKED still does the mutual exclusion; the due-time filter only decides what is
        # offered. Ordering is (next_attempt_at, created_at, id) so two workers walking the same
        # backlog see the same deterministic order and the oldest due row goes first.
        rows = await db.execute(
            select(ArtifactReconciliation)
            .where(ArtifactReconciliation.state == ReconciliationState.pending,
                   or_(ArtifactReconciliation.next_attempt_at.is_(None),
                       ArtifactReconciliation.next_attempt_at <= func.now()))
            .order_by(ArtifactReconciliation.next_attempt_at.nulls_first(),
                      ArtifactReconciliation.created_at,
                      ArtifactReconciliation.id)
            .limit(limit)
            .with_for_update(skip_locked=True))
        return list(rows.scalars().all())

    async def resolve(self, db: AsyncSession, rec_id: uuid.UUID, *,
                      state: ReconciliationState, detail: str | None = None,
                      retry_delay_s: int | None = None) -> None:
        rec = await db.get(ArtifactReconciliation, rec_id)
        if rec is None:
            return
        rec.state = state
        rec.retry_count = rec.retry_count + 1
        rec.updated_at = datetime.now(UTC)
        if detail is not None:
            rec.detail = detail
        # A row left PENDING is a retryable failure, and it gets a due time so the next sweep does
        # not simply spend another of its attempts. A row that reached a terminal state keeps no
        # schedule: it is never claimed again, and a stale due time on a resolved row would only be
        # something for a future reader to misinterpret.
        if state is ReconciliationState.pending:
            import meshpipeline.settings.policy as polcfg
            delay = polcfg.RECONCILE_RETRY_DELAY_SECONDS if retry_delay_s is None else retry_delay_s
            rec.next_attempt_at = func.now() + timedelta(seconds=int(delay))
        else:
            rec.next_attempt_at = None

    async def get_by_job(self, db: AsyncSession, job_id: uuid.UUID) -> list[ArtifactReconciliation]:
        rows = await db.execute(
            select(ArtifactReconciliation).where(ArtifactReconciliation.job_id == job_id))
        return list(rows.scalars().all())
