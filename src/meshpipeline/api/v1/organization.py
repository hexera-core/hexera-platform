# src/meshpipeline/api/v1/organization.py
# Responsibility: Report which organisation the caller acts within, and who else does.
# Boundaries: transport over account_service; nothing here creates, renames or invites, all of
# which are non-goals - and reading only.
from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends

from meshpipeline.api.security import org_dep, owner_dep
from meshpipeline.application import account_service
from meshpipeline.persistence.session import get_db

router = APIRouter()


@router.get("")
async def read_organization(owner_id: Annotated[str, Depends(owner_dep)] = "",
                            organization_id: Annotated[str, Depends(org_dep)] = "") -> dict:
    # DECISION 12 - the absent-or-malformed organisation degrades to the caller as the sole
    # member rather than to an empty list, which would read to the person whose account it is as
    # though their account had vanished. That rule is account_service's, not this route's: it is
    # a decision about what an account's owner is entitled to see, and this function only
    # renders what the service decided.
    async with get_db() as db:
        view = await account_service.organization_view(db, owner_id=owner_id,
                                                       organization_id=organization_id)

    return {
        "organization": ({"id": str(view.organization.id), "name": view.organization.name,
                          "slug": view.organization.slug}
                         if view.organization else None),
        "members": [{"email": m.email, "name": m.name, "role": m.role} for m in view.members],
    }
