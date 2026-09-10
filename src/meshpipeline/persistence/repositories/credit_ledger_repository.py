# Responsibility: Append credit ledger entries and derive an organisation's balance from them.
# Boundaries: rows and their sum - what a credit is worth, and when one may be spent, is not decided here.
from __future__ import annotations

import uuid

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from meshpipeline.persistence.models import CreditEntryType, CreditLedgerEntry


class CreditLedgerRepository:
    # APPEND AND SUM, and deliberately nothing else. There is no update and no delete method,
    # because an entry that can be rewritten is not a ledger - a correction is another entry.

    async def append(self, db: AsyncSession, *, organization_id: uuid.UUID,
                     entry_type: CreditEntryType, amount: int,
                     reason: str = "") -> CreditLedgerEntry:
        row = CreditLedgerEntry(organization_id=organization_id, entry_type=entry_type,
                                amount=int(amount), reason=reason[:128])
        db.add(row)
        await db.flush()
        return row

    async def balance(self, db: AsyncSession, *, organization_id: uuid.UUID) -> int:
        # coalesce, because SUM over no rows is NULL and a new organisation's balance is 0.
        res = await db.execute(
            select(func.coalesce(func.sum(CreditLedgerEntry.amount), 0))
            .where(CreditLedgerEntry.organization_id == organization_id))
        return int(res.scalar_one())
