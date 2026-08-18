# Responsibility: Store terminal events durably in the same transaction as the result they announce.
# Boundaries: the transactional outbox: the dedup key makes republication safe, so a retry announces once.
from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from meshpipeline.persistence.models import TerminalOutbox


def dedup_key_for(job_id: uuid.UUID | str) -> str:
    return f"terminal:{job_id}"


@dataclass(frozen=True)
class OutboxRow:
    id: uuid.UUID
    job_id: uuid.UUID
    execution_generation: int
    terminal_status: str
    dedup_key: str
    event_payload: dict
    publish_attempts: int


class TerminalOutboxRepository:
    async def enqueue(self, db: AsyncSession, *, job_id: uuid.UUID, execution_generation: int,
                      terminal_status: str, final_result_schema_version: int,
                      event_payload: dict) -> bool:
        stmt = (pg_insert(TerminalOutbox)
                .values(id=uuid.uuid4(), job_id=job_id, execution_generation=execution_generation,
                        terminal_status=terminal_status,
                        final_result_schema_version=final_result_schema_version,
                        event_payload=event_payload, dedup_key=dedup_key_for(job_id))
                .on_conflict_do_nothing(index_elements=["dedup_key"]))
        res = await db.execute(stmt)
        return bool(res.rowcount)

    async def claim_pending(self, db: AsyncSession, *, limit: int = 50,
                            job_id: uuid.UUID | None = None,
                            now: datetime | None = None) -> list[OutboxRow]:
        now = now or datetime.now(UTC)
        q = (select(TerminalOutbox)
             .where(TerminalOutbox.published_at.is_(None))
             .order_by(TerminalOutbox.created_at)
             .limit(limit).with_for_update(skip_locked=True))
        if job_id is not None:
            q = q.where(TerminalOutbox.job_id == job_id)
        rows = (await db.execute(q)).scalars().all()
        out: list[OutboxRow] = []
        for r in rows:
            r.locked_at = now
            r.publish_attempts = (r.publish_attempts or 0) + 1
            out.append(OutboxRow(id=r.id, job_id=r.job_id,
                                 execution_generation=r.execution_generation,
                                 terminal_status=r.terminal_status, dedup_key=r.dedup_key,
                                 event_payload=dict(r.event_payload or {}),
                                 publish_attempts=r.publish_attempts))
        await db.flush()
        return out

    async def mark_published(self, db: AsyncSession, row_id: uuid.UUID,
                             *, now: datetime | None = None) -> None:
        await db.execute(update(TerminalOutbox).where(TerminalOutbox.id == row_id)
                         .values(published_at=now or datetime.now(UTC), last_error=None))

    async def record_failure(self, db: AsyncSession, row_id: uuid.UUID, category: str) -> None:
        await db.execute(update(TerminalOutbox).where(TerminalOutbox.id == row_id)
                         .values(last_error=str(category)[:256]))

    async def get_by_job(self, db: AsyncSession, job_id: uuid.UUID) -> list[TerminalOutbox]:
        return list((await db.execute(
            select(TerminalOutbox).where(TerminalOutbox.job_id == job_id))).scalars().all())


__all__ = ["TerminalOutboxRepository", "OutboxRow", "dedup_key_for"]
