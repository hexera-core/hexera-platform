# Responsibility: Answer an operator's cross-tenant billing questions, and raise an invoice by hand.
# Boundaries: HTTP shape, the admin credential, and refusals. Every figure is computed by a
#             repository or the gateway; this module totals nothing itself.
#
# THIS IS THE ONLY UNSCOPED SURFACE IN THE PRODUCT. Every other route resolves a Principal and
# filters on its tenant; these deliberately read across all of them, because "which customers are
# past due" is not a question one tenant can ask. That is why the credential below is its own
# secret and why a blank one refuses rather than degrades.
from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, Field

import meshpipeline.settings.billing as billcfg
import meshpipeline.settings.plans as plancfg
import meshpipeline.settings.policy as polcfg
from meshpipeline.application import billing_service
from meshpipeline.contracts.billing import BillingUnavailable, get_billing_gateway
from meshpipeline.persistence.session import get_db

router = APIRouter()

#: Held to the same shape as the product's other refusals: it says what is wrong without saying
#: whether a key exists, which is what a caller probing for one would learn from the difference.
_REFUSED = "admin credential required"


async def admin_dep(x_admin_key: Annotated[str, Header(alias="X-Admin-Key")] = "") -> None:
    # CONSTANT-TIME COMPARISON, because this one is worth it: unlike a per-tenant key, a successful
    # guess here reads every customer's billing at once, so the timing side channel is worth closing
    # even though it is a hard attack.
    import hmac

    configured = polcfg.ADMIN_API_KEY.strip()
    if not configured:
        # FAIL CLOSED, and 404 rather than 503. A deployment that has not enabled cross-tenant
        # access should not advertise that the capability exists and is merely unconfigured.
        raise HTTPException(status_code=404, detail="not found")
    if not hmac.compare_digest(x_admin_key.strip(), configured):
        raise HTTPException(status_code=403, detail=_REFUSED)


class InvoiceRequest(BaseModel):
    #: THE SMALLEST CURRENCY UNIT, never a decimal. A float amount is how an invoice total stops
    #: matching what was agreed; `gt=0` because a zero or negative invoice is a credit note, which
    #: is a different object with different rules.
    amount: int = Field(gt=0)
    currency: str = Field(default="usd", max_length=3)
    description: str = Field(max_length=256)
    days_until_due: int = Field(default=30, ge=1, le=365)


@router.get("/organizations", dependencies=[Depends(admin_dep)])
async def list_organizations(limit: int = 100) -> dict:
    # THE OPERATOR'S LIST. Balance comes back with the row from one query rather than a lookup per
    # organisation - see list_with_billing for why an organisation with no ledger entries still
    # appears here with 0.
    bounded = max(1, min(limit, 500))
    async with get_db() as db:
        rows = await organization_repo().list_with_billing(db, limit=bounded)
    return {
        "organizations": [
            {
                "id": str(org.id),
                "name": org.name,
                "slug": org.slug,
                "plan": org.plan,
                "status": org.subscription_status,
                "included_credits": plancfg.limits_for(org.plan).included_credits,
                "balance": balance,
                "has_billing_account": bool(org.stripe_customer_id),
                # THE PROVIDER'S ID IS SHOWN. It is not a secret - it is the join key an operator
                # needs to open the same customer in the provider's dashboard, which is where every
                # question this page raises is actually answered.
                "stripe_customer_id": org.stripe_customer_id or "",
                "current_period_end": (org.current_period_end.isoformat()
                                       if org.current_period_end else None),
                "created_at": org.created_at.isoformat() if org.created_at else None,
            }
            for org, balance in rows
        ],
        "billing_enabled": billcfg.enabled(),
    }


@router.get("/plans", dependencies=[Depends(admin_dep)])
async def list_plans() -> dict:
    # THE SAME CATALOGUE THE CUSTOMER-FACING ROUTE SERVES, deliberately - two answers to "what are
    # the tiers" is how an operator ends up debugging a difference that is only in the reading.
    from meshpipeline.api.v1 import billing as customer_billing

    return await customer_billing.read_plans()


@router.get("/usage", dependencies=[Depends(admin_dep)])
async def read_usage(months: int = 6) -> dict:
    bounded = max(1, min(months, 36))
    async with get_db() as db:
        periods = await ledger_repo().usage_by_period(db, months=bounded)
        pending = await ledger_repo().unmetered_total(db)
    # THE METER BACKLOG travels with the periods because it is the caveat on them: a large pending
    # figure means the spend column is real but has not reached an invoice yet.
    return {"periods": periods, "pending_meter": pending}


@router.get("/organizations/{organization_id}/ledger", dependencies=[Depends(admin_dep)])
async def read_ledger(organization_id: str, limit: int = 50) -> dict:
    organization = _parse(organization_id)
    bounded = max(1, min(limit, 500))
    async with get_db() as db:
        rows = await ledger_repo().list_for_org(db, organization_id=organization, limit=bounded)
    return {
        "entries": [
            {
                "id": str(row.id),
                "entry_type": getattr(row.entry_type, "value", row.entry_type),
                #: SIGNED, exactly as stored, so a reader sums the column rather than branching.
                "amount": row.amount,
                "reason": row.reason,
                "created_at": row.created_at.isoformat() if row.created_at else None,
                # WHETHER THIS DEBIT REACHED THE METER. Null on a grant or refund forever, and on a
                # debit until the sweep reports it - which is the single most useful field here when
                # a customer's bill looks too small.
                "metered_at": row.metered_at.isoformat() if row.metered_at else None,
            }
            for row in rows
        ],
    }


@router.get("/organizations/{organization_id}/invoices", dependencies=[Depends(admin_dep)])
async def read_invoices(organization_id: str, limit: int = 10) -> dict:
    organization = _parse(organization_id)
    async with get_db() as db:
        row = await organization_repo().get_by_id(db, organization)
    if row is None or not row.stripe_customer_id:
        # NOT AN ERROR. An organisation that never checked out has no customer and therefore no
        # invoices; an empty list is the true answer, and a 404 would read as "no such tenant".
        return {"invoices": [], "reason": "this organisation has no billing account"}
    try:
        invoices = get_billing_gateway().list_invoices(
            customer_id=row.stripe_customer_id, limit=max(1, min(limit, 100)))
    except BillingUnavailable:
        raise HTTPException(status_code=503, detail="billing is not configured")
    return {"invoices": [_invoice(inv) for inv in invoices]}


@router.post("/organizations/{organization_id}/invoices", dependencies=[Depends(admin_dep)])
async def raise_invoice(organization_id: str, body: InvoiceRequest) -> dict:
    # THE ENTERPRISE PATH MADE EXPLICIT. `enterprise` has no price id and cannot be checked out, so
    # this is how that customer is actually billed - by an operator, for an amount agreed off the
    # product, on terms.
    organization = _parse(organization_id)
    async with get_db() as db:
        row = await organization_repo().get_by_id(db, organization)
        if row is None:
            raise HTTPException(status_code=404, detail="organisation not found")
        customer_id = row.stripe_customer_id
        if not customer_id:
            # A CUSTOMER IS CREATED FOR AN INVOICE, exactly as one is for a checkout - an enterprise
            # account may never have touched the self-serve flow, and refusing here would mean the
            # only way to bill them is to first make them buy something they do not want.
            try:
                gateway = get_billing_gateway()
            except BillingUnavailable:
                raise HTTPException(status_code=503, detail="billing is not configured")
            customer_id = gateway.ensure_customer(
                organization_id=str(organization), email="", name=row.name)
            await organization_repo().attach_customer(
                db, organization_id=organization, stripe_customer_id=customer_id)

    try:
        invoice = get_billing_gateway().create_invoice(
            customer_id=customer_id, amount=body.amount, currency=body.currency.lower(),
            description=body.description, days_until_due=body.days_until_due)
    except BillingUnavailable:
        raise HTTPException(status_code=503, detail="billing is not configured")
    return {"invoice": _invoice(invoice)}


@router.post("/meter/sweep", dependencies=[Depends(admin_dep)])
async def sweep_meter(limit: int = 0) -> dict:
    # THE METER SWEEP, REACHABLE OVER HTTP, because on the hosted deployment nothing else runs it.
    #
    # The sweep is also registered on Celery Beat, which is what drives it under docker-compose. The
    # hosted deployment has no Beat and its worker listens only to `simulation_jobs`, so a task
    # scheduled onto `cleanup_tasks` there is enqueued by nobody and drained by nobody. For cleanup
    # that is untidy; for metering it means debits accumulate and no overage ever reaches the
    # provider - silently, because an unrun sweep produces no error to notice.
    #
    # This route makes the API - which is already deployed, already holds the database and the
    # gateway, and already has a credential of its own - the thing a scheduler can point at. Cloud
    # Scheduler calls it with X-Admin-Key on whatever cadence the deployment wants.
    #
    # IT IS SAFE TO CALL AT ANY TIME, from anywhere, as often as anyone likes: each debit carries its
    # own idempotency key, so a sweep that overlaps another re-presents keys the provider has already
    # seen and the provider replays instead of double-charging.
    from meshpipeline.application import metering_service

    bounded = metering_service.SWEEP_BATCH if limit <= 0 else max(1, min(limit, 1000))
    async with get_db() as db:
        return await metering_service.report_pending_usage(db, limit=bounded)


@router.get("/organizations/{organization_id}", dependencies=[Depends(admin_dep)])
async def read_organization(organization_id: str) -> dict:
    organization = _parse(organization_id)
    async with get_db() as db:
        return await billing_service.read_subscription(db, organization_id=organization)


def _parse(organization_id: str) -> uuid.UUID:
    try:
        return uuid.UUID(organization_id)
    except ValueError:
        # 404, NOT 400. To an operator pasting an id, "that is not a tenant" and "that is not a
        # uuid" are the same discovery, and the narrower status says which one only to someone
        # enumerating.
        raise HTTPException(status_code=404, detail="organisation not found")


def _invoice(invoice) -> dict:
    return {
        "id": invoice.id,
        "number": invoice.number,
        "status": invoice.status,
        "amount_due": invoice.amount_due,
        "amount_paid": invoice.amount_paid,
        "currency": invoice.currency,
        "created_at": invoice.created_at.isoformat() if invoice.created_at else None,
        "hosted_url": invoice.hosted_url,
    }


def organization_repo():
    # FUNCTION SCOPE, like api_keys.py's own use of tenant_scope: an API module may not import a
    # persistence repository at module scope (test_route_transport_only).
    from meshpipeline.persistence.repositories.organization_repository import (
        OrganizationRepository,
    )
    return OrganizationRepository()


def ledger_repo():
    from meshpipeline.persistence.repositories.credit_ledger_repository import (
        CreditLedgerRepository,
    )
    return CreditLedgerRepository()
