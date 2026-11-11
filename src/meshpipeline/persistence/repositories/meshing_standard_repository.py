# Responsibility: Read and write an organisation's meshing standard row (organization_meshing_standards, 0020).
# Boundaries: rows only, BY ORGANISATION. Whether the caller may see or change an organisation's standard is
# application/meshing_standard_service's question (membership), never this module's.
from __future__ import annotations

import uuid

from sqlalchemy import delete, func
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from meshpipeline.persistence.models import OrganizationMeshingStandard


class MeshingStandardRepository:

    async def get_for_organization(self, db: AsyncSession,
                                   organization_id: uuid.UUID) -> OrganizationMeshingStandard | None:
        # NOT `get` (test_no_ambiguous_get_alias_was_restored): this reads BY THE ORGANISATION it belongs to,
        # and the one row there is.
        return await db.get(OrganizationMeshingStandard, organization_id)

    async def put(self, db: AsyncSession, *, organization_id: uuid.UUID, text: str, reading: dict | None,
                  updated_by: str, shell_elements: str = "quads") -> OrganizationMeshingStandard:
        # ONE ROW PER ORGANISATION, written in one statement: two members saving at once leave the last save,
        # whole - never one's text with the other's reading.
        stmt = insert(OrganizationMeshingStandard).values(
            organization_id=organization_id, text=text, reading=reading, updated_by=updated_by[:320],
            shell_elements=shell_elements)
        stmt = stmt.on_conflict_do_update(
            index_elements=[OrganizationMeshingStandard.organization_id],
            set_={"text": stmt.excluded.text, "reading": stmt.excluded.reading,
                  "shell_elements": stmt.excluded.shell_elements,
                  "updated_by": stmt.excluded.updated_by, "updated_at": func.now()})
        await db.execute(stmt)
        await db.flush()
        row = await db.get(OrganizationMeshingStandard, organization_id, populate_existing=True)
        assert row is not None
        return row

    async def clear(self, db: AsyncSession, *, organization_id: uuid.UUID) -> None:
        # No row is no standard: a cleared standard leaves nothing behind.
        await db.execute(delete(OrganizationMeshingStandard)
                         .where(OrganizationMeshingStandard.organization_id == organization_id))
        await db.flush()
