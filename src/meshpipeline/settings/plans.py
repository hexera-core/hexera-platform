# Responsibility: Answer what one plan is allowed to consume, with the deployment's own configuration as the floor.
# Boundaries: numbers only; who holds a plan is identity, and enforcing a number is the quota gate and the limiter.
from __future__ import annotations

from dataclasses import dataclass

import meshpipeline.settings.policy as polcfg
import meshpipeline.settings.runtime as rtcfg


@dataclass(frozen=True)
class PlanLimits:
    max_jobs_per_owner: int
    max_concurrent_jobs: int
    rate_limit_per_minute: int
    #: CREDITS INCLUDED in the tier's flat fee each period. Everything above this is overage, metered
    #: and billed in arrears. Zero means the tier includes nothing - the honest value for a caller
    #: with no plan, who is spending a signup grant rather than an allowance.
    included_credits: int = 0


@dataclass(frozen=True)
class PlanOverride:
    # Each field is optional because a plan states only what it changes; anything it leaves out is
    # the deployment's configured value, not a second copy of it that can drift.
    max_jobs_per_owner: int | None = None
    max_concurrent_jobs: int | None = None
    rate_limit_per_minute: int | None = None
    included_credits: int | None = None


# THE PLAN CATALOGUE. It was deliberately empty until the charging model was decided; the shape
# chosen is HYBRID - a flat tier carrying an allowance, with metered overage above it.
#
# WHAT EACH TIER DECLARES AND WHAT IT DOES NOT. A tier states only what it CHANGES. Anything left
# out stays the deployment's configured value rather than becoming a second copy that drifts - the
# reason PlanOverride's fields are all optional. That is why no tier restates rate_limit_per_minute
# it does not mean to move.
#
# THE NUMBERS BELOW ARE PLACEHOLDERS AND ARE MEANT TO BE ARGUED WITH. They are internally
# consistent - each tier is a strict superset of the one beneath it - but no pricing research
# produced them, and `included_credits` in particular must be set against the measured cost of a
# mesh job before anyone is charged. They live here, in configuration-shaped code, precisely so
# that changing them is a one-line diff and not a migration.
PLANS: dict[str, PlanOverride] = {
    "starter": PlanOverride(
        max_jobs_per_owner=50,
        max_concurrent_jobs=3,
        included_credits=1_000,
    ),
    "team": PlanOverride(
        max_jobs_per_owner=500,
        max_concurrent_jobs=10,
        rate_limit_per_minute=600,
        included_credits=10_000,
    ),
    # ENTERPRISE IS NOT PURCHASABLE and has no price id on purpose: it is invoiced, not checked out.
    # settings/billing.py resolves no price for this name, so a checkout naming it is refused rather
    # than half-completed - the plan is set by the operator who raises the invoice.
    "enterprise": PlanOverride(
        max_jobs_per_owner=10_000,
        max_concurrent_jobs=50,
        rate_limit_per_minute=3_000,
        included_credits=100_000,
    ),
}


def default_limits() -> PlanLimits:
    # Read through the modules rather than bound at import: these are the same globals operators
    # set today, and a test or a reload that changes one must change the answer here too.
    return PlanLimits(
        max_jobs_per_owner=polcfg.MAX_JOBS_PER_OWNER,
        max_concurrent_jobs=polcfg.MAX_CONCURRENT_JOBS,
        rate_limit_per_minute=rtcfg.RATE_LIMIT_PER_MINUTE,
        # NO PLAN INCLUDES ANYTHING. A caller without a tier is spending a signup grant, not an
        # allowance, and reporting a non-zero included figure here would make the overage reconciler
        # forgive usage that nobody paid for.
        included_credits=0,
    )


def limits_for(plan: str | None) -> PlanLimits:
    # An unknown plan name resolves to the defaults rather than refusing. A row naming a plan this
    # revision does not declare is a rollback or a half-finished product change, and neither is a
    # reason to stop serving a paying caller.
    base = default_limits()
    # A NON-STRING plan is treated as no plan, deliberately. A route declares
    # `plan: str = Depends(plan_dep)`, and that default is a FastAPI SENTINEL which only the
    # framework replaces - so every caller that invokes a route function DIRECTLY (the integration
    # tier does, and so does any script) hands the sentinel straight through to here. Calling
    # .strip() on it raised AttributeError and the route answered 500, which read as a broken
    # upload rather than a plan that was never resolved. Defaults are the right answer for a
    # caller that never had a plan to begin with.
    if not isinstance(plan, str):
        return base
    override = PLANS.get(plan.strip().lower())
    if override is None:
        return base
    return PlanLimits(
        max_jobs_per_owner=_or(override.max_jobs_per_owner, base.max_jobs_per_owner),
        max_concurrent_jobs=_or(override.max_concurrent_jobs, base.max_concurrent_jobs),
        rate_limit_per_minute=_or(override.rate_limit_per_minute, base.rate_limit_per_minute),
        included_credits=_or(override.included_credits, base.included_credits),
    )


def _or(declared: int | None, default: int) -> int:
    return default if declared is None else declared
