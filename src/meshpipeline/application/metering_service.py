# Responsibility: Charge an organisation for the work one job actually did, and report that
#                 consumption to the billing provider's meter once it is durable.
# Owns: what a job costs in credits, and the two-phase split between charging and reporting.
# Boundaries: it writes ledger entries through credit_service and talks to the provider through the
#             gateway CONTRACT. It decides no price in money - that is the provider's price object.
from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

import meshpipeline.settings.policy as polcfg
from meshpipeline.application import credit_service
from meshpipeline.contracts.billing import BillingUnavailable, get_billing_gateway
from meshpipeline.persistence.models import CreditEntryType, CreditLedgerEntry, JobStatus
from meshpipeline.persistence.repositories.organization_repository import (
    OrganizationRepository,
)

log = logging.getLogger(__name__)

organization_repo = OrganizationRepository()

#: How a job's charge is named in the ledger, so a person reading their own history can match an
#: entry to the run that caused it. The job id is appended by `charge_for_job`.
JOB_REASON_PREFIX = "mesh job"

#: HOW MANY unreported debits one sweep will carry. Bounded because the sweep runs on a schedule and
#: must be interruptible: a backlog that built up over an outage is drained across several runs
#: rather than in one transaction that holds a connection for minutes and may time out half way.
SWEEP_BATCH = 200


def credits_for_job(*, started_at: datetime | None, ended_at: datetime | None) -> int:
    """What one succeeded job costs. Pure, so the price can be asserted without a database."""
    base = polcfg.JOB_BASE_CREDITS
    per_minute = polcfg.CREDITS_PER_MESH_MINUTE
    if started_at is None or ended_at is None or per_minute <= 0:
        # A JOB WITH NO MEASURED SPAN PAYS THE BASE CHARGE ONLY. Both timestamps are nullable, and a
        # row missing one is a job that crossed a revision which did not set it. Guessing a duration
        # would invent the one number on the invoice a customer can check against their own clock.
        return max(base, 0)
    elapsed = (ended_at - started_at).total_seconds()
    if elapsed <= 0:
        # A NON-POSITIVE SPAN is a clock that moved backwards between two writes, not a free job.
        # It is charged as the base rather than refunded into a negative cost.
        return max(base, 0)
    # ROUNDED UP, so a forty-second mesh is one minute rather than zero. Rounding down would make
    # every short job free, which is most of them on a well-behaved geometry.
    minutes = int(-(-elapsed // 60))
    return max(base + minutes * per_minute, 0)


async def charge_for_job(db: AsyncSession, *, job) -> int:
    """Write the debit for one finished job. Returns the credits charged; 0 when nothing was."""
    # ONLY A SUCCEEDED JOB IS CHARGED. The pipeline has several failure paths that can burn hours -
    # FailedReason names four of them - and charging for those would make this product's worst days
    # its most expensive ones for the customer. It is also what makes a refund path unnecessary for
    # the ordinary failure, which is the failure that actually happens.
    if getattr(job, "status", None) != JobStatus.succeeded:
        return 0
    # READ THROUGH getattr, because this is called from the terminal transaction with whatever row
    # the job repository returned. A row shape this function does not recognise must resolve to
    # "not chargeable" rather than to an AttributeError - the caller's fail-open would swallow that
    # exception, but swallowing it means a silent revenue hole instead of an explicit refusal.
    if getattr(job, "organization_id", None) is None:
        # A JOB THAT NAMES NO TENANT CANNOT BE BILLED TO ONE. The column is nullable through the
        # tenancy migration, so this is a real row and not a fault - it is logged and left uncharged
        # rather than billed to a guess.
        log.info("job %s has no organisation; not charged", getattr(job, "id", "?"))
        return 0

    amount = credits_for_job(started_at=getattr(job, "started_at", None),
                             ended_at=getattr(job, "ended_at", None))
    if amount <= 0:
        return 0
    await credit_service.debit(db, organization_id=job.organization_id, amount=amount,
                               reason=f"{JOB_REASON_PREFIX} {getattr(job, 'id', '?')}"[:128])
    return amount


async def report_pending_usage(db: AsyncSession, *, limit: int = SWEEP_BATCH) -> dict:
    """Report unmetered debits to the provider's meter and stamp them. Safe to run twice."""
    try:
        gateway = get_billing_gateway()
    except BillingUnavailable:
        # A DEPLOYMENT THAT DOES NOT CHARGE still writes debits - the ledger is how an operator sees
        # what a tenant consumed, with or without a provider behind it. Nothing to report is not an
        # error, so the sweep says so and stops.
        return {"reported": 0, "skipped": 0, "billing": "unconfigured"}

    rows = (await db.execute(
        select(CreditLedgerEntry)
        .where(CreditLedgerEntry.metered_at.is_(None),
               CreditLedgerEntry.entry_type == CreditEntryType.debit)
        .order_by(CreditLedgerEntry.created_at.asc())
        .limit(limit))).scalars().all()

    reported: list[uuid.UUID] = []
    skipped = 0
    for row in rows:
        organization = await organization_repo.get_by_id(db, row.organization_id)
        if organization is None or not organization.stripe_customer_id:
            # NOT AN ERROR AND NOT REPORTABLE: an organisation that never checked out has no
            # customer to meter against. It is left UNSTAMPED on purpose, so that if it subscribes
            # later the consumption it already had is still there to report.
            skipped += 1
            continue
        try:
            gateway.report_usage(
                customer_id=organization.stripe_customer_id,
                # THE DEBIT IS STORED NEGATED; a meter takes a positive quantity.
                quantity=abs(row.amount),
                # THE LEDGER ROW ID, so a retried sweep presents the same idempotency key and the
                # provider replays rather than adding the same consumption to the bill twice. This
                # is the whole reason the sweep is safe to interrupt.
                idempotency_scope=str(row.id))
        except Exception:
            # ONE UNREPORTABLE ROW MUST NOT STOP THE SWEEP. It stays unstamped and the next run
            # tries it again; stopping here would let a single poisoned row block every later
            # tenant's consumption from ever reaching the meter.
            log.exception("could not report ledger entry %s to the meter", row.id)
            skipped += 1
            continue
        reported.append(row.id)

    if reported:
        # STAMPED AFTER the provider accepted them, never before. The other order loses a charge
        # whenever the process dies between the two, and a lost charge is invisible - there is no
        # later signal that says a stamped row was never actually reported.
        await db.execute(
            update(CreditLedgerEntry)
            .where(CreditLedgerEntry.id.in_(reported))
            .values(metered_at=datetime.now(UTC)))

    return {"reported": len(reported), "skipped": skipped, "billing": "configured"}
