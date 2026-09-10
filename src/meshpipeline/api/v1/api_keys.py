# src/meshpipeline/api/v1/api_keys.py
# Responsibility: Let a console session mint, list and revoke the keys that act on its behalf.
# Owns: the rule that a key may not manage keys.
# Boundaries: transport over api_key_service; the credential's meaning belongs to api/security.py.
from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from meshpipeline.api.security import owner_dep, principal_dep
from meshpipeline.application import api_key_service
from meshpipeline.contracts.identity import Credential, Principal
from meshpipeline.persistence.session import get_db

router = APIRouter()

#: THE REFUSAL, and it deliberately says something. Every other refusal on this API is
#: undifferentiated because the difference would disclose whether an account or a key exists.
#: This one discloses nothing of the sort - the caller has already proven a valid key - and a
#: person holding one needs to know the operation wants a console session rather than concluding
#: their key expired.
_KEY_CANNOT_MANAGE_KEYS = ("API keys cannot manage API keys. Sign in to the console to mint or "
                           "revoke a key.")


class CreateKeyIn(BaseModel):
    #: what the holder calls it. Display only, never used to find a key.
    name: str = Field(default="", max_length=128)


def _require_console_session(principal: Principal) -> None:
    """Refuse a caller acting on an API key.

    `resolve_principal` accepts `Authorization: Bearer hx_live_…` on EVERY /api/v1 route, so
    without this gate a leaked key mints its own replacements and revoking the original
    accomplishes nothing: the key becomes permanent, self-renewing persistence in the account.
    The same argument covers revocation, where a leaked key would instead revoke the owner's real
    keys as a denial of service. `Principal.credential` already carries the distinction, so the
    whole gate is one comparison.
    """
    if principal.credential is Credential.api_key:
        raise HTTPException(status_code=403, detail=_KEY_CANNOT_MANAGE_KEYS)


def _row(row) -> dict:
    # THE SECRET HALF IS NOT HERE, and neither is key_hash. `key_prefix` is the public half and is
    # what the holder matches their own copy against; the secret existed once, in the response to
    # the POST that minted it.
    return {
        "id": str(row.id),
        "name": row.name,
        "key_prefix": row.key_prefix,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "last_used_at": row.last_used_at.isoformat() if row.last_used_at else None,
        "revoked_at": row.revoked_at.isoformat() if row.revoked_at else None,
        "expires_at": row.expires_at.isoformat() if row.expires_at else None,
    }


@router.get("")
async def list_keys(principal: Annotated[Principal, Depends(principal_dep)],
                    owner_id: str = Depends(owner_dep)) -> dict:
    _require_console_session(principal)
    async with get_db() as db:
        rows = await api_key_service.list_for_owner(
            db, owner_id=owner_id, organization_id=principal.organization_id)
    return {"items": [_row(row) for row in rows]}


@router.post("", status_code=201)
async def create_key(body: CreateKeyIn,
                     principal: Annotated[Principal, Depends(principal_dep)],
                     # BOTH dependencies, deliberately. `owner_dep` is what
                     # test_job_mutating_routes_require_authentication looks for in the AST, and
                     # it resolves from the SAME cached principal - one credential check, not two.
                     owner_id: str = Depends(owner_dep)) -> dict:
    _require_console_session(principal)
    organization = None
    if principal.organization_id:
        try:
            organization = uuid.UUID(principal.organization_id)
        except ValueError:
            # Narrow to no organisation rather than raise, exactly as tenant_scope does. A key
            # stamped with the owner alone still authenticates; one that 500s is never issued.
            organization = None

    async with get_db() as db:
        issued = await api_key_service.issue(db, owner_id=owner_id, name=body.name,
                                             organization_id=organization)
    # `presented` IS THE SECRET. It exists in this response and nowhere else - not in the row,
    # not in the list, not in a log. A lost key is replaced, never looked up.
    return {"id": issued.key_id, "name": issued.name, "key_prefix": issued.key_prefix,
            "presented": issued.presented}


@router.delete("/{key_id}")
async def revoke_key(key_id: uuid.UUID,
                     principal: Annotated[Principal, Depends(principal_dep)],
                     owner_id: str = Depends(owner_dep)) -> dict:
    _require_console_session(principal)
    async with get_db() as db:
        revoked = await api_key_service.revoke(db, owner_id=owner_id, key_id=key_id,
                                               organization_id=principal.organization_id)
    # False for a key that does not exist AND for one already revoked - the repository's
    # `revoked_at is null` predicate makes a repeat revocation report False rather than rewriting
    # the moment it happened. Another tenant's key id is indistinguishable from a missing one.
    return {"revoked": revoked}
