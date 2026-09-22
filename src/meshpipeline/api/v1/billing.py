# Responsibility: Let a proven caller start a checkout, open the billing portal, and read their plan.
# Boundaries: HTTP shape and refusals only; what a checkout MEANS is application/billing_service.py.
from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

import meshpipeline.settings.billing as billcfg
import meshpipeline.settings.plans as plancfg
from meshpipeline.api.security import org_dep, owner_dep
from meshpipeline.application import billing_service
from meshpipeline.contracts.billing import BillingUnavailable
from meshpipeline.persistence.session import get_db

router = APIRouter()


class CheckoutRequest(BaseModel):
    plan: str


def _organization(organization_id: str) -> uuid.UUID:
    # THE NARROWING THE MUTATING ROUTES SHARE, and the one place billing differs from credits.py. A
    # balance degrades to 0 for an organisation-less caller because a number can be wrong and still
    # harmless; a checkout cannot degrade, because the alternative to refusing is charging a card
    # that cannot be attributed to any tenant. This refuses instead.
    if not organization_id:
        raise HTTPException(status_code=409, detail="this caller has no organisation to bill")
    try:
        return uuid.UUID(organization_id)
    except ValueError:
        raise HTTPException(status_code=409, detail="this caller has no organisation to bill")


@router.get("/plans")
async def read_plans() -> dict:
    # THE CATALOGUE AS THIS DEPLOYMENT SEES IT, so the console renders what can actually be bought
    # here rather than a list compiled into the browser bundle. A tier whose price id is unset
    # reports `purchasable: false` instead of being hidden: an operator looking at a tier missing
    # from their own console needs to see that it exists and is unconfigured.
    return {
        "plans": [
            {
                "name": name,
                "max_jobs_per_owner": limits.max_jobs_per_owner,
                "max_concurrent_jobs": limits.max_concurrent_jobs,
                "rate_limit_per_minute": limits.rate_limit_per_minute,
                "included_credits": limits.included_credits,
                "purchasable": bool(billcfg.price_for(name)[0]),
            }
            for name, limits in ((plan, plancfg.limits_for(plan)) for plan in plancfg.PLANS)
        ],
        # PRICES ARE NOT REPORTED HERE. What a tier COSTS lives with the provider, which is the one
        # place it can change without a deploy; copying an amount into this response would create a
        # second answer that silently disagrees the first time someone edits the price.
        "billing_enabled": billcfg.enabled(),
    }


@router.get("")
async def read_subscription(organization_id: Annotated[str, Depends(org_dep)] = "") -> dict:
    # READ-ONLY and degradable, like credits.py: an organisation-less caller reads the free shape
    # rather than an error, because showing a billing page is not a privileged action.
    if not organization_id:
        return billing_service.unsubscribed()
    try:
        organization = uuid.UUID(organization_id)
    except ValueError:
        # THE SAME NARROWING credits.py applies, and for the same reason: a Principal carries the
        # organisation as a string, and a value that is not a uuid is one this route cannot scope
        # on. It reads as unsubscribed, never as a 500 for a caller who has already proven who it is.
        return billing_service.unsubscribed()

    async with get_db() as db:
        return await billing_service.read_subscription(db, organization_id=organization)


@router.post("/checkout")
async def start_checkout(body: CheckoutRequest,
                         organization_id: Annotated[str, Depends(org_dep)] = "",
                         # `owner_dep` IS WHAT test_job_mutating_routes_require_authentication looks
                         # for in the AST, and it resolves from the same cached principal org_dep
                         # does - one credential check, not two. It is also the value used below:
                         # owner_id is the lowercased email everywhere in this schema, which is what
                         # the provider's customer is created with. Taking it from the proven
                         # principal rather than the request body is the point - a body-supplied
                         # address would let a caller attach their organisation's billing to
                         # somebody else's inbox.
                         owner_id: str = Depends(owner_dep)) -> dict:
    organization = _organization(organization_id)
    async with get_db() as db:
        try:
            url = await billing_service.start_checkout(
                db, organization_id=organization, plan=body.plan, email=owner_id)
        except BillingUnavailable:
            raise HTTPException(status_code=503, detail="billing is not configured")
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
    return {"url": url}


@router.post("/portal")
async def open_portal(organization_id: Annotated[str, Depends(org_dep)] = "",
                      # Present for the same reason as above: this route acts for a caller who is
                      # already known, and the fitness test reads the dependency rather than
                      # trusting the comment. The value itself is not needed here - the portal is
                      # addressed by the organisation's customer id, not by the actor.
                      owner_id: str = Depends(owner_dep)) -> dict:
    organization = _organization(organization_id)
    async with get_db() as db:
        try:
            url = await billing_service.portal_url(db, organization_id=organization)
        except BillingUnavailable:
            raise HTTPException(status_code=503, detail="billing is not configured")
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc))
    return {"url": url}
