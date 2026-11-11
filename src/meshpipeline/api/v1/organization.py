# src/meshpipeline/api/v1/organization.py
# Responsibility: Report which organisation the caller acts within, and who else does; and hold the
# organisation's meshing standard (the console's Meshing settings).
# Boundaries: transport over account_service and meshing_standard_service; nothing here creates, renames
# or invites, all of which are non-goals. The meshing standard is the one thing a member may write here.
from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from meshpipeline.api.security import org_dep, owner_dep, principal_dep
from meshpipeline.application import account_service, audit, meshing_standard_service
from meshpipeline.contracts.identity import Credential, Principal
from meshpipeline.persistence.session import get_db

router = APIRouter()

#: THE REFUSALS. One answer for every caller outside an organisation it can name: the difference
#: would say whether somebody else's organisation exists. A key caller is told plainly, as
#: api_keys tells it - it has already proven a valid key.
_NO_ORGANISATION = "No organisation to hold a meshing standard."
_KEY_CANNOT_SET_STANDARD = ("API keys cannot change the meshing standard. Sign in to the console "
                            "to change it.")


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


class MeshingStandardIn(BaseModel):
    #: the standard as the user writes it, the way they write a run's notes; empty clears it
    text: str = Field(default="", max_length=meshing_standard_service.MAX_CHARS)


@router.get("/meshing-standard")
async def read_meshing_standard(owner_id: Annotated[str, Depends(owner_dep)] = "",
                                organization_id: Annotated[str, Depends(org_dep)] = "") -> dict:
    """The caller's organisation's meshing standard: the words, what the reader understood (one
    plain line each, as a run says it), whether they were read, and who saved them when."""
    async with get_db() as db:
        got = await meshing_standard_service.view(db, owner_id=owner_id,
                                                  organization_id=organization_id)
    if got is None:
        raise HTTPException(status_code=404, detail=_NO_ORGANISATION)
    return got


@router.put("/meshing-standard")
async def save_meshing_standard(body: MeshingStandardIn,
                                principal: Annotated[Principal, Depends(principal_dep)],
                                owner_id: str = Depends(owner_dep)) -> dict:
    """Save (or, with no words, clear) the caller's organisation's meshing standard. It is read
    once here and the reading is kept for every run. A console session only."""
    if principal.credential is Credential.api_key:
        raise HTTPException(status_code=403, detail=_KEY_CANNOT_SET_STANDARD)
    try:
        got = await meshing_standard_service.save(
            owner_id=owner_id, organization_id=principal.organization_id, text=body.text,
            sessions=get_db)
    except meshing_standard_service.TooLong as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if got is None:
        raise HTTPException(status_code=404, detail=_NO_ORGANISATION)
    audit.record("meshing_standard.cleared" if got["status"] == "empty" else "meshing_standard.saved",
                 actor="console", organization_id=principal.organization_id,
                 detail={"chars": len(got["text"]), "read": got["status"] == "read"})
    return got
