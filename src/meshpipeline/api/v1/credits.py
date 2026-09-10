# Responsibility: Report what an organisation's credit balance is.
# Boundaries: reading only - nothing in this cycle spends, and this route offers no way to.
from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends

from meshpipeline.api import pagination
from meshpipeline.api.schemas import listing
from meshpipeline.api.security import org_dep
from meshpipeline.application import credit_service
from meshpipeline.persistence.session import get_db

router = APIRouter()


@router.get("")
async def read_balance(organization_id: Annotated[str, Depends(org_dep)] = "") -> dict:
    # An organisation-less caller reads 0 rather than failing. That is a deployment between the
    # migration and the image that fills the column, or a lookup that could not run - neither is
    # a reason to answer a proven caller with an error.
    if not organization_id:
        return {"balance": 0, "unit": "credits"}

    try:
        organization = uuid.UUID(organization_id)
    except ValueError:
        # SAME ANSWER AS AN ABSENT ORGANISATION, and for the same reason: a Principal carries the
        # organisation as a string, and a value that is not a uuid is one this route cannot scope
        # on. tenant_scope treats a malformed id as absent rather than raising inside a request;
        # a balance that reads 0 is the consistent narrowing, never a 500 for a proven caller.
        return {"balance": 0, "unit": "credits"}
    async with get_db() as db:
        balance = await credit_service.balance(db, organization_id=organization)
    # The unit is named but deliberately undefined: what a credit BUYS is not decided, and a
    # client that reads this must not infer a currency from a bare number.
    return {"balance": balance, "unit": "credits"}


@router.get("/history")
async def read_history(limit: int = pagination.DEFAULT_LIMIT, cursor: str | None = None,
                       organization_id: Annotated[str, Depends(org_dep)] = "") -> dict:
    bounded = pagination.clamp_limit(limit)
    # THE SAME NARROWING read_balance applies. An absent or malformed organisation is a
    # deployment between the migration and the image that fills the column, not a bad request:
    # it reads as an empty ledger, never as a 500 for a caller who has already proven who it is.
    if not organization_id:
        return listing.page([], limit=bounded, last_key=None)
    try:
        organization = uuid.UUID(organization_id)
    except ValueError:
        return listing.page([], limit=bounded, last_key=None)

    async with get_db() as db:
        rows = await credit_service.history(db, organization_id=organization, limit=bounded,
                                            before=pagination.decode_cursor(cursor))
    items = [{
        "id": str(row.id),
        "entry_type": getattr(row.entry_type, "value", row.entry_type),
        # SIGNED, exactly as stored. A grant is positive and a debit negative, so a client sums
        # the column rather than branching on the type - the same reason the column is signed.
        "amount": row.amount,
        "reason": row.reason,
        "created_at": row.created_at.isoformat() if row.created_at else None,
    } for row in rows]
    last = (rows[-1].created_at, rows[-1].id) if rows else None
    return listing.page(items, limit=bounded, last_key=last)
