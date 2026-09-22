# Responsibility: Declare how the product charges for what it does, without naming a payment provider.
# Owns: the gateway Protocol, the neutral event value, and the runtime-injected accessor.
# Boundaries: no keys, price ids or provider vocabulary - those belong to settings and to the adapter.
# Collaborates with: adapters/stripe_billing/ for the one implementation that exists today.
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol, runtime_checkable


class BillingError(RuntimeError):
    pass


class BillingUnavailable(BillingError):
    # NO GATEWAY IS CONFIGURED, and only that. It is distinct from a provider failure on purpose: an
    # unconfigured deployment is a 503 an operator fixes with a value, while a provider fault is an
    # upstream error the caller retries. Collapsing them loses the difference at exactly the moment
    # somebody is trying to tell them apart.
    pass


class EventVerificationFailed(BillingError):
    # THE PRESENTED SIGNATURE DID NOT VERIFY. Its own type because the route answers it 400 and
    # every other failure 500: a body that cannot verify must never be retried, and a 5xx would ask
    # the provider to redeliver a forged or stale request indefinitely.
    pass


@dataclass(frozen=True, slots=True)
class BillingEvent:
    # ONE NOTIFICATION from the provider, already proven authentic.
    #
    # WHY `payload` IS A MAPPING AND NOT THE PROVIDER'S OWN OBJECT. The SDK returns objects whose
    # attributes are the provider's vocabulary, and accepting one here would put that vocabulary
    # into application/ - which is the boundary this contract exists to hold. A mapping is also what
    # makes the handlers testable without the SDK installed at all.
    id: str
    type: str
    payload: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class Invoice:
    # ONE ISSUED INVOICE, flattened out of the provider's own object for the same reason BillingEvent
    # is: what an operator needs to see - who, how much, paid or not, where to open it - is a small
    # fixed set, and reading it off the provider's shape would put that vocabulary into the console.
    id: str
    number: str
    status: str
    #: THE SMALLEST CURRENCY UNIT (cents, pence), never a float. Money in a float is how a total
    #: stops matching the sum of its lines.
    amount_due: int
    amount_paid: int
    currency: str
    created_at: datetime | None
    #: The provider-hosted page. The product renders no invoice of its own: a PDF that disagrees
    #: with the provider's is a support case nobody can settle.
    hosted_url: str = ""


@runtime_checkable
class BillingGateway(Protocol):

    #: The provider's customer id for this organisation, created if it does not exist yet. Must be
    #: IDEMPOTENT on organization_id: a retried call after a timeout must not mint a second customer
    #: that then owns half the billing history.
    def ensure_customer(self, *, organization_id: str, email: str, name: str) -> str: ...

    #: The URL a person is sent to in order to buy `plan`. `overage_price_id` may be blank, which
    #: means the tier is flat-rate with no metered component.
    def start_checkout(self, *, customer_id: str, price_id: str, overage_price_id: str,
                       organization_id: str, plan: str) -> str: ...

    #: The URL where a person manages the card, invoices and cancellation they already have.
    def billing_portal(self, *, customer_id: str) -> str: ...

    #: Report metered consumption for the current period. Must AGGREGATE rather than overwrite, so
    #: two reports in one window add up instead of the later one discarding the earlier.
    def report_usage(self, *, customer_id: str, quantity: int,
                     idempotency_scope: str) -> None: ...

    #: Raise an invoice for an amount agreed outside the product - the enterprise path, which is
    #: invoiced rather than checked out. `amount` is in the smallest currency unit.
    def create_invoice(self, *, customer_id: str, amount: int, currency: str,
                       description: str, days_until_due: int) -> Invoice: ...

    #: The invoices this customer has, newest first. Read-only: what an invoice SAYS is the
    #: provider's record, and the product keeps no second copy to disagree with it.
    def list_invoices(self, *, customer_id: str, limit: int = 10) -> list[Invoice]: ...

    #: Prove a delivered webhook body came from the provider, and turn it into a BillingEvent.
    #: Raises EventVerificationFailed when it did not. Implementations must verify over the RAW
    #: bytes: re-encoding the body changes it and invalidates a signature that was in fact valid.
    def verify_event(self, *, payload: bytes, signature: str) -> BillingEvent: ...


# runtime-injected accessor
# The concrete gateway is SELECTED from config by adapters.stripe_billing.build_billing_gateway and
# INJECTED here by runtime composition. Product/application call get_billing_gateway() (this neutral
# accessor) - they never reach for the adapter, which is what keeps a provider swap a composition
# change rather than an edit to every call site.
_gateway: BillingGateway | None = None


def set_billing_gateway(gateway: BillingGateway | None) -> None:
    global _gateway
    _gateway = gateway


def get_billing_gateway() -> BillingGateway:
    if _gateway is None:
        # NOT AN ASSERTION FAILURE but the ordinary state of a deployment that does not charge: a
        # developer checkout, a test, a self-hosted install. The routes turn this into a 503 and the
        # rest of the product serves normally.
        raise BillingUnavailable(
            "no billing gateway configured - runtime composition must call set_billing_gateway()")
    return _gateway
