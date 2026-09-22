# Responsibility: Turn a billing event into this schema's facts, and start the two hosted flows.
# Owns: what an event MEANS - which plan an organisation holds, and when credits are granted.
# Boundaries: it calls the gateway CONTRACT for the provider and the repositories for rows. It
#             verifies no signature (the route does) and it decides no limits (settings/plans.py).
from __future__ import annotations

import logging
import uuid
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

import meshpipeline.settings.billing as billcfg
import meshpipeline.settings.plans as plancfg
from meshpipeline.application import credit_service
from meshpipeline.contracts.billing import BillingEvent, get_billing_gateway
from meshpipeline.persistence.models import StripeEvent
from meshpipeline.persistence.repositories.organization_repository import (
    OrganizationRepository,
)

log = logging.getLogger(__name__)

organization_repo = OrganizationRepository()

#: THE EVENTS THIS PROCESS ACTS ON. Everything else the provider sends is acknowledged and dropped.
#:
#: WHY AN ALLOWLIST AND NOT A HANDLER LOOKUP THAT SHRUGS. A provider endpoint is configured with the
#: event types it sends, by a person, in a browser - so the set arriving here can widen without any
#: change to this repository. An explicit list means a newly-enabled event is visibly ignored rather
#: than silently matching some near-miss handler.
HANDLED = frozenset({
    "checkout.session.completed",
    "customer.subscription.created",
    "customer.subscription.updated",
    "customer.subscription.deleted",
    "invoice.paid",
    "invoice.payment_failed",
})

#: What a period's included allowance is called in the ledger, so a person reading their own history
#: sees why the credits are there.
ALLOWANCE_REASON = "plan allowance"


async def start_checkout(db: AsyncSession, *, organization_id: uuid.UUID, plan: str,
                         email: str) -> str:
    """The URL a person is sent to in order to buy `plan`. Raises on a plan that cannot be sold."""
    price_id, overage_price_id = billcfg.price_for(plan)
    if not price_id:
        # A TIER WITH NO PRICE IS NOT PURCHASABLE, and that is two situations wearing one shape:
        # `enterprise`, invoiced by hand and deliberately priceless, and a tier whose price id has
        # simply not been configured here yet. Both must refuse. Proceeding would open a session
        # with no line item, which fails inside the provider's own page after the person has
        # already left the console.
        raise ValueError(f"plan {plan!r} is not purchasable in this deployment")

    gateway = get_billing_gateway()
    organization = await organization_repo.get_by_id(db, organization_id)
    if organization is None:
        raise ValueError("organisation not found")

    customer_id = organization.stripe_customer_id
    if not customer_id:
        # CREATED ON FIRST CHECKOUT, not at signup. Most organisations never buy anything, and a
        # customer minted per signup fills the provider account with rows that never carry a charge.
        # The gateway's idempotency is on the organisation id, so two racing first checkouts resolve
        # to one customer.
        customer_id = gateway.ensure_customer(
            organization_id=str(organization_id), email=email, name=organization.name)
        await organization_repo.attach_customer(
            db, organization_id=organization_id, stripe_customer_id=customer_id)

    return gateway.start_checkout(
        customer_id=customer_id, price_id=price_id, overage_price_id=overage_price_id,
        organization_id=str(organization_id), plan=plan.strip().lower())


async def portal_url(db: AsyncSession, *, organization_id: uuid.UUID) -> str:
    """Where a person manages the card, the invoices and the cancellation they already have."""
    gateway = get_billing_gateway()
    organization = await organization_repo.get_by_id(db, organization_id)
    if organization is None or not organization.stripe_customer_id:
        # NO CUSTOMER MEANS NOTHING TO MANAGE. An organisation that never checked out has no card,
        # no invoice and no subscription, so the portal would open on an empty page. Refusing here
        # lets the console show "choose a plan" instead.
        raise ValueError("this organisation has no billing account yet")
    return gateway.billing_portal(customer_id=organization.stripe_customer_id)


async def read_subscription(db: AsyncSession, *, organization_id: uuid.UUID) -> dict:
    """What the console shows on the billing page. Never raises for an organisation without one."""
    organization = await organization_repo.get_by_id(db, organization_id)
    if organization is None:
        return unsubscribed()
    limits = plancfg.limits_for(organization.plan)
    return {
        "plan": organization.plan,
        "status": organization.subscription_status,
        "current_period_end": (organization.current_period_end.isoformat()
                               if organization.current_period_end else None),
        "included_credits": limits.included_credits,
        # WHETHER A CARD EXISTS, never the card. Payment methods live in the provider's portal, and
        # that is why this integration has no card fields anywhere in its own surface.
        "has_billing_account": bool(organization.stripe_customer_id),
    }


def unsubscribed() -> dict:
    # THE UNSUBSCRIBED ANSWER, stated rather than computed - the same discipline credits.py applies
    # to its empty ledger. An organisation with no plan has no period and no account, and deriving
    # those from an absent row means inventing values that only ever describe nothing.
    return {
        "plan": "",
        "status": "",
        "current_period_end": None,
        "included_credits": 0,
        "has_billing_account": False,
    }


async def handle_event(db: AsyncSession, event: BillingEvent) -> bool:
    """Apply one verified event. Returns False when it was a duplicate or not of interest."""
    if event.type not in HANDLED:
        return False

    # THE IDEMPOTENCY CLAIM, taken BEFORE the effect and inside the SAME transaction as it. The
    # provider delivers at least once and retries every non-2xx, so a repeat is ordinary traffic.
    # Because the event id is the primary key, a second delivery - even one racing the first in
    # another worker - collides here and is rolled back with its effect unapplied, rather than
    # granting a period's credits twice.
    db.add(StripeEvent(id=event.id, event_type=event.type[:128]))
    try:
        await db.flush()
    except IntegrityError:
        await db.rollback()
        log.info("billing event %s already handled; ignoring duplicate delivery", event.id)
        return False

    obj = event.payload
    if event.type.startswith("customer.subscription."):
        await _apply_subscription(db, obj, deleted=event.type.endswith(".deleted"))
    elif event.type == "checkout.session.completed":
        await _apply_checkout(db, obj)
    elif event.type == "invoice.paid":
        await _apply_invoice_paid(db, obj)
    elif event.type == "invoice.payment_failed":
        await _apply_payment_failed(db, obj)
    return True


async def _apply_checkout(db: AsyncSession, session: Mapping[str, Any]) -> None:
    # FULFILMENT LIVES HERE, in the webhook, not on the success page the browser lands on. The
    # browser may never arrive - the tab is closed, the redirect is lost, the network drops - and
    # with an asynchronous payment method the money is not even confirmed at redirect time. The
    # webhook is the only delivery that is retried until it is acknowledged.
    if str(session.get("payment_status", "")) not in {"paid", "no_payment_required"}:
        # AN UNPAID COMPLETED SESSION is a real state, not a contradiction: a delayed payment method
        # completes the session and settles later. Acting now would grant a plan that has not been
        # paid for; the later `invoice.paid` is what carries the credits.
        return
    organization_id = _organization_of(session)
    if organization_id is None:
        return
    customer_id = _id_of(session.get("customer"))
    if customer_id:
        # THE BACKSTOP for an organisation whose customer id never landed - a checkout started by a
        # revision that crashed between minting the customer and writing the column.
        await organization_repo.attach_customer(
            db, organization_id=organization_id, stripe_customer_id=customer_id)


async def _apply_subscription(db: AsyncSession, subscription: Mapping[str, Any], *,
                              deleted: bool) -> None:
    organization_id = _organization_of(subscription)
    if organization_id is None:
        return

    if deleted:
        # THE PLAN IS CLEARED, not kept with a dead status. settings/plans.py resolves an empty plan
        # to the deployment's configured limits, which is exactly what a former customer should get:
        # the free behaviour, immediately, with no second rule that has to remember to consult the
        # status column before honouring the tier.
        await organization_repo.set_subscription(
            db, organization_id=organization_id, subscription_id="", plan="",
            status="canceled", current_period_end=None)
        return

    plan = str((subscription.get("metadata") or {}).get("plan", "")).strip().lower()
    await organization_repo.set_subscription(
        db, organization_id=organization_id,
        subscription_id=str(subscription.get("id", "")),
        # `None`, NOT `""`, when the subscription carries no plan metadata: the repository reads
        # None as "leave the tier alone" and "" as "clear it". A subscription created by hand in the
        # provider's dashboard is how an enterprise deal is set up, and it has no metadata of ours -
        # clearing its plan would downgrade the customer paying the most.
        plan=plan or None,
        status=str(subscription.get("status", "")),
        current_period_end=_period_end(subscription))


#: THE INVOICE REASONS THAT CARRY AN ALLOWANCE. Stripe sets `billing_reason` on every invoice, and
#: only these two are a subscription PERIOD being paid for: the first one at checkout, and each
#: renewal after it.
#:
#: WHY AN ALLOWLIST RATHER THAN "has a subscription". `subscription_cycle` and `subscription_create`
#: are the two that mean "a new period just started". `subscription_update` (a mid-cycle plan
#: change, prorated) and `subscription_threshold` (a usage-billing threshold reached) also name a
#: subscription, and granting a full period's credits for either would hand out an allowance for a
#: period that has not begun - twice over, if the customer changes plan twice in a month.
ALLOWANCE_BILLING_REASONS = frozenset({"subscription_create", "subscription_cycle"})


async def _apply_invoice_paid(db: AsyncSession, invoice: Mapping[str, Any]) -> None:
    # THE PERIOD'S ALLOWANCE IS GRANTED WHEN THE MONEY ARRIVES, never at checkout and never on a
    # timer. `invoice.paid` fires for the first period and for every renewal, so one handler covers
    # both, and an organisation whose renewal fails simply does not receive the next grant.
    #
    # IT MUST BE A SUBSCRIPTION PERIOD, AND THAT IS NOT A FORMALITY. This deployment raises
    # STANDALONE invoices of its own - `admin_billing.raise_invoice` bills an enterprise account for
    # an amount agreed off the product - and those arrive here as `invoice.paid` against the same
    # customer. Without this check, paying a one-off invoice would grant a full plan allowance that
    # nobody bought, every time, and the larger the customer the more often it would happen.
    reason = str(invoice.get("billing_reason", "")).strip()
    if reason not in ALLOWANCE_BILLING_REASONS:
        log.info("invoice %s paid with billing_reason %r; no allowance is granted",
                 invoice.get("id", "?"), reason or "(none)")
        return

    customer_id = _id_of(invoice.get("customer"))
    if not customer_id:
        return
    organization = await organization_repo.get_by_stripe_customer(db, customer_id)
    if organization is None:
        log.warning("invoice paid for unknown billing customer %s", customer_id)
        return

    # THE AMOUNT COMES FROM THE PLAN CATALOGUE, not from the invoice. What the customer PAID and
    # what the tier INCLUDES are different numbers - an invoice also carries the overage of the
    # period that just closed - and reading the invoice would conflate them.
    included = plancfg.limits_for(organization.plan).included_credits
    if included <= 0:
        return
    await credit_service.grant(db, organization_id=organization.id, amount=included,
                               reason=ALLOWANCE_REASON)


async def _apply_payment_failed(db: AsyncSession, invoice: Mapping[str, Any]) -> None:
    # RECORDED, NOT ENFORCED. The provider retries on its own dunning schedule and the subscription
    # moves to `past_due` - which is not `canceled`, and deliberately keeps serving. Cutting a
    # customer off at the first failed retry would end a paying relationship over an expired card.
    # The eventual `customer.subscription.deleted` is what stops service.
    customer_id = _id_of(invoice.get("customer"))
    if not customer_id:
        return
    organization = await organization_repo.get_by_stripe_customer(db, customer_id)
    if organization is None:
        return
    log.warning("payment failed for organisation %s (customer %s); the provider will retry",
                organization.id, customer_id)


def _organization_of(obj: Mapping[str, Any]) -> uuid.UUID | None:
    # THE METADATA IS THE LINK. It is set on both the session and the subscription at checkout, so
    # every event handled here carries it - except one created by hand in the dashboard, which is
    # why a miss is logged and dropped rather than raised. There is no safe fallback: attaching a
    # customer to a guessed organisation would cross one tenant's billing with another's.
    raw = str((obj.get("metadata") or {}).get("organization_id", "")).strip()
    if not raw:
        log.warning("billing object %s carries no organization_id metadata", obj.get("id", "?"))
        return None
    try:
        return uuid.UUID(raw)
    except ValueError:
        log.warning("billing object %s carries a malformed organization_id %r",
                    obj.get("id", "?"), raw)
        return None


def _id_of(value: Any) -> str:
    # A PROVIDER REFERENCE IS EITHER AN ID OR AN EXPANDED OBJECT, depending on the event and on
    # whether anything asked for expansion. Both spellings mean the same thing, and a handler that
    # assumed one of them breaks the day the other arrives.
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, Mapping):
        return str(value.get("id", "") or "")
    return str(getattr(value, "id", "") or "")


def _period_end(subscription: Mapping[str, Any]) -> datetime | None:
    raw = subscription.get("current_period_end")
    if not raw:
        return None
    # UNIX SECONDS. Made timezone-aware immediately because the column is DateTime(timezone=True),
    # and a naive datetime written there is an hour-shaped bug that only appears wherever the
    # server's local zone is not UTC.
    return datetime.fromtimestamp(int(raw), tz=UTC)
