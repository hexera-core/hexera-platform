# Responsibility: Establish who is calling, refuse a caller who cannot prove it, and hold a keyed caller to its plan's rate.
# Boundaries: identity and the limits that identity carries; what it may then do is the route's question.
from __future__ import annotations

import hmac
import logging
import time
from hashlib import sha256
from typing import Annotated

from fastapi import Depends, Header, HTTPException

import meshpipeline.settings.policy as polcfg
from meshpipeline.application import account_service
from meshpipeline.contracts import api_key
from meshpipeline.contracts.identity import Credential, Principal
from meshpipeline.contracts.rate_limit import incr_window
from meshpipeline.persistence.session import get_db
from meshpipeline.settings import plans

logger = logging.getLogger(__name__)

_BEARER_SCHEME = "bearer"
#: > one 60s window, so a window outlives its own counting (as in middleware/hardening.py)
_WINDOW_TTL_SECONDS = 90


def expected_user_sig(user_id: str) -> str:
    return hmac.new(
        polcfg.USER_TOKEN_SECRET.encode(), (user_id or "").encode(), sha256
    ).hexdigest()


def verify_identity(x_api_key: str | None, x_user_id: str | None,
                    x_user_sig: str | None) -> str:
    if polcfg.MESH_API_KEY:
        if not x_api_key or not hmac.compare_digest(x_api_key, polcfg.MESH_API_KEY):
            raise HTTPException(status_code=401, detail="Invalid or missing X-API-Key header")

    user_id = (x_user_id or "").strip()

    if polcfg.USER_TOKEN_SECRET:
        if not user_id:
            raise HTTPException(status_code=401, detail="Missing X-User-Id header")
        if not x_user_sig or not hmac.compare_digest(x_user_sig, expected_user_sig(user_id)):
            raise HTTPException(status_code=401, detail="Invalid or missing X-User-Sig for the given X-User-Id")
        return user_id

    # Dev / single-tenant: identity is self-asserted (documented residual).
    return user_id or (x_api_key or "dev-user")


def _bearer_credential(authorization: str | None) -> str:
    scheme, _, value = (authorization or "").strip().partition(" ")
    return value.strip() if scheme.lower() == _BEARER_SCHEME else ""


async def resolve_principal(authorization: str | None, x_api_key: str | None,
                            x_user_id: str | None, x_user_sig: str | None) -> Principal:
    # THE ONE SEAM. Every credential this product accepts becomes a Principal here and nowhere
    # else, so the rest of the system knows only "who is calling", never which header proved it.
    # The indirection is what makes the tenant boundary cheap to widen: when organisations arrive,
    # this function starts filling in `organization_id` and every call site is already reading the
    # answer from a Principal instead of pulling an owner id out of a header.
    presented = _bearer_credential(authorization)
    if presented:
        return await _principal_from_key(presented)

    owner_id = verify_identity(x_api_key, x_user_id, x_user_sig)
    return Principal(owner_id=owner_id,
                     organization_id=await _organization_for(owner_id),
                     credential=Credential.signed_header if polcfg.USER_TOKEN_SECRET
                     else Credential.self_asserted)


async def _organization_for(owner_id: str) -> str:
    # THE ONE PLACE a header credential's tenant is resolved. One indexed read per request; a
    # cache belongs here and nowhere else, which is why the journey is a single call rather than
    # a join written at each call site.
    #
    # A key credential does NOT come through here: its row already names an organisation, and a
    # key may be scoped to one while its owner belongs to several.
    if not owner_id:
        return ""
    try:
        async with get_db() as db:
            return await account_service.organization_id_for_owner(db, owner_id)
    except Exception as exc:
        # FAIL OPEN TO OWNER SCOPE, never to a refusal. The organisation is a scope, not a
        # credential: the caller has already proven who they are, and a lookup that cannot run
        # must degrade to today's owner-only behaviour rather than 500 a proven request. Reads
        # fall back to owner_id when the principal names no organisation (see the repositories).
        #
        # LOGGED AT `error`, NOT `warning`, because failing open here is not symmetric. On a READ
        # it costs nothing durable - the request is answered owner-scoped and the next one is
        # answered correctly. On a WRITE the same empty string reaches `tenant_scope.stamp`,
        # which writes owner_id alone, and that row is then invisible to every later org-scoped
        # read, forever: 0004's backfill is a one-shot that has already run, so nothing
        # re-stamps it. A transient database blip therefore leaves permanent damage behind, and
        # rows like these would also block 0005's NOT NULL. This must page somebody, not sit in
        # a warning stream. Repairing them means re-running 0004's stamping UPDATEs; see
        # docs/deployment/identity-platform.md section 7a.
        logger.error("could not resolve an organisation for %s - scoping on owner alone, and any "
                     "write in this request will be stamped with the owner only: %s",
                     owner_id, exc)
        return ""


async def _principal_from_key(presented: str) -> Principal:
    from meshpipeline.application import api_key_service

    # Shape first, and refuse on shape alone. A bearer token that is not one of ours must not cost
    # a database session: anyone may send an Authorization header, so that would be a free way to
    # exhaust the connection pool. The service checks the shape again for its own callers.
    if api_key.parse(presented) is None:
        raise HTTPException(status_code=401, detail="Invalid, revoked or expired API key")

    async with get_db() as db:
        principal = await api_key_service.authenticate(db, presented)
    if principal is None:
        # One refusal for every cause. A caller learns that this credential does not work, not
        # which part of it was wrong.
        raise HTTPException(status_code=401, detail="Invalid, revoked or expired API key")
    await _enforce_plan_rate(principal)
    return principal


async def _enforce_plan_rate(principal: Principal) -> None:
    # Applied HERE because this is the first point at which the plan is known - the transport
    # middleware limits by header or client address, before any credential has been read. A
    # keyed caller is therefore held to its own plan's rate rather than to whatever it shares
    # an egress address with.
    limit = plans.limits_for(principal.plan).rate_limit_per_minute
    if limit <= 0:
        return
    window = int(time.time() // 60)
    try:
        used = await incr_window(f"plan:{principal.owner_id}", window, _WINDOW_TTL_SECONDS)
    except Exception as exc:
        # Fail open, exactly as the transport limiter does: an unavailable counter must not turn
        # into an outage for callers who have proven who they are.
        logger.warning("plan rate limiter unavailable - failing open: %s", exc)
        return
    if used > limit:
        raise HTTPException(status_code=429, detail="Rate limit exceeded, retry shortly",
                            headers={"Retry-After": str(60 - int(time.time() % 60))})


async def principal_dep(
    authorization: Annotated[str | None, Header()] = None,
    x_api_key: Annotated[str | None, Header()] = None,
    x_user_id: Annotated[str | None, Header()] = None,
    x_user_sig: Annotated[str | None, Header()] = None,
) -> Principal:
    return await resolve_principal(authorization, x_api_key, x_user_id, x_user_sig)


async def owner_dep(
    # FastAPI resolves this ONCE per request and shares it with every other dependency that asks
    # for it, so a route taking both the owner and the plan verifies the key once - one database
    # read, one recorded use, one rate-limit unit. It is optional only so that an in-process
    # caller with headers in hand can still resolve an identity without an ASGI request.
    principal: Annotated[Principal | None, Depends(principal_dep)] = None,
    x_api_key: Annotated[str | None, Header()] = None,
    x_user_id: Annotated[str | None, Header()] = None,
    x_user_sig: Annotated[str | None, Header()] = None,
) -> str:
    if principal is None:
        principal = await resolve_principal(None, x_api_key, x_user_id, x_user_sig)
    return principal.owner_id


async def plan_dep(principal: Annotated[Principal, Depends(principal_dep)]) -> str:
    return principal.plan


async def org_dep(principal: Annotated[Principal, Depends(principal_dep)]) -> str:
    # Beside owner_dep, resolved from the SAME cached principal - so a route taking both gets one
    # credential check and one organisation lookup, not two.
    return principal.organization_id
