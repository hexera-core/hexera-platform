# Responsibility: Read and write organizations rows.
# Boundaries: rows only - who may belong to one is the membership repository's question.
from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from meshpipeline.persistence.models import Organization


class OrganizationRepository:

    async def create(self, db: AsyncSession, *, name: str, slug: str) -> Organization:
        row = Organization(name=name[:256], slug=slug[:64])
        db.add(row)
        await db.flush()
        return row

    async def get(self, db: AsyncSession, organization_id: uuid.UUID) -> Organization | None:
        res = await db.execute(select(Organization).where(Organization.id == organization_id))
        return res.scalar_one_or_none()
