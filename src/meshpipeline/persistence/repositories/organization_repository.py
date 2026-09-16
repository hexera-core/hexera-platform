# Responsibility: Read and write organizations rows.
# Boundaries: rows only - who may belong to one is the membership repository's question.
from __future__ import annotations

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from meshpipeline.persistence.models import Organization


class OrganizationRepository:

    async def create(self, db: AsyncSession, *, name: str, slug: str) -> Organization:
        row = Organization(name=name[:256], slug=slug[:64])
        db.add(row)
        await db.flush()
        return row

    async def get_by_id(self, db: AsyncSession,
                        organization_id: uuid.UUID) -> Organization | None:
        # NOT `get`. A bare `get` on a repository does not say what it reads by, and
        # test_no_ambiguous_get_alias_was_restored pins that across every repository here: the
        # ones that read a tenant's row are `get_for_owner`, the one that deliberately does not
        # scope is `get_internal`, and this one reads an organisation BY ITS ID. The caller that
        # decides whether the id may be shown at all is application/account_service.
        return await db.get(Organization, organization_id)
