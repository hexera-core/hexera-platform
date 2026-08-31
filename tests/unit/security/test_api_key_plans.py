# Responsibility: Verify a plan raises or lowers the limits it declares, and that a caller without one keeps today's.
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

import meshpipeline.settings.policy as polcfg
import meshpipeline.settings.runtime as rtcfg
from meshpipeline.application.job_service import JobService
from meshpipeline.settings import plans


def _globals(jobs=5, concurrent=20, rate=240):
    return (patch.object(polcfg, "MAX_JOBS_PER_OWNER", jobs),
            patch.object(polcfg, "MAX_CONCURRENT_JOBS", concurrent),
            patch.object(rtcfg, "RATE_LIMIT_PER_MINUTE", rate))


# the defaults ARE the current global environment values

@pytest.mark.parametrize("plan", [None, "", "   ", "default", "a-plan-nobody-declared"])
def test_an_absent_or_undeclared_plan_yields_exactly_the_global_values(plan):
    a, b, c = _globals(jobs=7, concurrent=31, rate=99)
    with a, b, c, patch.dict(plans.PLANS, {}, clear=True):
        limits = plans.limits_for(plan)
    assert limits.max_jobs_per_owner == 7
    assert limits.max_concurrent_jobs == 31
    assert limits.rate_limit_per_minute == 99


def test_the_shipped_catalogue_declares_no_plan_that_changes_behaviour():
    # Numbers per plan are a product decision that has not been made; until it is, every plan
    # resolves to the deployment's own configuration.
    a, b, c = _globals()
    with a, b, c:
        assert all(plans.limits_for(name) == plans.default_limits() for name in plans.PLANS)


# a declared plan overrides, field by field

def test_a_declared_plan_overrides_the_global_values():
    a, b, c = _globals(jobs=5, concurrent=20, rate=240)
    override = plans.PlanOverride(max_jobs_per_owner=50, max_concurrent_jobs=500,
                                  rate_limit_per_minute=6000)
    with a, b, c, patch.dict(plans.PLANS, {"scale": override}, clear=True):
        limits = plans.limits_for("scale")
    assert (limits.max_jobs_per_owner, limits.max_concurrent_jobs,
            limits.rate_limit_per_minute) == (50, 500, 6000)


def test_a_partial_plan_leaves_the_fields_it_does_not_declare_at_the_default():
    a, b, c = _globals(jobs=5, concurrent=20, rate=240)
    with a, b, c, patch.dict(plans.PLANS, {"trial": plans.PlanOverride(max_jobs_per_owner=1)},
                             clear=True):
        limits = plans.limits_for("trial")
    assert limits.max_jobs_per_owner == 1
    assert limits.max_concurrent_jobs == 20
    assert limits.rate_limit_per_minute == 240


def test_a_plan_name_is_matched_case_and_whitespace_insensitively():
    a, b, c = _globals()
    with a, b, c, patch.dict(plans.PLANS, {"scale": plans.PlanOverride(max_jobs_per_owner=9)},
                             clear=True):
        assert plans.limits_for("  SCALE ").max_jobs_per_owner == 9


# the quota gate reads the plan, and defaults to today's behaviour without one

def _counts(user_active: int, total_active: int):
    return (patch("meshpipeline.application.job_service.job_repo.count_active_for_owner",
                  new=AsyncMock(return_value=user_active)),
            patch("meshpipeline.application.job_service.job_repo.count_total_active",
                  new=AsyncMock(return_value=total_active)))


async def test_a_plan_raises_the_per_owner_job_limit():
    a, b, c = _globals(jobs=2, concurrent=10)
    counted, total = _counts(5, 5)
    with a, b, c, counted, total, patch.dict(
            plans.PLANS, {"scale": plans.PlanOverride(max_jobs_per_owner=10)}, clear=True):
        await JobService().check_quotas(AsyncMock(), "tenant-a", plan="scale")


async def test_a_plan_lowers_the_per_owner_job_limit():
    a, b, c = _globals(jobs=5, concurrent=10)
    counted, total = _counts(1, 1)
    with a, b, c, counted, total, patch.dict(
            plans.PLANS, {"trial": plans.PlanOverride(max_jobs_per_owner=1)}, clear=True):
        with pytest.raises(ValueError, match="active job"):
            await JobService().check_quotas(AsyncMock(), "tenant-a", plan="trial")


async def test_a_plan_raises_the_whole_system_concurrency_limit():
    a, b, c = _globals(jobs=5, concurrent=10)
    counted, total = _counts(0, 10)
    with a, b, c, counted, total, patch.dict(
            plans.PLANS, {"scale": plans.PlanOverride(max_concurrent_jobs=100)}, clear=True):
        await JobService().check_quotas(AsyncMock(), "tenant-a", plan="scale")


async def test_without_a_plan_the_quota_gate_behaves_exactly_as_it_does_today():
    a, b, c = _globals(jobs=2, concurrent=10)
    counted, total = _counts(2, 2)
    with a, b, c, counted, total, patch.dict(plans.PLANS, {}, clear=True):
        with pytest.raises(ValueError, match="active job"):
            await JobService().check_quotas(AsyncMock(), "tenant-a")


async def test_the_refusal_quotes_the_plan_s_limit_not_the_global_one():
    a, b, c = _globals(jobs=5, concurrent=10)
    counted, total = _counts(1, 1)
    with a, b, c, counted, total, patch.dict(
            plans.PLANS, {"trial": plans.PlanOverride(max_jobs_per_owner=1)}, clear=True):
        with pytest.raises(ValueError, match="limit: 1 per user"):
            await JobService().check_quotas(AsyncMock(), "tenant-a", plan="trial")


# the transport limiter reads the same authority

def test_the_unauthenticated_transport_limit_is_the_default_plan_s_rate():
    a, b, c = _globals(rate=137)
    with a, b, c:
        assert plans.limits_for(None).rate_limit_per_minute == 137
        assert plans.default_limits().rate_limit_per_minute == 137
