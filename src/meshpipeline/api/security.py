# Responsibility: Establish who is calling, and refuse a caller who cannot prove it.
# Boundaries: identity only.
from __future__ import annotations

import hmac
import logging
from hashlib import sha256
from typing import Annotated

from fastapi import Header, HTTPException

import meshpipeline.settings.policy as polcfg

logger = logging.getLogger(__name__)


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


async def owner_dep(
    x_api_key: Annotated[str | None, Header()] = None,
    x_user_id: Annotated[str | None, Header()] = None,
    x_user_sig: Annotated[str | None, Header()] = None,
) -> str:
    return verify_identity(x_api_key, x_user_id, x_user_sig)
