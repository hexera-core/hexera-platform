# Responsibility: Accept the provider's deliveries, prove they are the provider's, and hand them on.
# Boundaries: proof and acknowledgement only. What an event MEANS is application/billing_service.py.
from __future__ import annotations

import logging

from fastapi import APIRouter, Header, HTTPException, Request

from meshpipeline.application import billing_service
from meshpipeline.contracts.billing import (
    BillingUnavailable,
    EventVerificationFailed,
    get_billing_gateway,
)
from meshpipeline.persistence.session import get_db

log = logging.getLogger(__name__)

router = APIRouter()

#: THE LARGEST BODY THIS ROUTE WILL BUFFER, in bytes.
#:
#: This endpoint is public and exempt from the rate limiter (see api/middleware/hardening.py), which
#: makes the body the one remaining thing an anonymous caller controls before authentication. The
#: signature cannot be checked without the bytes - it is an HMAC over them - so "verify first" is not
#: available; a cap is.
#:
#: 256 KiB is far above anything the provider sends. A fat `invoice.paid` with many line items is a
#: few tens of kilobytes, and the provider's own documented ceiling is well under this - so a body
#: past it is not a Stripe event that grew, it is somebody else.
MAX_WEBHOOK_BODY_BYTES = 256 * 1024


@router.post("")
async def receive(request: Request,
                  stripe_signature: str = Header(default="", alias="Stripe-Signature")) -> dict:
    # THIS ROUTE CARRIES NO `owner_dep` AND MUST NOT. It is declared in
    # _UNAUTHENTICATED_ROUTE_ALLOWED with the reason: the caller is the payment provider, which
    # holds no credential of ours and cannot present one. The SIGNATURE is the authentication, and
    # it is checked below before the body is read as anything but bytes.
    try:
        gateway = get_billing_gateway()
    except BillingUnavailable:
        # FAIL CLOSED. With no gateway there is no secret, and with no secret there is no way to
        # tell the provider from anyone who found the URL. Every handler behind this endpoint writes
        # to the credit ledger, so accepting unverified bodies here would be an unauthenticated
        # grant of credits. An unconfigured deployment refuses rather than trusting.
        log.error("billing webhook received but no gateway is configured")
        raise HTTPException(status_code=503, detail="billing is not configured")

    # THE RAW BYTES, never a parsed model. The signature is an HMAC over the body exactly as sent,
    # so any re-encoding - FastAPI parsing to a dict and something re-serialising it - changes those
    # bytes and invalidates a signature that was in fact valid. That is why this handler takes a
    # `Request` rather than a Pydantic body, and why the bytes are accumulated unchanged below.
    # THE DECLARED LENGTH IS CHECKED FIRST, so an oversized body is refused before it is read rather
    # than after it is already in memory. A caller can lie about this header, which is why the read
    # below is bounded too - but an honest large request is rejected for the cost of parsing an int.
    declared = request.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > MAX_WEBHOOK_BODY_BYTES:
        raise HTTPException(status_code=413, detail="payload too large")

    payload = b""
    async for chunk in request.stream():
        payload += chunk
        if len(payload) > MAX_WEBHOOK_BODY_BYTES:
            # STREAMED AND BOUNDED, not `await request.body()`. The latter buffers whatever arrives
            # in full, so a caller that omits or understates `content-length` would still have the
            # whole thing held in memory before any check ran - which is the concurrency and memory
            # exhaustion this cap exists to stop. Reading in chunks means the refusal lands at the
            # limit rather than at the end.
            raise HTTPException(status_code=413, detail="payload too large")

    try:
        event = gateway.verify_event(payload=payload, signature=stripe_signature)
    except EventVerificationFailed as exc:
        # 400, NOT 500. A bad signature is a rejected caller, and a 5xx would make the provider
        # retry a body that can never verify - the same forged or stale delivery, on a backoff,
        # indefinitely. The reason is logged without the payload: an unverified body is
        # attacker-controlled and does not belong in logs an operator reads.
        log.warning("rejected billing webhook: signature verification failed (%s)",
                    type(exc).__name__)
        raise HTTPException(status_code=400, detail="signature verification failed")

    try:
        async with get_db() as db:
            applied = await billing_service.handle_event(db, event)
    except Exception:
        # 500 SO THE PROVIDER RETRIES. This is the one failure that SHOULD be retried: the event was
        # genuinely theirs and the effect did not land. The idempotency row is written in the same
        # transaction as the effect, so a rolled-back attempt leaves no claim behind and the retry
        # applies cleanly rather than being mistaken for a duplicate.
        log.exception("billing event %s failed to apply; asking the provider to retry", event.id)
        raise HTTPException(status_code=500, detail="event could not be applied")

    # 200 FOR AN IGNORED EVENT TOO. An event type this deployment does not act on, and a duplicate
    # delivery, are both fully handled outcomes - answering anything else would make the provider
    # retry them on a backoff and eventually disable the endpoint for failing.
    return {"received": True, "applied": applied}
