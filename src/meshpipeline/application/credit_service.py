# Responsibility: Issue credits to an organisation and answer what its balance is.
# Owns: the fact that a balance is derived from entries, never stored.
# Boundaries: issuance and reading only - this cycle deliberately has no way to spend (design section 2).
from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession

import meshpipeline.settings.policy as polcfg
from meshpipeline.persistence.models import CreditEntryType
from meshpipeline.persistence.repositories.credit_ledger_repository import (
    CreditLedgerRepository,
)

credit_ledger_repo = CreditLedgerRepository()

#: What a signup grant is called in the ledger, so a person reading their own history sees why
#: the credits are there.
SIGNUP_REASON = "signup grant"


async def grant(db: AsyncSession, *, organization_id: uuid.UUID, amount: int,
                reason: str = "") -> None:
    # A grant is POSITIVE, always. Allowing a negative one would make this the single function in
    # the cycle capable of removing credits - a debit wearing the wrong name - and the design says
    # nothing here spends.
    if amount < 0:
        raise ValueError("a grant cannot be negative")
    # Zero is not an error, it is the configured way to disable the grant. The ledger's CHECK
    # refuses a zero-amount row, so the entry is simply not written.
    if amount == 0:
        return
    await credit_ledger_repo.append(db, organization_id=organization_id,
                                    entry_type=CreditEntryType.grant, amount=amount,
                                    reason=reason)


async def balance(db: AsyncSession, *, organization_id: uuid.UUID) -> int:
    return await credit_ledger_repo.balance(db, organization_id=organization_id)


async def history(db: AsyncSession, *, organization_id: uuid.UUID, limit: int = 25,
                  before: tuple[datetime, uuid.UUID] | None = None) -> list:
    return await credit_ledger_repo.list_for_org(db, organization_id=organization_id,
                                                 limit=limit, before=before)


async def grant_signup_credits(db: AsyncSession, *, organization_id: uuid.UUID) -> int:
    # Read through the module, not bound at import, so an operator's value and a test's
    # monkeypatch both reach it - the same discipline settings/plans.py uses.
    amount = polcfg.SIGNUP_GRANT_CREDITS
    await grant(db, organization_id=organization_id, amount=amount, reason=SIGNUP_REASON)
    return amount
