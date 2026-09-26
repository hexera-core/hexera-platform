# Responsibility: Verify a signup grant is a budget and not a free tier - a tenant with no paid plan
#                 is refused the job its balance cannot cover, and a paying one never is.
from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest

import meshpipeline.settings.policy as polcfg
from meshpipeline.application import credit_service, spend_gate

ORG = uuid.uuid4()


def _org(*, plan="", status=""):
    return SimpleNamespace(id=ORG, plan=plan, subscription_status=status)


@pytest.fixture
def tenant(monkeypatch):
    state = {"org": _org(), "balance": 100, "running": 0}

    async def _get_by_id(db, organization_id):
        return state["org"] if organization_id == ORG else None

    async def _balance(db, *, organization_id):
        return state["balance"]

    async def _running(db, organization_id):
        # PER ORGANISATION: every member spends the same balance.
        assert organization_id == ORG
        return state["running"]

    monkeypatch.setattr(spend_gate.organization_repo, "get_by_id", _get_by_id)
    monkeypatch.setattr(spend_gate.job_repo, "count_active_for_organization", _running)
    monkeypatch.setattr(credit_service, "balance", _balance)
    monkeypatch.setattr(polcfg, "JOB_BASE_CREDITS", 10)
    monkeypatch.setattr(polcfg, "CREDIT_GATE_ENABLED", True)
    return state


async def _admit(organization_id=str(ORG)):
    return await spend_gate.admit(None, owner_id="a@example.com", organization_id=organization_id)


# A TENANT WITH NO PAID PLAN

@pytest.mark.asyncio
async def test_a_balance_that_covers_a_run_is_admitted_on_the_free_limits(tenant):
    assert await _admit() == ""


@pytest.mark.asyncio
async def test_an_exhausted_balance_is_refused(tenant):
    tenant["balance"] = 9
    with pytest.raises(spend_gate.OutOfCredits, match="out of credits"):
        await _admit()


@pytest.mark.asyncio
async def test_a_negative_balance_is_refused(tenant):
    # The last run's minutes can take the ledger below zero; the next run must not start on debt.
    tenant["balance"] = -4
    with pytest.raises(spend_gate.OutOfCredits):
        await _admit()


@pytest.mark.asyncio
async def test_a_running_job_holds_back_its_base_charge(tenant):
    # Charged at the END, so without the reservation a balance that covers one run would admit as
    # many as the quota allows - and the count is the organisation's, because a teammate's run
    # spends the same balance.
    tenant["balance"], tenant["running"] = 15, 1
    with pytest.raises(spend_gate.OutOfCredits, match="already in progress"):
        await _admit()
    tenant["balance"] = 20
    assert await _admit() == ""


@pytest.mark.asyncio
async def test_an_out_of_credits_refusal_is_a_quota_refusal_to_existing_callers(tenant):
    # Every caller already maps ValueError to its own quota answer; this must land there too.
    tenant["balance"] = 0
    with pytest.raises(ValueError):
        await _admit()


@pytest.mark.asyncio
async def test_a_disabled_gate_admits_anyone(tenant, monkeypatch):
    monkeypatch.setattr(polcfg, "CREDIT_GATE_ENABLED", False)
    tenant["balance"] = -1_000
    assert await _admit() == ""


@pytest.mark.asyncio
async def test_a_cancelled_subscription_is_held_to_its_balance(tenant):
    # The webhook clears the plan on deletion, but a row that kept a tier beside a dead status is
    # not a paying customer either.
    tenant["org"], tenant["balance"] = _org(plan="starter", status="canceled"), 0
    with pytest.raises(spend_gate.OutOfCredits):
        await _admit()


# A PAYING TENANT

@pytest.mark.parametrize("status", ["active", "trialing", "past_due", ""])
@pytest.mark.asyncio
async def test_a_paying_tenant_is_never_refused_for_balance(tenant, status):
    # Its allowance running out is what the metered overage price bills; `past_due` keeps serving
    # while the provider retries the card, and an empty status is an operator-set invoiced tier.
    tenant["org"], tenant["balance"] = _org(plan="Team", status=status), -5_000
    assert await _admit() == "team"


@pytest.mark.asyncio
async def test_the_plan_returned_is_what_the_quota_counts_against(tenant):
    tenant["org"] = _org(plan="starter", status="active")
    assert await _admit() == "starter"


# NO ORGANISATION TO BILL

@pytest.mark.parametrize("organization_id", ["", "not-a-uuid", str(uuid.uuid4())])
@pytest.mark.asyncio
async def test_a_caller_with_no_billable_organisation_is_admitted(tenant, organization_id):
    # Nothing would be debited at the job's end either - metering_service skips a job with no
    # tenant - so refusing here protects nothing and breaks the owner-only posture.
    tenant["balance"] = -1_000
    assert await _admit(organization_id) == ""


def test_admissions_for_one_tenant_are_serialised_before_anything_is_read():
    # Without the lock two approvals read the same balance and running count before either has
    # created its job. Order matters: the lock must come before both reads.
    import inspect
    source = inspect.getsource(spend_gate.admit)
    lock = source.index("_serialise_admissions")
    assert lock < source.index("count_active_for_organization")
    assert lock < source.index("credit_service.balance")
