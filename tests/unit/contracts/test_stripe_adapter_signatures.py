# Responsibility: Prove every call the Stripe adapter makes is one the INSTALLED SDK accepts.
# Boundaries: no network. Each service the adapter touches is replaced by an autospec of the SDK's own
#             class, so a keyword the real method does not take fails here instead of in production.
from __future__ import annotations

import types
from unittest.mock import create_autospec

import pytest

stripe = pytest.importorskip("stripe")

from meshpipeline.adapters.stripe_billing import StripeBillingGateway  # noqa: E402


def _service(cls, **returns):
    service = create_autospec(cls, instance=True)
    for method, value in returns.items():
        getattr(service, method).return_value = value
    return service


@pytest.fixture
def gateway():
    from stripe._customer_service import CustomerService
    from stripe._invoice_item_service import InvoiceItemService
    from stripe._invoice_service import InvoiceService
    from stripe._subscription_service import SubscriptionService
    from stripe.billing._meter_event_service import MeterEventService
    from stripe.billing_portal._session_service import SessionService as PortalSessionService
    from stripe.checkout._session_service import SessionService as CheckoutSessionService

    invoice = types.SimpleNamespace(id="in_1", number="N-1", status="open", amount_due=100,
                                    amount_paid=0, currency="usd", created=0,
                                    hosted_invoice_url="https://invoice.stripe.com/x")
    subscription = create_autospec(stripe.Subscription, instance=True)
    subscription.to_dict.return_value = {"items": {"data": [
        {"id": "si_flat", "price": {"recurring": {"usage_type": "licensed"}}},
        {"id": "si_meter", "price": {"recurring": {"usage_type": "metered"}}}]}}

    gw = StripeBillingGateway("sk_test_x", api_version="2026-08-26.dahlia",
                              webhook_secret="whsec_x", console_base_url="https://c.example")
    gw._client = types.SimpleNamespace(
        customers=_service(CustomerService, create=types.SimpleNamespace(id="cus_1")),
        checkout=types.SimpleNamespace(sessions=_service(
            CheckoutSessionService, create=types.SimpleNamespace(url="https://checkout.stripe.com/x"))),
        billing_portal=types.SimpleNamespace(sessions=_service(
            PortalSessionService, create=types.SimpleNamespace(url="https://billing.stripe.com/x"))),
        subscriptions=_service(SubscriptionService, retrieve=subscription),
        billing=types.SimpleNamespace(meter_events=_service(MeterEventService)),
        invoice_items=_service(InvoiceItemService),
        invoices=_service(InvoiceService, create=invoice, finalize_invoice=invoice,
                          list=types.SimpleNamespace(data=[invoice])),
    )
    return gw


def test_every_write_binds_against_the_real_sdk_signature(gateway):
    # THE BUG THIS EXISTS FOR: `idempotency_key=` as a bare keyword is a TypeError on every
    # StripeClient service - it belongs inside `options`. A faked client accepted it, so every write
    # failed in production and passed here.
    gateway.ensure_customer(organization_id="org", email="a@example.com", name="A")
    gateway.start_checkout(customer_id="cus_1", price_id="price_f", overage_price_id="price_o",
                           organization_id="org", plan="starter")
    gateway.billing_portal(customer_id="cus_1")
    gateway.report_usage(customer_id="cus_1", quantity=3, idempotency_scope="row")
    gateway.change_plan(subscription_id="sub_1", price_id="price_tf", overage_price_id="price_to",
                        plan="team")
    gateway.create_invoice(customer_id="cus_1", amount=100, currency="usd", description="d",
                           days_until_due=7)
    gateway.list_invoices(customer_id="cus_1")


def test_every_write_carries_its_idempotency_key_inside_options(gateway):
    gateway.ensure_customer(organization_id="org-7", email="a@example.com", name="A")
    gateway.report_usage(customer_id="cus_1", quantity=3, idempotency_scope="row-9")
    assert gateway._client.customers.create.call_args.kwargs["options"] == {
        "idempotency_key": "customer:org-7"}
    assert gateway._client.billing.meter_events.create.call_args.kwargs["options"] == {
        "idempotency_key": "usage:row-9"}


def test_a_plan_change_reads_the_subscription_as_a_plain_dict_and_swaps_each_item_by_id(gateway):
    # An SDK object is not a mapping (`.get` raises), and naming a price without its item id ADDS a
    # line - two flat fees from then on.
    gateway.change_plan(subscription_id="sub_1", price_id="price_tf", overage_price_id="price_to",
                        plan="team")
    params = gateway._client.subscriptions.update.call_args.args[1]
    assert params["items"] == [{"id": "si_flat", "price": "price_tf"},
                               {"id": "si_meter", "price": "price_to"}]
    assert params["metadata"] == {"plan": "team"}
