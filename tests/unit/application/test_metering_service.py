# Responsibility: Verify a job is charged for what it did, once, and that the meter sweep is safe
#                 to interrupt and safe to run twice.
from __future__ import annotations

import types
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


class _Tenant:
    # THE ORGANISATION AND ITS BALANCE as the charge sees them. Defaults to a tenant with no paid
    # plan, whose debits are never overage.
    def __init__(self):
        self.plan, self.status, self.customer, self.balance = "", "", None, 0

    async def get_by_id(self, db, organization_id):
        return types.SimpleNamespace(id=organization_id, plan=self.plan,
                                     subscription_status=self.status,
                                     stripe_customer_id=self.customer)


@pytest.fixture
def tenant(monkeypatch):
    t = _Tenant()
    monkeypatch.setattr(metering_service, "organization_repo", t)

    async def _balance(db, *, organization_id):
        return t.balance

    monkeypatch.setattr(credit_service, "balance", _balance)
    return t


@pytest.fixture
def debits(monkeypatch, tenant):
    recorded: list[dict] = []

    async def _debit(db, *, organization_id, amount, reason="", overage=0):
        recorded.append({"organization_id": organization_id, "amount": amount, "reason": reason,
                         "overage": overage})

    async def _refund(db, *, organization_id, amount, reason=""):
        recorded.append({"organization_id": organization_id, "refund": amount, "reason": reason})

    monkeypatch.setattr(credit_service, "debit", _debit)
    monkeypatch.setattr(credit_service, "refund", _refund)
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


# WHICH PART OF A CHARGE IS OVERAGE

@pytest.mark.asyncio
async def test_a_tenant_with_no_paid_plan_never_accrues_overage(debits, prices, tenant):
    # No metered price exists for it: the balance goes negative and the credit gate refuses the
    # next run. Signup credits must never turn into a bill.
    tenant.customer, tenant.balance = "cus_1", -100
    await metering_service.charge_for_job(None, job=_Job())
    assert debits == [debits[0]] and debits[0]["overage"] == 0


@pytest.mark.asyncio
async def test_a_subscriber_whose_allowance_covers_the_run_accrues_none(debits, prices, tenant):
    # THE DOUBLE-BILL THIS FIXES: the flat fee already bought these credits, so reporting the run
    # to the metered price as well charged for it twice.
    tenant.plan, tenant.status, tenant.customer, tenant.balance = "starter", "active", "cus_1", 900
    await metering_service.charge_for_job(None, job=_Job())
    assert [d.get("overage") for d in debits] == [0]


@pytest.mark.asyncio
async def test_only_the_part_beyond_the_balance_is_overage_and_it_is_paid_back(
        debits, prices, tenant):
    tenant.plan, tenant.status, tenant.customer, tenant.balance = "starter", "active", "cus_1", 4
    job = _Job()
    await metering_service.charge_for_job(None, job=job)
    debit, refund = debits
    assert (debit["amount"], debit["overage"]) == (15, 11)
    # The overage is paid in money, so it returns to the balance as credits; otherwise the next
    # period's allowance would pay for it again after the invoice already had.
    assert refund["refund"] == 11 and str(job.id) in refund["reason"]


@pytest.mark.asyncio
async def test_a_subscriber_already_at_zero_is_billed_the_whole_run(debits, prices, tenant):
    tenant.plan, tenant.status, tenant.customer, tenant.balance = "team", "past_due", "cus_1", 0
    await metering_service.charge_for_job(None, job=_Job())
    assert debits[0]["overage"] == 15 and debits[1]["refund"] == 15


@pytest.mark.asyncio
async def test_an_invoiced_tier_with_no_billing_customer_accrues_no_overage(
        debits, prices, tenant):
    # Nobody to report it to: the operator reads the negative balance when raising the invoice, and
    # paying it back here would erase the one record of it.
    tenant.plan, tenant.status, tenant.customer, tenant.balance = "enterprise", "", None, 0
    await metering_service.charge_for_job(None, job=_Job())
    assert [d.get("overage") for d in debits] == [0]


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
    def __init__(self, *, amount=-15, overage=15, organization_id=None):
        self.id = uuid.uuid4()
        self.amount = amount
        self.overage = overage
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
async def test_the_overage_is_reported_not_the_whole_debit(orgs):
    # The allowance paid for the rest of the run; only the part beyond it goes to the metered price.
    gateway = _Gateway()
    billing_contract.set_billing_gateway(gateway)
    row = _Row(amount=-15, overage=6)
    await metering_service.report_pending_usage(_Db([row]))
    assert gateway.reported[0]["quantity"] == 6
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


def test_the_sweep_reads_only_reportable_overage():
    # STARVATION: 0007's sweep read the oldest 200 unmetered debits and skipped any without a
    # customer, leaving them unstamped - so once 200 such rows existed, every run re-read them and
    # reported nothing. The filter belongs in the query, and a covered debit is not in it at all.
    import inspect
    source = inspect.getsource(metering_service.report_pending_usage)
    assert "CreditLedgerEntry.overage > 0" in source
    assert "Organization.stripe_customer_id.is_not(None)" in source


@pytest.mark.asyncio
async def test_a_customer_detached_mid_sweep_is_left_unstamped(orgs):
    # The query excludes these; one detached between the read and the report is left for the next
    # run rather than reported against nobody.
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
async def test_a_failed_debit_is_unwound_in_a_savepoint_not_left_poisoning_the_session():
    # THE ONE THAT MATTERS MOST. `credit_service.debit` FLUSHES, so a failed insert or a dropped
    # connection leaves the session requiring a rollback before it will accept another statement.
    # Catching the exception is NOT sufficient: the outbox write and the commit that follow would
    # fail, rolling back the terminal transition and leaving a finished four-hour mesh stuck
    # non-terminal until the reaper marked it failed.
    #
    # The savepoint is what makes the fail-open true. This asserts the debit runs INSIDE one and
    # that the savepoint is rolled back, leaving the outer transaction usable.
    import types

    from meshpipeline.application import terminal_finalize

    class _Savepoint:
        def __init__(self, db):
            self.db = db

        async def __aenter__(self):
            self.db.events.append("savepoint-begin")
            return self

        async def __aexit__(self, exc_type, exc, tb):
            self.db.events.append("savepoint-rollback" if exc_type else "savepoint-commit")
            # False: the exception keeps propagating to the caller's own handler, exactly as a real
            # savepoint context does. Swallowing it here would hide whether the handler copes.
            return False

    class _Session:
        def __init__(self):
            self.events: list[str] = []

        def begin_nested(self):
            return _Savepoint(self)

    async def _explode(db, *, job):
        db.events.append("debit-attempted")
        raise RuntimeError("the ledger insert failed and this session now needs a rollback")

    session = _Session()
    original = metering_service.charge_for_job
    metering_service.charge_for_job = _explode
    try:
        await terminal_finalize._charge_fail_open(session, types.SimpleNamespace(id="job-1"))
    finally:
        metering_service.charge_for_job = original

    assert session.events == ["savepoint-begin", "debit-attempted", "savepoint-rollback"]


@pytest.mark.asyncio
async def test_a_successful_debit_commits_its_savepoint():
    import types

    from meshpipeline.application import terminal_finalize

    class _Savepoint:
        def __init__(self, db):
            self.db = db

        async def __aenter__(self):
            self.db.events.append("savepoint-begin")
            return self

        async def __aexit__(self, exc_type, exc, tb):
            self.db.events.append("savepoint-rollback" if exc_type else "savepoint-commit")
            return False

    class _Session:
        def __init__(self):
            self.events: list[str] = []

        def begin_nested(self):
            return _Savepoint(self)

    async def _ok(db, *, job):
        db.events.append("debit-written")
        return 15

    session = _Session()
    original = metering_service.charge_for_job
    metering_service.charge_for_job = _ok
    try:
        await terminal_finalize._charge_fail_open(session, types.SimpleNamespace(id="job-1"))
    finally:
        metering_service.charge_for_job = original

    assert session.events == ["savepoint-begin", "debit-written", "savepoint-commit"]


@pytest.mark.asyncio
async def test_the_fail_open_handler_cannot_itself_raise(caplog):
    # A handler whose LOGGING raises escapes the except block and rolls back the transaction - which
    # is exactly the outcome the fail-open exists to prevent, arriving through the code meant to
    # prevent it. This drives a row that has no `.id` at all through the real handler.
    import types

    from meshpipeline.application import terminal_finalize

    async def _boom(db, *, job):
        raise RuntimeError("metering is broken")

    class _Savepoint:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

    class _Session:
        def begin_nested(self):
            return _Savepoint()

    original = metering_service.charge_for_job
    metering_service.charge_for_job = _boom
    try:
        # A ROW WITH NO `.id` AT ALL, driven through the real handler: the log line must read it
        # defensively or the handler raises while handling and unwinds the transaction itself.
        await terminal_finalize._charge_fail_open(_Session(), types.SimpleNamespace())
    finally:
        metering_service.charge_for_job = original
