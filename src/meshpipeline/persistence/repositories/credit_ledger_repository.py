# Responsibility: Append credit ledger entries and derive an organisation's balance from them.
# Boundaries: rows and their sum - what a credit is worth, and when one may be spent, is not decided here.
from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import case, func, select, tuple_
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

    async def list_for_org(self, db: AsyncSession, *, organization_id: uuid.UUID,
                           limit: int = 25,
                           before: tuple[datetime, uuid.UUID] | None = None
                           ) -> list[CreditLedgerEntry]:
        """One page of an organisation's movements, newest first.

        Served directly by `ix_credit_ledger_org_created`, which 0003 created for the balance
        query and this ordering alike - the reason decision 9 adds no index this cycle.

        Organisation-scoped ONLY, with no owner fallback: the ledger has no `owner_id` column,
        because a credit belongs to the tenant rather than to whoever happened to spend it.
        """
        statement = (
            select(CreditLedgerEntry)
            .where(CreditLedgerEntry.organization_id == organization_id)
            .order_by(CreditLedgerEntry.created_at.desc(), CreditLedgerEntry.id.desc())
            .limit(limit))
        if before is not None:
            statement = statement.where(
                tuple_(CreditLedgerEntry.created_at, CreditLedgerEntry.id) < before)
        result = await db.execute(statement)
        return list(result.scalars().all())

    async def usage_by_period(self, db: AsyncSession, *, months: int = 6) -> list[dict]:
        """Credits granted and spent per calendar month, across every tenant. Admin routes only."""
        # DATE_TRUNC IN THE DATABASE, not a Python loop over rows. The ledger grows forever, and the
        # months an operator wants are a handful - pulling every entry back to bucket it in the
        # process would move the whole table to answer a six-row question.
        #
        # GRANTS AND DEBITS ARE SUMMED SEPARATELY rather than netted. A month where 10,000 credits
        # were granted and 10,000 spent is not the same month as one where nothing happened, and a
        # single net figure cannot tell them apart.
        bucket = func.date_trunc("month", CreditLedgerEntry.created_at).label("period")
        res = await db.execute(
            select(bucket,
                   func.coalesce(func.sum(case((CreditLedgerEntry.amount > 0,
                                                CreditLedgerEntry.amount), else_=0)), 0),
                   func.coalesce(func.sum(case((CreditLedgerEntry.amount < 0,
                                                -CreditLedgerEntry.amount), else_=0)), 0),
                   func.count())
            .group_by(bucket)
            .order_by(bucket.desc())
            .limit(months))
        return [{"period": row[0].isoformat() if row[0] else None,
                 "granted": int(row[1] or 0), "spent": int(row[2] or 0),
                 "entries": int(row[3] or 0)}
                for row in res.all()]

    async def unmetered_total(self, db: AsyncSession) -> dict:
        """How much consumption has been debited but not yet reported to the provider's meter."""
        # THE SWEEP'S BACKLOG, which is what an operator actually needs to see: a number that keeps
        # climbing means usage is being recorded and never billed, and nothing else in the product
        # would say so.
        res = await db.execute(
            select(func.count(),
                   func.coalesce(func.sum(-CreditLedgerEntry.amount), 0))
            .where(CreditLedgerEntry.metered_at.is_(None),
                   CreditLedgerEntry.entry_type == CreditEntryType.debit))
        row = res.one()
        return {"entries": int(row[0] or 0), "credits": int(row[1] or 0)}
