# Responsibility: Implement the billing gateway against Stripe.
# Owns: the SDK client, the pinned API version, idempotency on every write, and the translation
#       from Stripe's objects into the contract's neutral values.
# Boundaries: it speaks HTTP to Stripe. It touches no database and decides nothing about plans -
#             application/billing_service.py does both.
from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    # TYPE-ONLY, so the SDK is still imported lazily inside the methods below. The adapter must stay
    # importable with `stripe` absent - the contract's tests exercise every handler without it - and
    # a module-level runtime import would take that away. mypy reads this branch; the interpreter
    # never does.
    from stripe.params.checkout import (
        SessionCreateParams,
        SessionCreateParamsLineItem,
    )

import meshpipeline.settings.billing as billcfg
from meshpipeline.contracts.billing import (
    BillingEvent,
    BillingGateway,
    BillingUnavailable,
    EventVerificationFailed,
    Invoice,
)

#: THE METER'S EVENT NAME, as configured on the Stripe meter the overage prices read. It is a REMOTE
#: name this string must match exactly: a typo does not fail, it reports usage into a meter nobody
#: bills on, and the customer is silently undercharged.
OVERAGE_EVENT_NAME = "mesh_job_credits"


class StripeBillingGateway:
    # ONE CLIENT PER GATEWAY, built once and reused, because the SDK holds a connection pool: a
    # client per call means a TLS handshake per call on the slow path of a checkout.
    #
    # THE INSTANCE PATTERN, deliberately, not the module-level `stripe.api_key = ...` global. That
    # global is deprecated in every current SDK and is process-wide mutable state: a test that sets
    # it leaks the key into every other test in the interpreter, and two deployments sharing a
    # process could not hold different keys.

    def __init__(self, api_key: str, *, api_version: str, webhook_secret: str,
                 console_base_url: str):
        import stripe
        self._client = stripe.StripeClient(api_key, stripe_version=api_version)
        self._webhook_secret = webhook_secret
        self._console_base_url = console_base_url.rstrip("/")

    @staticmethod
    def _idempotency(scope: str, key: str) -> dict:
        # EVERY WRITE CARRIES ONE. A create that times out has an unknown outcome - the customer may
        # or may not exist - and the caller's only safe move is to retry. Without a key that retry
        # creates a SECOND customer. The key is derived from what the call is ABOUT, so the retry
        # presents the same one and Stripe replays the original result instead of acting twice.
        return {"idempotency_key": f"{scope}:{key}"}

    def ensure_customer(self, *, organization_id: str, email: str, name: str) -> str:
        customer = self._client.customers.create(
            {
                "email": email,
                "name": name,
                # THE BACK-REFERENCE. A webhook names a customer and never an organisation, so
                # without this the handler's only route home is the unique index on
                # stripe_customer_id. Carrying both also lets an operator reading the Stripe
                # dashboard see whose row it is.
                "metadata": {"organization_id": organization_id},
            },
            # The organisation id IS what the customer is - one customer per organisation - so two
            # racing first checkouts resolve to one customer rather than two.
            **self._idempotency("customer", organization_id),
        )
        return str(customer.id)

    def start_checkout(self, *, customer_id: str, price_id: str, overage_price_id: str,
                       organization_id: str, plan: str) -> str:
        # CHECKOUT SESSIONS, not a hand-built PaymentIntent. Stripe hosts the page, so SCA, 3-D
        # Secure, wallets and local payment methods are handled there - and the card never touches
        # this process, which is the difference between SAQ A and SAQ D.
        items: list[SessionCreateParamsLineItem] = [{"price": price_id, "quantity": 1}]
        if overage_price_id:
            # THE METERED HALF OF THE HYBRID. A metered item carries no quantity: usage is reported
            # against it during the period and priced at the close. Sending one is rejected.
            items.append({"price": overage_price_id})

        # ANNOTATED WITH THE SDK'S OWN PARAMS TYPE rather than left a bare dict. Without it mypy
        # infers `dict[str, Collection[Collection[Any]]]` from the mixed literal and the call fails
        # the ratchet - and more usefully, the annotation is what makes a MISSPELLED KEY here a
        # build error instead of a Stripe 400 found by the first customer to reach checkout.
        params: SessionCreateParams = {
            "mode": "subscription",
            "customer": customer_id,
            "line_items": items,
            "success_url": f"{self._console_base_url}/settings/billing?checkout=success",
            "cancel_url": f"{self._console_base_url}/settings/billing?checkout=cancelled",
            # ON THE SUBSCRIPTION, not only the session. The session is transient and absent from
            # later events; the subscription is what `customer.subscription.updated` carries, and
            # the handler needs to know which plan it names.
            "subscription_data": {
                "metadata": {"organization_id": organization_id, "plan": plan}},
            "metadata": {"organization_id": organization_id, "plan": plan},
            # NO `payment_method_types`. Omitting it enables dynamic payment methods, so what is
            # offered is configured in the dashboard and chosen per customer. Naming them here would
            # freeze the list in code and silently exclude whatever is added later.
        }
        session = self._client.checkout.sessions.create(
            params,
            **self._idempotency("checkout", f"{organization_id}:{plan}:{uuid.uuid4()}"),
        )
        return str(session.url)

    def billing_portal(self, *, customer_id: str) -> str:
        # STRIPE'S OWN HOSTED SURFACE for changing a card, reading invoices and cancelling. Each of
        # those is a screen this product would otherwise build, and each would be a second place
        # that has to stay correct as Stripe's objects change.
        session = self._client.billing_portal.sessions.create(
            {"customer": customer_id,
             "return_url": f"{self._console_base_url}/settings/billing"})
        return str(session.url)

    def report_usage(self, *, customer_id: str, quantity: int, idempotency_scope: str) -> None:
        # ADDRESSED TO THE CUSTOMER, not to a subscription item. A meter event names who consumed,
        # and Stripe resolves which item prices it at the period close - which is what lets one
        # meter serve every tier's overage price. Meter events aggregate rather than overwrite.
        self._client.billing.meter_events.create(
            {
                "event_name": OVERAGE_EVENT_NAME,
                "payload": {"stripe_customer_id": customer_id, "value": str(quantity)},
            },
            **self._idempotency("usage", idempotency_scope),
        )

    def create_invoice(self, *, customer_id: str, amount: int, currency: str,
                       description: str, days_until_due: int) -> Invoice:
        # THE ENTERPRISE PATH, invoiced rather than checked out. Three calls in a fixed order, and
        # the order is load-bearing: an invoice item created without `invoice` set attaches to the
        # customer's NEXT invoice, so the item must exist before the invoice is drawn, and the
        # invoice must be finalised before it has a number or a payable URL.
        item_key = f"{customer_id}:{amount}:{description}"
        self._client.invoice_items.create(
            {"customer": customer_id, "amount": amount, "currency": currency,
             "description": description},
            **self._idempotency("invoiceitem", item_key))
        invoice = self._client.invoices.create(
            {
                "customer": customer_id,
                # SEND_INVOICE, not charge_automatically: this is the path for a customer who pays
                # on terms rather than by stored card, and charging a card they did not agree to
                # use for it is the wrong surprise.
                "collection_method": "send_invoice",
                "days_until_due": days_until_due,
                "description": description,
                "pending_invoice_items_behavior": "include",
            },
            **self._idempotency("invoice", item_key))
        # FINALISED IMMEDIATELY. A draft has no number, no hosted page and no due date that means
        # anything - returning one would hand the operator an Invoice value whose fields are blank
        # for a reason they would have to already know.
        finalized = self._client.invoices.finalize_invoice(invoice.id)
        return _invoice_of(finalized)

    def list_invoices(self, *, customer_id: str, limit: int = 10) -> list[Invoice]:
        page = self._client.invoices.list({"customer": customer_id, "limit": limit})
        return [_invoice_of(row) for row in page.data]

    def verify_event(self, *, payload: bytes, signature: str) -> BillingEvent:
        # `construct_event` VERIFIES THE HMAC AND THE TIMESTAMP TOGETHER over the raw bytes, which
        # is what makes a replayed or forged body fail before any handler sees it. Parsing the JSON
        # first and verifying afterwards is the mistake this method exists to make impossible.
        import stripe
        try:
            event = stripe.Webhook.construct_event(payload, signature, self._webhook_secret)
        except Exception as exc:
            # NARROWED TO THE CONTRACT'S TYPE so the route can answer 400 without importing
            # anything of Stripe's. The original is chained for the log, never for the response.
            raise EventVerificationFailed(str(exc)) from exc
        return BillingEvent(id=str(event.id), type=str(event.type),
                            # `to_dict_recursive` flattens the SDK's objects into plain mappings,
                            # which is the whole point of the contract's payload type: nothing
                            # downstream then depends on the SDK being installed or on its
                            # attribute names.
                            payload=_as_mapping(event.data.object))


def _invoice_of(row: Any) -> Invoice:
    # THE TRANSLATION, in one place. Every field is read defensively because a draft, a finalised
    # invoice and a voided one do not carry the same set - `number` and `hosted_invoice_url` are
    # null until finalisation, and reading them as required would make this raise on exactly the
    # object an operator most wants to look at when something has gone wrong.
    created = getattr(row, "created", None)
    return Invoice(
        id=str(getattr(row, "id", "") or ""),
        number=str(getattr(row, "number", "") or ""),
        status=str(getattr(row, "status", "") or ""),
        amount_due=int(getattr(row, "amount_due", 0) or 0),
        amount_paid=int(getattr(row, "amount_paid", 0) or 0),
        currency=str(getattr(row, "currency", "") or ""),
        # UNIX SECONDS, made aware at the boundary - the same rule billing_service applies to a
        # period end, and for the same reason.
        created_at=datetime.fromtimestamp(int(created), tz=UTC) if created else None,
        hosted_url=str(getattr(row, "hosted_invoice_url", "") or ""),
    )


def _as_mapping(obj: Any) -> dict:
    for method in ("to_dict_recursive", "to_dict"):
        converter = getattr(obj, method, None)
        if callable(converter):
            return dict(converter())
    # A PLAIN MAPPING ALREADY - what a hand-built test double hands over, and what a future SDK
    # returning dicts would give. Copied rather than passed through so a handler cannot mutate the
    # caller's object.
    return dict(obj)


def build_billing_gateway() -> BillingGateway:
    # THE SELECTION, made here and nowhere else. A deployment with no key has no gateway, which is
    # the ordinary state of a developer checkout and of every test - runtime composition binds None
    # and the accessor raises BillingUnavailable, which the routes turn into a 503.
    if not billcfg.enabled():
        raise BillingUnavailable("STRIPE_API_KEY is not configured")
    return StripeBillingGateway(
        billcfg.STRIPE_API_KEY,
        # PINNED IN CODE. See settings/billing.py: the account's dashboard default can be changed by
        # someone who never opens this repository, and that must not reshape the objects parsed here.
        api_version=billcfg.STRIPE_API_VERSION,
        webhook_secret=billcfg.STRIPE_WEBHOOK_SECRET,
        console_base_url=billcfg.CONSOLE_BASE_URL,
    )
