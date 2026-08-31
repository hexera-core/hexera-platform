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


@dataclass(frozen=True)
class PlanOverride:
    # Each field is optional because a plan states only what it changes; anything it leaves out is
    # the deployment's configured value, not a second copy of it that can drift.
    max_jobs_per_owner: int | None = None
    max_concurrent_jobs: int | None = None
    rate_limit_per_minute: int | None = None


# THE PLAN CATALOGUE, deliberately empty. The mechanism ships now so a key can carry a plan and the
# quota gate can honour it; what a paid tier is actually allowed to consume is a product decision
# that has not been made, and inventing numbers here would make it look as though it had. An empty
# catalogue means every caller - keyed or not - gets exactly the limits this deployment configures,
# which is the behaviour that exists today.
PLANS: dict[str, PlanOverride] = {}


def default_limits() -> PlanLimits:
    # Read through the modules rather than bound at import: these are the same globals operators
    # set today, and a test or a reload that changes one must change the answer here too.
    return PlanLimits(
        max_jobs_per_owner=polcfg.MAX_JOBS_PER_OWNER,
        max_concurrent_jobs=polcfg.MAX_CONCURRENT_JOBS,
        rate_limit_per_minute=rtcfg.RATE_LIMIT_PER_MINUTE,
    )


def limits_for(plan: str | None) -> PlanLimits:
    # An unknown plan name resolves to the defaults rather than refusing. A row naming a plan this
    # revision does not declare is a rollback or a half-finished product change, and neither is a
    # reason to stop serving a paying caller.
    base = default_limits()
    override = PLANS.get((plan or "").strip().lower())
    if override is None:
        return base
    return PlanLimits(
        max_jobs_per_owner=_or(override.max_jobs_per_owner, base.max_jobs_per_owner),
        max_concurrent_jobs=_or(override.max_concurrent_jobs, base.max_concurrent_jobs),
        rate_limit_per_minute=_or(override.rate_limit_per_minute, base.rate_limit_per_minute),
    )


def _or(declared: int | None, default: int) -> int:
    return default if declared is None else declared
