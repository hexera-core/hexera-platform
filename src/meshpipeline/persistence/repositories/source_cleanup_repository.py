# Responsibility: Own the durable cleanup intent for an uploaded source object, and its claim and resolution.
# Boundaries: storage for the intent; deciding what to delete is the maintenance sweep's, and the bytes are the store's.
from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from meshpipeline.persistence.models import ReconciliationState, SourceObjectCleanup
from meshpipeline.persistence.repositories import tenant_scope

#: How many times the sweep may fail to delete one object before it stops retrying and leaves the
#: record for a person. Bounded so retry metadata cannot grow without end.
MAX_CLEANUP_RETRIES = 5


class SourceCleanupRepository:

    async def record_intent(self, db: AsyncSession, *, owner_id: str, source_id: uuid.UUID,
                            object_key: str, organization_id: str = "") -> None:
        # Written and COMMITTED BEFORE the object is uploaded, so the object can never exist without
        # something durable naming it. Idempotent on the object key: re-recording the same intent
        # (a retry, a redelivery) is a no-op rather than a second row.
        await db.execute(
            pg_insert(SourceObjectCleanup)
            .values(id=uuid.uuid4(), source_id=source_id, object_key=object_key,
                    state=ReconciliationState.pending,
                    **tenant_scope.stamp(owner_id=owner_id, organization_id=organization_id))
            .on_conflict_do_nothing(index_elements=["object_key"]))

    async def resolve(self, db: AsyncSession, *, object_key: str, state: ReconciliationState,
                      detail: str | None = None) -> None:
        # Resolution is keyed on the object, not on a row id, so a caller that never read the row
        # (the upload path) can still close its own intent.
        row = (await db.execute(
            select(SourceObjectCleanup)
            .where(SourceObjectCleanup.object_key == object_key))).scalar_one_or_none()
        if row is None:
            return
        row.state = state
        row.updated_at = datetime.now(UTC)
        if detail is not None:
            row.last_error = detail[:512]

    async def claim_pending(self, db: AsyncSession, *,
                            limit: int = 50) -> list[SourceObjectCleanup]:
        # FOR UPDATE SKIP LOCKED is the claim: two reconcilers running at once take disjoint rows,
        # and a reconciler that dies mid-sweep has its locks released by PostgreSQL when its
        # connection goes - so a stale claim recovers itself rather than wedging the record.
        rows = await db.execute(
            select(SourceObjectCleanup)
            .where(SourceObjectCleanup.state == ReconciliationState.pending)
            .order_by(SourceObjectCleanup.created_at)
            .limit(limit)
            .with_for_update(skip_locked=True))
        return list(rows.scalars().all())

    async def record_failure(self, db: AsyncSession, *, row_id: uuid.UUID, detail: str) -> None:
        # A delete that failed stays PENDING and is retried, until the budget is spent - at which
        # point it is abandoned rather than retried forever, and a person still has the record.
        row = await db.get(SourceObjectCleanup, row_id)
        if row is None:
            return
        row.retry_count = row.retry_count + 1
        row.last_error = detail[:512]
        row.updated_at = datetime.now(UTC)
        if row.retry_count >= MAX_CLEANUP_RETRIES:
            row.state = ReconciliationState.abandoned
