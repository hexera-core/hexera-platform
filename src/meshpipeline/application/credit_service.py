# Responsibility: Issue credits to an organisation and answer what its balance is.
# Owns: the fact that a balance is derived from entries, never stored.
# Boundaries: issuance, spending and reading. Spending arrived with Stripe billing; the ledger's
#             shape was settled for it from the start, so it was a new caller and not a migration.
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


async def debit(db: AsyncSession, *, organization_id: uuid.UUID, amount: int,
                reason: str = "", overage: int = 0) -> None:
    # THE SPENDING SIDE, which the identity cycle declared and deliberately did not write. It takes
    # a POSITIVE amount and stores it NEGATED, so no caller has to remember the sign convention -
    # a caller that got it wrong would write a debit that increases the balance, and the ledger's
    # only CHECK is that the amount is non-zero, so nothing downstream would catch it.
    if amount < 0:
        raise ValueError("a debit takes a positive amount; it is stored negated")
    if amount == 0:
        return
    # `overage` is the part of this debit the metered price will bill - never more than the debit.
    if not 0 <= overage <= amount:
        raise ValueError("a debit's overage lies between 0 and the debit itself")
    await credit_ledger_repo.append(db, organization_id=organization_id,
                                    entry_type=CreditEntryType.debit, amount=-amount,
                                    reason=reason, overage=overage)


async def refund(db: AsyncSession, *, organization_id: uuid.UUID, amount: int,
                 reason: str = "") -> None:
    # A DEBIT THAT SHOULD NOT HAVE STOOD, returned as its own entry type rather than by deleting the
    # debit. The ledger is append-only because "why is my balance this?" must stay answerable, and a
    # deleted row answers it with silence. This pipeline has several failure paths that can strand a
    # charge - FailedReason, artifact_reconciliations - and each of them ends here.
    if amount < 0:
        raise ValueError("a refund cannot be negative")
    if amount == 0:
        return
    await credit_ledger_repo.append(db, organization_id=organization_id,
                                    entry_type=CreditEntryType.refund, amount=amount,
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
