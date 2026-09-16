# Responsibility: Read and record what an uploaded geometry's coordinates mean physically.
# Boundaries: an interpretation is immutable once recorded - a job that ran under millimetres keeps meaning millimetres.
from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from meshpipeline.contracts.geometry_units import (
    GeometryInterpretation,
    LengthUnit,
    ResolutionBasis,
    scale_to_metres,
)
from meshpipeline.persistence.models import GeometryInterpretationRow
from meshpipeline.persistence.repositories import tenant_scope


def _to_domain(row: GeometryInterpretationRow) -> GeometryInterpretation:
    return GeometryInterpretation(
        interpretation_id=str(row.id), owner_id=row.owner_id,
        geometry_source_id=str(row.geometry_source_id), unit=LengthUnit(row.unit),
        scale_to_metres=float(row.scale_to_metres), basis=ResolutionBasis(row.basis),
        evidence=row.evidence or "")


class GeometryInterpretationRepository:

    async def record(self, db: AsyncSession, *, owner_id: str, geometry_source_id: uuid.UUID,
                     unit: LengthUnit, basis: ResolutionBasis, evidence: str = "",
                     organization_id: str = "") -> GeometryInterpretation:
        # `find` stays owner_id-only, deliberately: it is the idempotency check behind
        # `uq_geometry_interpretation`, which is itself keyed on owner_id, not on the
        # organisation. Widening its match to the organisation would let this return a
        # DIFFERENT owner's row for the same source/unit/basis inside one organisation - the
        # right answer to "does a duplicate already exist for this constraint" is the
        # constraint's own key, not the wider tenant scope.
        existing = await self.find(db, owner_id=owner_id, geometry_source_id=geometry_source_id,
                                   unit=unit, basis=basis)
        if existing is not None:
            return existing
        row = GeometryInterpretationRow(
            **tenant_scope.stamp(owner_id=owner_id, organization_id=organization_id),
            geometry_source_id=geometry_source_id, unit=unit.value,
            scale_to_metres=scale_to_metres(unit), basis=basis.value, evidence=evidence[:256])
        db.add(row)
        await db.flush()
        return _to_domain(row)

    async def find(self, db: AsyncSession, *, owner_id: str, geometry_source_id: uuid.UUID,
                   unit: LengthUnit, basis: ResolutionBasis) -> GeometryInterpretation | None:
        # Owner-scoped only - see the note in `record`. This is the unique constraint's own
        # lookup, not a tenant-facing read.
        res = await db.execute(
            select(GeometryInterpretationRow).where(
                GeometryInterpretationRow.owner_id == owner_id,
                GeometryInterpretationRow.geometry_source_id == geometry_source_id,
                GeometryInterpretationRow.unit == unit.value,
                GeometryInterpretationRow.basis == basis.value))
        row = res.scalar_one_or_none()
        return _to_domain(row) if row is not None else None

    async def get_for_owner(self, db: AsyncSession, interpretation_id: uuid.UUID,
                            owner_id: str, *, organization_id: str = "") -> GeometryInterpretation | None:
        res = await db.execute(
            select(GeometryInterpretationRow).where(
                GeometryInterpretationRow.id == interpretation_id,
                tenant_scope.scope(GeometryInterpretationRow, owner_id=owner_id,
                                   organization_id=organization_id)))
        row = res.scalar_one_or_none()
        return _to_domain(row) if row is not None else None

    async def latest_for_source(self, db: AsyncSession, *, owner_id: str,
                                geometry_source_id: uuid.UUID,
                                organization_id: str = "") -> GeometryInterpretation | None:
        res = await db.execute(
            select(GeometryInterpretationRow)
            .where(tenant_scope.scope(GeometryInterpretationRow, owner_id=owner_id,
                                      organization_id=organization_id),
                   GeometryInterpretationRow.geometry_source_id == geometry_source_id)
            .order_by(GeometryInterpretationRow.created_at.desc(),
                      GeometryInterpretationRow.id.desc()))
        row = res.scalars().first()
        return _to_domain(row) if row is not None else None
