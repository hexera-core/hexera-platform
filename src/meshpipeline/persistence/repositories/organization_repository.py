# Responsibility: Read and write organizations rows.
# Boundaries: rows only - who may belong to one is the membership repository's question.
from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from meshpipeline.persistence.models import Organization


class OrganizationRepository:

    async def create(self, db: AsyncSession, *, name: str, slug: str) -> Organization:
        row = Organization(name=name[:256], slug=slug[:64])
        db.add(row)
        await db.flush()
        return row

    async def get_by_id(self, db: AsyncSession,
                        organization_id: uuid.UUID) -> Organization | None:
        # NOT `get`. A bare `get` on a repository does not say what it reads by, and
        # test_no_ambiguous_get_alias_was_restored pins that across every repository here: the
        # ones that read a tenant's row are `get_for_owner`, the one that deliberately does not
        # scope is `get_internal`, and this one reads an organisation BY ITS ID. The caller that
        # decides whether the id may be shown at all is application/account_service.
        return await db.get(Organization, organization_id)

    async def get_by_stripe_customer(self, db: AsyncSession,
                                     stripe_customer_id: str) -> Organization | None:
        # THE WEBHOOK'S ONLY ROUTE HOME. A Stripe event names a customer; it does not name an
        # organisation. This reads the unique index 0006_stripe_billing built, which is what makes
        # that lookup a single row rather than a scan.
        res = await db.execute(
            select(Organization)
            .where(Organization.stripe_customer_id == stripe_customer_id.strip())
            .limit(1))
        return res.scalar_one_or_none()

    async def attach_customer(self, db: AsyncSession, *, organization_id: uuid.UUID,
                              stripe_customer_id: str) -> None:
        row = await db.get(Organization, organization_id)
        if row is None:
            return
        row.stripe_customer_id = stripe_customer_id.strip()[:255]
        await db.flush()

    async def set_subscription(self, db: AsyncSession, *, organization_id: uuid.UUID,
                               subscription_id: str, plan: str | None, status: str,
                               current_period_end: datetime | None) -> None:
        # ONE WRITER for all four subscription columns, because they are one fact. Letting a caller
        # move `plan` without moving `subscription_status` is how a cancelled organisation keeps a
        # paid tier's limits - the two columns disagreeing IS the bug, so they are set together.
        #
        # `plan=None` MEANS LEAVE IT ALONE, and it is distinct from `plan=""` which CLEARS it. Both
        # states are real and they are opposites: a cancellation clears the plan, while an event for
        # a subscription created by hand in the Stripe dashboard carries no plan metadata and must
        # not be allowed to wipe the tier an enterprise customer is paying for. Collapsing them onto
        # the empty string makes the second case silently perform the first.
        row = await db.get(Organization, organization_id)
        if row is None:
            return
        row.stripe_subscription_id = (subscription_id or "").strip()[:255] or None
        if plan is not None:
            row.plan = plan.strip().lower()[:64]
        row.subscription_status = (status or "").strip()[:32]
        row.current_period_end = current_period_end
        await db.flush()

    async def list_with_billing(self, db: AsyncSession, *, limit: int = 100
                                ) -> list[tuple[Organization, int]]:
        """Every organisation with its credit balance. CROSS-TENANT: admin routes only."""
        # ONE QUERY, not a balance lookup per row. The ledger is summed in an OUTER JOIN so an
        # organisation that has never had an entry still appears - with 0 rather than absent, which
        # is the whole point of an operator's list: the tenants with nothing are the interesting ones.
        from meshpipeline.persistence.models import CreditLedgerEntry

        balance = (
            select(CreditLedgerEntry.organization_id.label("org"),
                   func.coalesce(func.sum(CreditLedgerEntry.amount), 0).label("balance"))
            .group_by(CreditLedgerEntry.organization_id)
            .subquery())
        res = await db.execute(
            select(Organization, func.coalesce(balance.c.balance, 0))
            .outerjoin(balance, balance.c.org == Organization.id)
            .order_by(Organization.created_at.desc())
            .limit(limit))
        return [(row[0], int(row[1] or 0)) for row in res.all()]
