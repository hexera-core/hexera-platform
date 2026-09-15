# Responsibility: Read and record what an uploaded geometry was measured to be.
# Boundaries: one row per source, tenant-scoped; it decides nothing about what the document means.
from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from meshpipeline.persistence.models import GeometryMeasurement


class GeometryMeasurementRepository:
    """The one reader and writer of `geometry_measurements`.

    Every read is tenant-scoped, like every other geometry read: absent and foreign are the same
    answer, so a tenant cannot learn that another tenant uploaded the same bytes.
    """

    async def record(self, db: AsyncSession, *, owner_id: str, geometry_source_id: uuid.UUID,
                     sha256: str, purpose: str, status: str, document: dict,
                     reason: str = "", facts_schema_version: int = 0, agent_git_sha: str = "",
                     measure_seconds: float | None = None) -> GeometryMeasurement:
        """Write the measurement, or replace the one already there for this source.

        Re-measuring the same source is legitimate - a measurement package moves on, a failed
        attempt is retried - and the answer for one upload is still one row, so an existing row is
        updated in place rather than joined by a second one. The identity that must not change is
        the sha256: it is a property of the bytes, and a different digest is a different upload.
        """
        existing = await self.for_source(db, owner_id=owner_id, geometry_source_id=geometry_source_id)
        row = existing if existing is not None else GeometryMeasurement(
            geometry_source_id=geometry_source_id, owner_id=owner_id, sha256=sha256)
        row.sha256 = sha256
        row.purpose = purpose[:32]
        row.status = status[:32]
        row.reason = (reason or "")[:512]
        row.document = document
        row.facts_schema_version = int(facts_schema_version or 0)
        row.agent_git_sha = (agent_git_sha or "")[:64]
        row.measure_seconds = measure_seconds
        if existing is None:
            db.add(row)
        await db.flush()
        return row

    async def for_source(self, db: AsyncSession, *, owner_id: str,
                         geometry_source_id: uuid.UUID) -> GeometryMeasurement | None:
        res = await db.execute(
            select(GeometryMeasurement).where(
                GeometryMeasurement.geometry_source_id == geometry_source_id,
                GeometryMeasurement.owner_id == owner_id))
        return res.scalar_one_or_none()
