# Responsibility: Decide whether a tenant may start a job it would be charged for, and which plan's
#                 limits that job is held to.
# Owns: the rule that signup credits are a budget, not a free tier - a tenant without a live
#       subscription cannot start work its balance does not cover.
# Boundaries: it reads the organisation and the ledger; it writes nothing. The charge itself is
#             metering_service's, at the job's end; the per-plan counts are JobService.check_quotas.
from __future__ import annotations

import logging
import uuid

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

import meshpipeline.settings.policy as polcfg
from meshpipeline.application import credit_service
from meshpipeline.persistence.repositories.job_repository import JobRepository
from meshpipeline.persistence.repositories.organization_repository import (
    OrganizationRepository,
)

log = logging.getLogger(__name__)

organization_repo = OrganizationRepository()
job_repo = JobRepository()

#: THE SUBSCRIPTION STATES THAT STILL PAY FOR OVERAGE. `past_due` is in on purpose: the provider is
#: retrying the card on its own dunning schedule, and billing_service deliberately keeps serving
#: until `customer.subscription.deleted` - refusing jobs here would cut the customer off at the
#: first failed retry by a second route. An EMPTY status beside a plan is an operator-set tier
#: (an invoiced enterprise account), which has no provider subscription to report a status.
PAYING_STATUSES = frozenset({"active", "trialing", "past_due", ""})


class OutOfCredits(ValueError):
    """A new job was refused because nothing would pay for it.

    A ValueError so every caller that already maps a quota refusal to its own answer - the
    conversation's `quota_exceeded`, the dispute route's 429 - maps this one too without a new
    branch at each site.
    """


def paying_plan(organization) -> str:
    """The plan an organisation is billed on, or "" when nothing is billing it."""
    plan = (getattr(organization, "plan", "") or "").strip().lower()
    if not plan:
        return ""
    status = (getattr(organization, "subscription_status", "") or "").strip().lower()
    return plan if status in PAYING_STATUSES else ""


async def admit(db: AsyncSession, *, owner_id: str, organization_id: str) -> str:
    """Refuse a job this tenant cannot pay for; otherwise return the plan whose limits apply."""
    organization = await _organization(db, organization_id)
    if organization is None:
        # NO ORGANISATION, NO LEDGER. Every account provisioned since 0003 has one, so this is the
        # owner-only posture 0004's backfill left behind or a dev deployment with no tenancy. There
        # is nothing to debit at the job's end either (metering_service skips it), so there is
        # nothing to protect by refusing here.
        return ""

    plan = paying_plan(organization)
    if plan:
        # A PAYING TENANT IS NEVER REFUSED FOR BALANCE. Its allowance running out is exactly what
        # the metered overage price exists to bill; the balance going negative is the overage.
        return plan
    if not polcfg.CREDIT_GATE_ENABLED:
        return ""

    # ONE ADMISSION AT A TIME PER TENANT. Two approvals - two sessions, or two members - would
    # otherwise both read the same balance and the same running count before either had created
    # its job, and a balance covering one run would admit both. The lock is transaction-scoped and
    # every caller creates its job in the SAME transaction, so the second admission waits until
    # the first job exists and is counted below.
    await _serialise_admissions(db, organization.id)

    # RESERVE FOR WHAT IS ALREADY RUNNING, across the whole organisation. A job is charged when it
    # ENDS, so a balance read alone would admit as many concurrent jobs as the quota allows against
    # a balance that covers one - and every member draws on the same balance. Each running job
    # holds back at least the base charge; the minutes it will add are unknowable here, which is why
    # the ledger can still go a little negative and the next job is refused until it is topped up.
    per_job = max(polcfg.JOB_BASE_CREDITS, 1)
    running = await job_repo.count_active_for_organization(db, organization.id)
    balance = await credit_service.balance(db, organization_id=organization.id)
    if balance - running * per_job >= per_job:
        return ""

    log.info("refused a job for organisation %s: balance %s does not cover a run",
             organization.id, balance)
    if running:
        raise OutOfCredits(
            "Your remaining credits are held by the run already in progress. Wait for it to "
            "finish, or choose a plan under Billing to keep running meshes.")
    raise OutOfCredits(
        "You are out of credits, so I did not start this run. Choose a plan under Billing to "
        "keep running meshes.")


async def _serialise_admissions(db: AsyncSession, organization_id) -> None:
    # The same transaction-scoped advisory lock JobService.check_quotas takes per owner, keyed on
    # the tenant instead. A no-op off Postgres, where the unit tier runs.
    bind = getattr(db, "bind", None)
    if bind is None or bind.dialect.name != "postgresql":
        return
    await db.execute(text("SELECT pg_advisory_xact_lock(hashtext(:k))"),
                     {"k": f"credit-admission:{organization_id}"})


async def _organization(db: AsyncSession, organization_id: str):
    if not organization_id:
        return None
    try:
        key = uuid.UUID(str(organization_id))
    except ValueError:
        return None
    return await organization_repo.get_by_id(db, key)
