# Responsibility: Turn the console's Identity Platform token into the identity the rest of this API already understands.
# Owns: the only endpoint that accepts a credential the product did not mint, and the refusal it answers with.
# Boundaries: identity only - it never decides what the caller may then do.
from __future__ import annotations

import hmac
import logging
from typing import Annotated

from fastapi import APIRouter, Body, Header, HTTPException

import meshpipeline.settings.policy as polcfg
from meshpipeline.application import account_service
from meshpipeline.contracts import firebase_token
from meshpipeline.persistence.session import get_db

logger = logging.getLogger(__name__)

router = APIRouter()

#: ONE refusal for every cause: an absent token, a malformed one, a forged one, an expired one,
#: one addressed to another project. The difference is only useful to somebody probing which of
#: their guesses is close - the same discipline api_key_service.authenticate applies.
REFUSAL = "Invalid or expired sign-in token"


@router.post("/auth/session")
async def create_session(
    id_token: Annotated[str, Body(embed=True)] = "",
    x_api_key: Annotated[str | None, Header()] = None,
) -> dict:
    # THE INTERNAL GATE, checked before anything else. This endpoint accepts a credential minted
    # outside the product, so it must not be a surface the public can reach even to have refused.
    # An unset MESH_API_KEY is local dev, where nothing is gated - identical to verify_identity.
    if polcfg.MESH_API_KEY:
        if not x_api_key or not hmac.compare_digest(x_api_key, polcfg.MESH_API_KEY):
            raise HTTPException(status_code=401, detail="Invalid or missing X-API-Key header")

    # Refused here too, not only inside firebase_token.verify: a deployment with no project
    # configured must refuse before it even asks the verifier, the same "shape first, refuse
    # on shape alone" discipline security._principal_from_key applies to a malformed API key -
    # cheap and certain beats trusting a downstream call to catch the same thing every time.
    if not polcfg.FIREBASE_PROJECT_ID:
        logger.info("sign-in refused: token did not verify")
        raise HTTPException(status_code=401, detail=REFUSAL)

    try:
        verified = firebase_token.verify(id_token,
                                         project_id=polcfg.FIREBASE_PROJECT_ID)
    except firebase_token.InvalidToken:
        # Deliberately NOT logged with the token, the exception message or the email. The first
        # is a live credential, and the third would put an address into the log of every failed
        # attempt - including attempts by people who do not have an account here.
        logger.info("sign-in refused: token did not verify")
        raise HTTPException(status_code=401, detail=REFUSAL) from None

    try:
        async with get_db() as db:
            account = await account_service.resolve_or_provision(db, verified)
    except account_service.SignupDisabled:
        # 403, not 401, and the one refusal that says something. A user can act on "this
        # deployment is not open"; it discloses no account existence, because it is the answer
        # for every unrecognised token alike.
        raise HTTPException(
            status_code=403,
            detail="This deployment is not accepting new accounts") from None

    return {
        "user_id": account.user_id,
        "owner_id": account.owner_id,
        "organization_id": account.organization_id,
        "email_verified": account.email_verified,
        "name": account.name,
    }
