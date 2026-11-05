# Responsibility: Own the durable cleanup intent for an uploaded source object, and its claim and resolution.
# Boundaries: storage for the intent; deciding what to delete is the maintenance sweep's, and the bytes are the store's.
from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, or_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from meshpipeline.persistence.models import ReconciliationState, SourceObjectCleanup
from meshpipeline.persistence.repositories import tenant_scope

#: How many times the sweep may fail to delete one object before it stops retrying and leaves the
#: record for a person. Bounded so retry metadata cannot grow without end.
MAX_CLEANUP_RETRIES = 5


class SourceCleanupRepository:

    async def record_intent(self, db: AsyncSession, *, owner_id: str, source_id: uuid.UUID,
                            object_key: str, organization_id: str = "",
                            hold: timedelta | None = None) -> None:
        # Written and COMMITTED BEFORE the object is uploaded, so the object can never exist without
        # something durable naming it. Idempotent on the object key: re-recording the same intent
        # (a retry, a redelivery) is a no-op rather than a second row.
        #
        # `hold` is for an upload whose bytes arrive WITHOUT passing through this process - a
        # client writing straight to the store - so they may still be arriving long after this
        # commit. The sweep leaves the intent alone until the hold has passed (claim_pending). The
        # moment is computed by the database, like the comparison that reads it.
        values: dict = dict(id=uuid.uuid4(), source_id=source_id, object_key=object_key,
                            state=ReconciliationState.pending,
                            **tenant_scope.stamp(owner_id=owner_id,
                                                 organization_id=organization_id))
        if hold is not None:
            values["next_attempt_at"] = func.now() + hold
        await db.execute(
            pg_insert(SourceObjectCleanup)
            .values(**values)
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
        # A settled intent keeps no schedule: it is never claimed again, and a stale hold on a
        # resolved row would only be something for a later reader to misinterpret.
        if state is not ReconciliationState.pending:
            row.next_attempt_at = None
        if detail is not None:
            row.last_error = detail[:512]

    async def get_upload_for_owner(self, db: AsyncSession, *, source_id: uuid.UUID,
                                   object_key: str, owner_id: str,
                                   organization_id: str = "") -> SourceObjectCleanup | None:
        # THE CALLER'S OWN upload, or nothing. Scoped on the tenant (the organisation, falling back
        # to the owner) AND on the actor who began it: an upload is one person's action, and an id
        # that belongs to someone else - in another tenant or not - reads exactly like one that
        # never existed, so nobody can finish, probe or delete another person's bytes.
        return (await db.execute(self._upload_query(
            source_id=source_id, object_key=object_key, owner_id=owner_id,
            organization_id=organization_id))).scalar_one_or_none()

    async def lock_upload_for_owner(self, db: AsyncSession, *, source_id: uuid.UUID,
                                    object_key: str, owner_id: str,
                                    organization_id: str = "") -> SourceObjectCleanup | None:
        # The same scoped read, holding the row until the caller's transaction ends. Finishing an
        # upload takes it, so two finishes of one upload run one after the other and the second
        # sees the first one's verdict - rather than both adopting the object, or the loser's
        # compensation deleting bytes the winner already owns. It WAITS (no SKIP LOCKED): a sweep
        # holding the row is settled in seconds, and its verdict is exactly what the finish must see.
        return (await db.execute(self._upload_query(
            source_id=source_id, object_key=object_key, owner_id=owner_id,
            organization_id=organization_id).with_for_update())).scalar_one_or_none()

    @staticmethod
    def _upload_query(*, source_id: uuid.UUID, object_key: str, owner_id: str,
                      organization_id: str):
        return (select(SourceObjectCleanup)
                .where(SourceObjectCleanup.object_key == object_key,
                       SourceObjectCleanup.source_id == source_id,
                       SourceObjectCleanup.owner_id == owner_id,
                       tenant_scope.scope(SourceObjectCleanup, owner_id=owner_id,
                                          organization_id=organization_id)))

    async def claim_pending(self, db: AsyncSession, *,
                            limit: int = 50) -> list[SourceObjectCleanup]:
        # FOR UPDATE SKIP LOCKED is the claim: two reconcilers running at once take disjoint rows,
        # and a reconciler that dies mid-sweep has its locks released by PostgreSQL when its
        # connection goes - so a stale claim recovers itself rather than wedging the record.
        #
        # DUE INTENTS ONLY. `next_attempt_at IS NULL` is every multipart intent, due at once; a
        # direct upload's intent is held until its bytes can no longer be arriving. The comparison
        # is made by the DATABASE (`func.now()`), not the sweeping process, whose clock is not the
        # clock that set the hold.
        rows = await db.execute(
            select(SourceObjectCleanup)
            .where(SourceObjectCleanup.state == ReconciliationState.pending,
                   or_(SourceObjectCleanup.next_attempt_at.is_(None),
                       SourceObjectCleanup.next_attempt_at <= func.now()))
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
