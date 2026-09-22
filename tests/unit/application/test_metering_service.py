# Responsibility: Verify a job is charged for what it did, once, and that the meter sweep is safe
#                 to interrupt and safe to run twice.
from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest

import meshpipeline.settings.policy as polcfg
from meshpipeline.application import credit_service, metering_service
from meshpipeline.contracts import billing as billing_contract
from meshpipeline.persistence.models import JobStatus

NOW = datetime(2026, 9, 22, 12, 0, tzinfo=UTC)


class _Job:
    def __init__(self, *, status=JobStatus.succeeded, organization_id=None,
                 started_at=NOW, ended_at=NOW + timedelta(minutes=5)):
        self.id = uuid.uuid4()
        self.status = status
        self.organization_id = organization_id or uuid.uuid4()
        self.started_at = started_at
        self.ended_at = ended_at


@pytest.fixture
def debits(monkeypatch):
    recorded: list[dict] = []

    async def _debit(db, *, organization_id, amount, reason=""):
        recorded.append({"organization_id": organization_id, "amount": amount, "reason": reason})

    monkeypatch.setattr(credit_service, "debit", _debit)
    return recorded


@pytest.fixture
def prices(monkeypatch):
    monkeypatch.setattr(polcfg, "JOB_BASE_CREDITS", 10)
    monkeypatch.setattr(polcfg, "CREDITS_PER_MESH_MINUTE", 1)


# WHAT A JOB COSTS

def test_a_job_pays_the_base_charge_plus_its_minutes(prices):
    assert metering_service.credits_for_job(
        started_at=NOW, ended_at=NOW + timedelta(minutes=5)) == 15


def test_a_part_minute_rounds_up(prices):
    # Rounding DOWN would make every short job free, which is most of them on a well-behaved
    # geometry - the base charge would become the entire price of the product.
    assert metering_service.credits_for_job(
        started_at=NOW, ended_at=NOW + timedelta(seconds=40)) == 11


def test_a_job_with_no_measured_span_pays_only_the_base(prices):
    # Both timestamps are nullable. Guessing a duration would invent the one number on the invoice
    # a customer can check against their own clock.
    assert metering_service.credits_for_job(started_at=None, ended_at=NOW) == 10
    assert metering_service.credits_for_job(started_at=NOW, ended_at=None) == 10


def test_a_clock_that_moved_backwards_is_charged_the_base_not_refunded(prices):
    # A negative span is a clock disagreeing with itself between two writes, not a free job - and
    # certainly not a credit back.
    assert metering_service.credits_for_job(
        started_at=NOW, ended_at=NOW - timedelta(minutes=5)) == 10


# WHO IS CHARGED

@pytest.mark.asyncio
async def test_a_succeeded_job_is_charged(debits, prices):
    job = _Job()
    charged = await metering_service.charge_for_job(None, job=job)
    assert charged == 15
    assert debits[0]["organization_id"] == job.organization_id
    # The reason names the run, so a person reading their own history can match an entry to it.
    assert str(job.id) in debits[0]["reason"]


@pytest.mark.parametrize("status", [JobStatus.failed, JobStatus.running, JobStatus.pending,
                                    JobStatus.queued, JobStatus.pending_review])
@pytest.mark.asyncio
async def test_only_a_succeeded_job_is_charged(debits, prices, status):
    # The pipeline has several failure paths that can burn hours. Charging for them would make this
    # product's worst days its most expensive ones for the customer.
    assert await metering_service.charge_for_job(None, job=_Job(status=status)) == 0
    assert debits == []


@pytest.mark.asyncio
async def test_a_job_with_no_organisation_is_not_billed_to_a_guess(debits, prices):
    job = _Job()
    job.organization_id = None
    assert await metering_service.charge_for_job(None, job=job) == 0
    assert debits == []


@pytest.mark.asyncio
async def test_a_zero_price_writes_nothing(debits, monkeypatch):
    # The ledger's CHECK refuses a zero amount, so a deployment that prices jobs at nothing must not
    # reach the insert at all.
    monkeypatch.setattr(polcfg, "JOB_BASE_CREDITS", 0)
    monkeypatch.setattr(polcfg, "CREDITS_PER_MESH_MINUTE", 0)
    assert await metering_service.charge_for_job(None, job=_Job()) == 0
    assert debits == []


# THE METER SWEEP

class _Row:
    def __init__(self, *, amount=-15, organization_id=None):
        self.id = uuid.uuid4()
        self.amount = amount
        self.organization_id = organization_id or uuid.uuid4()


class _Result:
    def __init__(self, rows):
        self._rows = rows

    def scalars(self):
        return self

    def all(self):
        return self._rows


class _Db:
    def __init__(self, rows):
        self.rows = rows
        self.updates: list = []

    async def execute(self, statement):
        text = str(statement)
        if text.lstrip().upper().startswith("UPDATE"):
            self.updates.append(statement)
            return None
        return _Result(self.rows)


class _Org:
    def __init__(self, customer="cus_1"):
        self.stripe_customer_id = customer


class _Gateway:
    def __init__(self, *, failing_on=None):
        self.reported: list = []
        self.failing_on = failing_on

    def report_usage(self, *, customer_id, quantity, idempotency_scope):
        if self.failing_on is not None and idempotency_scope == self.failing_on:
            raise RuntimeError("the provider refused")
        self.reported.append({"customer_id": customer_id, "quantity": quantity,
                              "idempotency_scope": idempotency_scope})


@pytest.fixture(autouse=True)
def _clear_gateway():
    yield
    billing_contract.set_billing_gateway(None)


@pytest.fixture
def orgs(monkeypatch):
    class _Repo:
        organization = _Org()

        async def get_by_id(self, db, organization_id):
            return self.organization

    repo = _Repo()
    monkeypatch.setattr(metering_service, "organization_repo", repo)
    return repo


@pytest.mark.asyncio
async def test_a_deployment_without_billing_reports_nothing_and_does_not_fail(orgs):
    billing_contract.set_billing_gateway(None)
    out = await metering_service.report_pending_usage(_Db([_Row()]))
    assert out == {"reported": 0, "skipped": 0, "billing": "unconfigured"}


@pytest.mark.asyncio
async def test_a_debit_is_reported_as_a_positive_quantity(orgs):
    gateway = _Gateway()
    billing_contract.set_billing_gateway(gateway)
    row = _Row(amount=-15)
    await metering_service.report_pending_usage(_Db([row]))
    assert gateway.reported[0]["quantity"] == 15
    # THE LEDGER ROW ID IS THE IDEMPOTENCY KEY, so a retried sweep replays rather than adding the
    # same consumption to the bill a second time. Meters aggregate; a duplicate is a real overcharge.
    assert gateway.reported[0]["idempotency_scope"] == str(row.id)


@pytest.mark.asyncio
async def test_rows_are_stamped_only_after_the_provider_accepted_them(orgs):
    billing_contract.set_billing_gateway(_Gateway())
    db = _Db([_Row()])
    out = await metering_service.report_pending_usage(db)
    assert out["reported"] == 1
    assert len(db.updates) == 1


@pytest.mark.asyncio
async def test_one_unreportable_row_does_not_stop_the_sweep(orgs):
    # Stopping on the first failure would let a single poisoned row block every later tenant's
    # consumption from ever reaching the meter.
    first, second = _Row(), _Row()
    gateway = _Gateway(failing_on=str(first.id))
    billing_contract.set_billing_gateway(gateway)
    out = await metering_service.report_pending_usage(_Db([first, second]))
    assert out == {"reported": 1, "skipped": 1, "billing": "configured"}
    assert [r["idempotency_scope"] for r in gateway.reported] == [str(second.id)]


@pytest.mark.asyncio
async def test_an_organisation_with_no_customer_is_left_unstamped(orgs):
    # Left UNSTAMPED on purpose: if that tenant subscribes later, the consumption it already had is
    # still there to report rather than having been silently written off.
    orgs.organization = _Org(customer=None)
    gateway = _Gateway()
    billing_contract.set_billing_gateway(gateway)
    db = _Db([_Row()])
    out = await metering_service.report_pending_usage(db)
    assert out["skipped"] == 1
    assert gateway.reported == []
    assert db.updates == []


# THE FAIL-OPEN, which is the one place in billing that swallows an error

@pytest.mark.asyncio
async def test_charging_never_raises_on_a_row_shape_it_does_not_recognise():
    # `charge_for_job` is called from inside the transaction that makes a job terminal. An exception
    # escaping it rolls that back, so a finished four-hour mesh would be stuck as `running` until
    # the reaper marked it failed - the customer loses the work AND is told it broke.
    #
    # The caller wraps this in a fail-open, but a row it cannot read must resolve to "not chargeable"
    # HERE rather than relying on that: swallowing an AttributeError upstream turns an explicit
    # refusal into a silent revenue hole nobody is looking for.
    import types

    assert await metering_service.charge_for_job(None, job=types.SimpleNamespace()) == 0


@pytest.mark.asyncio
async def test_the_fail_open_handler_cannot_itself_raise(caplog):
    # A handler whose LOGGING raises escapes the except block and rolls back the transaction - which
    # is exactly the outcome the fail-open exists to prevent, arriving through the code meant to
    # prevent it. This drives a row that has no `.id` at all through the real handler.
    import types

    from meshpipeline.application import terminal_finalize

    async def _boom(db, *, job):
        raise RuntimeError("metering is broken")

    original = metering_service.charge_for_job
    metering_service.charge_for_job = _boom
    try:
        await terminal_finalize._charge_fail_open(None, types.SimpleNamespace())
    finally:
        metering_service.charge_for_job = original
