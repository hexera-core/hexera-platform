# src/meshpipeline/api/v1/organization.py
# Responsibility: Report which organisation the caller acts within, and who else does.
# Boundaries: reading only - nothing here creates, renames or invites, all of which are non-goals.
from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends

from meshpipeline.api.security import org_dep, owner_dep
from meshpipeline.persistence.repositories.membership_repository import MembershipRepository
from meshpipeline.persistence.repositories.organization_repository import OrganizationRepository
from meshpipeline.persistence.session import get_db

router = APIRouter()

organization_repo = OrganizationRepository()
membership_repo = MembershipRepository()


@router.get("")
async def read_organization(owner_id: Annotated[str, Depends(owner_dep)] = "",
                            organization_id: Annotated[str, Depends(org_dep)] = "") -> dict:
    parsed = _parsed(organization_id)
    if parsed is None:
        # DECISION 12. An absent or malformed organisation is a deployment between 0004 and the
        # image that fills the column, or a lookup that could not run - the same states
        # credits.py answers 0 for. Answering with an empty member list would read to the person
        # whose account it is as though their account had vanished, so the caller is shown
        # themselves: the honest degraded view, and exactly what tenant_scope's owner fallback
        # means everywhere else.
        return {"organization": None,
                "members": [{"email": owner_id, "name": owner_id, "role": "owner"}]}

    async with get_db() as db:
        row = await organization_repo.get(db, parsed)
        members = await membership_repo.list_members(db, organization_id=parsed)

    return {
        "organization": ({"id": str(row.id), "name": row.name, "slug": row.slug}
                         if row else None),
        "members": [{"email": user.email, "name": user.name or user.email,
                     "role": getattr(role, "value", role)} for user, role in members],
    }


def _parsed(organization_id: str) -> uuid.UUID | None:
    if not organization_id:
        return None
    try:
        return uuid.UUID(organization_id)
    except (ValueError, AttributeError, TypeError):
        return None
