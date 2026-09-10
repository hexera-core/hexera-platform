# Responsibility: Tell the browser what this deployment supports.
# Boundaries: published capability facts only - never a credential, and never a value the client is not entitled to.
from __future__ import annotations

from fastapi import APIRouter

import meshpipeline.settings.policy as polcfg
from meshpipeline.contracts.intake_formats import capability_payload

router = APIRouter()


@router.get("")
async def client_config() -> dict:
    return {
        # What the browser may OFFER. Advisory only: the server validates every upload, so a
        # client that cannot read this is not a hole - it simply offers everything and lets the
        # upload be refused.
        "intake": capability_payload(),
        # WHETHER THE CONSOLE SHOULD OFFER SIGN-UP. Advisory, exactly like `intake` above: the
        # real gate is on POST /auth/session, because anyone can create an Identity Platform
        # account against the project's public web API key without asking this endpoint first.
        "auth": {
            "signup_enabled": polcfg.CONSOLE_SIGNUP_ENABLED,
        },
        "viewer": {
            "grid_px":          polcfg.VIEWER_GRID_PX,
            "fine_fill":        polcfg.VIEWER_FINE_FILL,
            "frame_frac":       polcfg.VIEWER_FRAME_FRAC,
            "flag_span_factor": polcfg.VIEWER_FLAG_SPAN_FACTOR,
            "max_flags":        polcfg.DISPUTE_MAX_FLAGS,
        },
    }
