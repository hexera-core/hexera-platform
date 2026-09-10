# Responsibility: Report what an organisation's credit balance is.
# Boundaries: reading only - nothing in this cycle spends, and this route offers no way to.
from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends

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
