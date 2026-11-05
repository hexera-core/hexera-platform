# src/meshpipeline/api/v1/account.py
# Responsibility: Let a signed-in person download their data and delete their account.
# Boundaries: transport over privacy_service. Both routes refuse an API key: a leaked key must not
#             be enough to export a tenant wholesale or to erase it.
from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Body, Depends, HTTPException
from fastapi.responses import JSONResponse

from meshpipeline.api.security import principal_dep
from meshpipeline.application import audit, privacy_service
from meshpipeline.contracts.identity import Credential, Principal
from meshpipeline.persistence.session import get_db

router = APIRouter()


def _console_session(principal: Principal) -> Principal:
    if principal.credential == Credential.api_key:
        raise HTTPException(status_code=403,
                            detail="Sign in to the console to export or delete your account")
    return principal


@router.get("/export")
async def export_account(principal: Annotated[Principal, Depends(principal_dep)]):
    principal = _console_session(principal)
    async with get_db() as db:
        body = await privacy_service.export_account(
            db, owner_id=principal.owner_id, organization_id=principal.organization_id)
    return JSONResponse(body, headers={
        "Content-Disposition": 'attachment; filename="hexera-account-export.json"',
        "Cache-Control": "no-store",
    })


@router.post("/erasure", status_code=202)
async def request_erasure(principal: Annotated[Principal, Depends(principal_dep)],
                          confirm_email: Annotated[str, Body(embed=True)] = "") -> dict:
    principal = _console_session(principal)
    # TYPED CONFIRMATION, checked here rather than trusted to the form: this is irreversible, and
    # a stray request from a script or a mis-click must not reach it.
    if confirm_email.strip().lower() != principal.owner_id.strip().lower():
        raise HTTPException(status_code=400,
                            detail="Type your account's email address to confirm deletion")
    try:
        async with get_db() as db:
            result = await privacy_service.request_erasure(
                db, owner_id=principal.owner_id, organization_id=principal.organization_id)
    except privacy_service.ErasureRefused as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    # AUDITED HERE, after get_db has committed: a line describing a rolled-back erasure would
    # record something that never happened.
    detail = result.pop("audit_detail", None)
    if detail is not None:
        audit.record("privacy.erasure_requested", actor=detail.pop("actor", ""),
                     organization_id=principal.organization_id, detail=detail)
    return result
