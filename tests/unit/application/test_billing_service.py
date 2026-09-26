# Responsibility: Verify a Stripe event moves exactly the facts it should, once, and never twice.
from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy.exc import IntegrityError

import meshpipeline.settings.billing as billcfg
from meshpipeline.application import billing_service, credit_service
from meshpipeline.contracts.billing import BillingEvent


class _Obj:
    # An ORGANISATION ROW double. The repositories return objects, so this is attribute-accessed -
    # unlike an event payload, which the contract defines as a plain mapping.
    def __init__(self, **fields):
        for key, value in fields.items():
            setattr(self, key, value)


def _event(event_type: str, payload: dict, event_id: str = "evt_1") -> BillingEvent:
    # THE CONTRACT'S OWN VALUE, not a provider object. `BillingEvent.payload` is a Mapping by
    # design, which is what lets every handler be exercised with the SDK absent entirely.
    return BillingEvent(id=event_id, type=event_type, payload=payload)


class _DbDouble:

    def __init__(self, *, colliding: bool = False):
        self.added: list = []
        self.colliding = colliding
        self.rolled_back = False

    def add(self, row):
        self.added.append(row)

    async def flush(self):
        if self.colliding:
            raise IntegrityError("duplicate", None, Exception("duplicate"))

    async def rollback(self):
        self.rolled_back = True


class _OrgDouble:

    def __init__(self, organization=None):
        self.organization = organization
        self.subscriptions: list[dict] = []
        self.attached: list[dict] = []

    async def get_by_id(self, db, organization_id):
        return self.organization

    async def get_by_stripe_customer(self, db, stripe_customer_id):
        return self.organization

    async def attach_customer(self, db, *, organization_id, stripe_customer_id):
        self.attached.append({"organization_id": organization_id,
                              "stripe_customer_id": stripe_customer_id})

    async def set_subscription(self, db, *, organization_id, subscription_id, plan, status,
                               current_period_end):
        self.subscriptions.append({"organization_id": organization_id, "plan": plan,
                                   "subscription_id": subscription_id, "status": status,
                                   "current_period_end": current_period_end})


@pytest.fixture
def orgs(monkeypatch):
    double = _OrgDouble()
    monkeypatch.setattr(billing_service, "organization_repo", double)
    return double


@pytest.fixture
def grants(monkeypatch):
    recorded: list[dict] = []

    async def _grant(db, *, organization_id, amount, reason=""):
        recorded.append({"organization_id": organization_id, "amount": amount, "reason": reason})

    monkeypatch.setattr(credit_service, "grant", _grant)
    return recorded


@pytest.mark.asyncio
async def test_an_unhandled_event_type_is_ignored_without_claiming_it(orgs):
    # An event this deployment does not act on must not write an idempotency row. Claiming it would
    # mean that enabling the handler later silently skips every event already delivered.
    db = _DbDouble()
    applied = await billing_service.handle_event(db, _event("customer.created", {"id": "cus_1"}))
    assert applied is False
    assert db.added == []


@pytest.mark.asyncio
async def test_a_duplicate_delivery_applies_nothing(orgs, grants):
    # THE CENTRAL GUARANTEE. Stripe delivers at least once and retries every non-2xx, so the same
    # `invoice.paid` arrives more than once as a matter of routine. The second delivery must not
    # grant a second period's credits.
    db = _DbDouble(colliding=True)
    applied = await billing_service.handle_event(
        db, _event("invoice.paid", {"id": "in_1", "customer": "cus_1",
                                "billing_reason": "subscription_cycle"}))
    assert applied is False
    assert db.rolled_back is True
    assert grants == []


@pytest.mark.asyncio
async def test_invoice_paid_grants_the_plans_allowance(orgs, grants):
    org_id = uuid.uuid4()
    orgs.organization = _Obj(id=org_id, plan="team", stripe_customer_id="cus_1")
    await billing_service.handle_event(
        db := _DbDouble(), _event("invoice.paid", {"id": "in_1", "customer": "cus_1",
                                "billing_reason": "subscription_cycle"}))
    assert db.added  # the event was claimed
    # THE AMOUNT COMES FROM THE PLAN CATALOGUE, not from the invoice. What the customer PAID and
    # what the tier INCLUDES are different numbers, and reading the invoice would conflate them.
    assert grants == [{"organization_id": org_id, "amount": 10_000,
                       "reason": billing_service.ALLOWANCE_REASON}]


@pytest.mark.asyncio
async def test_a_plan_that_includes_nothing_grants_nothing(orgs, grants):
    # An organisation on no plan whose invoice is paid - possible for a one-off charge - must not
    # receive credits. The ledger's CHECK refuses a zero amount anyway; this stops the call.
    orgs.organization = _Obj(id=uuid.uuid4(), plan="", stripe_customer_id="cus_1")
    await billing_service.handle_event(
        _DbDouble(), _event("invoice.paid", {"id": "in_1", "customer": "cus_1",
                                "billing_reason": "subscription_cycle"}))
    assert grants == []


@pytest.mark.asyncio
async def test_an_expanded_customer_object_resolves_like_a_bare_id(orgs, grants):
    # A Stripe reference is either an id string or an expanded object depending on the event and on
    # what asked for expansion. Both spellings mean the same customer.
    org_id = uuid.uuid4()
    orgs.organization = _Obj(id=org_id, plan="starter", stripe_customer_id="cus_1")
    await billing_service.handle_event(
        _DbDouble(), _event("invoice.paid", {"id": "in_1", "customer": {"id": "cus_1"},
                                "billing_reason": "subscription_cycle"}))
    assert [g["organization_id"] for g in grants] == [org_id]


@pytest.mark.asyncio
async def test_a_cancelled_subscription_clears_the_plan(orgs):
    org_id = uuid.uuid4()
    await billing_service.handle_event(
        _DbDouble(),
        _event("customer.subscription.deleted",
               {"id": "sub_1", "metadata": {"organization_id": str(org_id)},
                "status": "canceled"}))
    # CLEARED, not kept with a dead status: settings/plans.py resolves an empty plan to the
    # deployment's defaults, which is exactly the free behaviour a former customer should get.
    assert orgs.subscriptions == [{"organization_id": org_id, "plan": "", "subscription_id": "",
                                   "status": "canceled", "current_period_end": None}]


@pytest.mark.asyncio
async def test_a_subscription_without_our_metadata_does_not_clear_the_tier(orgs):
    # THE ENTERPRISE PATH. A subscription created by hand in the Stripe dashboard carries no
    # metadata of ours. Passing "" here would wipe the tier of the customer paying the most; the
    # repository reads None as "leave the plan alone", which is why this asserts on None and not "".
    org_id = uuid.uuid4()
    await billing_service.handle_event(
        _DbDouble(),
        _event("customer.subscription.updated",
               {"id": "sub_1", "metadata": {"organization_id": str(org_id)},
                "status": "active", "current_period_end": 1_760_000_000}))
    assert orgs.subscriptions[0]["plan"] is None
    assert orgs.subscriptions[0]["status"] == "active"


@pytest.mark.asyncio
async def test_a_period_end_is_stored_timezone_aware(orgs):
    # The column is DateTime(timezone=True). A naive datetime written there is an hour-shaped bug
    # that only appears where the server's local zone is not UTC.
    org_id = uuid.uuid4()
    await billing_service.handle_event(
        _DbDouble(),
        _event("customer.subscription.updated",
               {"id": "sub_1", "metadata": {"organization_id": str(org_id), "plan": "team"},
                "status": "active", "current_period_end": 1_760_000_000}))
    stored = orgs.subscriptions[0]["current_period_end"]
    assert stored == datetime.fromtimestamp(1_760_000_000, tz=UTC)
    assert stored.tzinfo is not None


@pytest.mark.asyncio
async def test_an_unpaid_checkout_session_is_not_fulfilled(orgs):
    # A delayed payment method completes the session and settles later. Acting now would attach a
    # customer for a payment that may never arrive; the later invoice.paid is what carries credits.
    await billing_service.handle_event(
        _DbDouble(),
        _event("checkout.session.completed",
               {"id": "cs_1", "payment_status": "unpaid", "customer": "cus_1",
                "metadata": {"organization_id": str(uuid.uuid4())}}))
    assert orgs.attached == []


@pytest.mark.asyncio
async def test_a_checkout_without_organisation_metadata_is_dropped(orgs):
    # There is no safe fallback: attaching a customer to a guessed organisation would cross one
    # tenant's billing with another's. It is logged and dropped.
    await billing_service.handle_event(
        _DbDouble(),
        _event("checkout.session.completed",
               {"id": "cs_1", "payment_status": "paid", "customer": "cus_1", "metadata": {}}))
    assert orgs.attached == []


@pytest.mark.asyncio
async def test_a_plan_with_no_configured_price_cannot_be_checked_out(orgs, monkeypatch):
    # `enterprise` is invoiced by hand and deliberately has no price, and an unconfigured tier looks
    # identical here. Both must refuse before a Stripe session is opened with no line item.
    monkeypatch.setattr(billcfg, "STRIPE_PRICE_TEAM", "")
    orgs.organization = _Obj(id=uuid.uuid4(), name="Acme", stripe_customer_id="cus_1")
    with pytest.raises(ValueError):
        await billing_service.start_checkout(
            _DbDouble(), organization_id=uuid.uuid4(), plan="team", email="a@example.com")


# WHICH INVOICES CARRY AN ALLOWANCE

@pytest.mark.asyncio
async def test_a_standalone_invoice_grants_no_allowance(orgs, grants):
    # THE SELF-INFLICTED ONE. `admin_billing.raise_invoice` raises STANDALONE invoices against the
    # same customer to bill an enterprise account for an amount agreed off the product. Those arrive
    # here as `invoice.paid` too, and granting a full plan allowance for one would hand out credits
    # nobody bought - every time, and most often to the largest customer.
    orgs.organization = _Obj(id=uuid.uuid4(), plan="team", stripe_customer_id="cus_1")
    await billing_service.handle_event(
        _DbDouble(),
        _event("invoice.paid", {"id": "in_1", "customer": "cus_1",
                                "billing_reason": "manual"}))
    assert grants == []


@pytest.mark.asyncio
async def test_an_invoice_with_no_billing_reason_grants_nothing(orgs, grants):
    # Absent is not "probably a renewal". An allowance granted on a guess is indistinguishable from
    # one that was paid for, and the ledger would carry it as though it had been.
    orgs.organization = _Obj(id=uuid.uuid4(), plan="team", stripe_customer_id="cus_1")
    await billing_service.handle_event(
        _DbDouble(), _event("invoice.paid", {"id": "in_1", "customer": "cus_1"}))
    assert grants == []


@pytest.mark.parametrize("reason", ["subscription_create", "subscription_cycle"])
@pytest.mark.asyncio
async def test_the_first_period_and_every_renewal_do_grant(orgs, grants, reason):
    orgs.organization = _Obj(id=uuid.uuid4(), plan="team", stripe_customer_id="cus_1")
    await billing_service.handle_event(
        _DbDouble(),
        _event("invoice.paid", {"id": "in_1", "customer": "cus_1", "billing_reason": reason}))
    assert [g["amount"] for g in grants] == [10_000]


@pytest.mark.parametrize("reason", ["subscription_update", "subscription_threshold"])
@pytest.mark.asyncio
async def test_a_mid_cycle_subscription_invoice_grants_nothing(orgs, grants, reason):
    # A prorated plan change and a usage threshold both name a subscription without starting a new
    # period. Granting for either would hand out a full allowance twice in one month to a customer
    # who simply changed their mind about a tier.
    orgs.organization = _Obj(id=uuid.uuid4(), plan="team", stripe_customer_id="cus_1")
    await billing_service.handle_event(
        _DbDouble(),
        _event("invoice.paid", {"id": "in_1", "customer": "cus_1", "billing_reason": reason}))
    assert grants == []


# A SECOND CHECKOUT

class _CheckoutGateway:
    def __init__(self):
        self.sessions: list[dict] = []

    def ensure_customer(self, *, organization_id, email, name):
        return "cus_new"

    def start_checkout(self, **kwargs):
        self.sessions.append(kwargs)
        return "https://checkout.stripe.com/c/pay/cs_test"


@pytest.fixture
def checkout_gateway(monkeypatch):
    from meshpipeline.contracts import billing as billing_contract
    gateway = _CheckoutGateway()
    billing_contract.set_billing_gateway(gateway)
    monkeypatch.setattr(billcfg, "STRIPE_PRICE_TEAM", "price_team")
    yield gateway
    billing_contract.set_billing_gateway(None)


@pytest.mark.parametrize("status", ["active", "trialing", "past_due", "unpaid", "incomplete"])
@pytest.mark.asyncio
async def test_a_live_subscriber_cannot_open_a_second_subscription(orgs, checkout_gateway, status):
    # Checkout in subscription mode always creates a NEW subscription: an "upgrade" through it
    # would leave the customer paying two flat fees. Changing tier is the portal's.
    orgs.organization = _Obj(id=uuid.uuid4(), name="Acme", stripe_customer_id="cus_1",
                             stripe_subscription_id="sub_1", subscription_status=status)
    with pytest.raises(billing_service.AlreadySubscribed):
        await billing_service.start_checkout(
            _DbDouble(), organization_id=uuid.uuid4(), plan="team", email="a@example.com")
    assert checkout_gateway.sessions == []


@pytest.mark.asyncio
async def test_a_former_subscriber_may_check_out_again(orgs, checkout_gateway):
    orgs.organization = _Obj(id=uuid.uuid4(), name="Acme", stripe_customer_id="cus_1",
                             stripe_subscription_id="", subscription_status="canceled")
    url = await billing_service.start_checkout(
        _DbDouble(), organization_id=uuid.uuid4(), plan="team", email="a@example.com")
    assert url.startswith("https://checkout.stripe.com/")
    assert checkout_gateway.sessions[0]["customer_id"] == "cus_1"
