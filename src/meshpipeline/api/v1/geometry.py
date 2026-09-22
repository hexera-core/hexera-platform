# Responsibility: Serve the geometry check to the console - what the scout proposed and the
# pictures it drew - and record what the user confirmed on it, in the session the intake reads.
# Boundaries: transport. It reads and writes the check's stored objects and the session's declared
# fields; it judges nothing, draws nothing and calls no model.
from __future__ import annotations

import json
import logging
import time
import uuid
from datetime import timedelta
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

import meshpipeline.settings.geometry_check as gcfg
from meshpipeline.api.security import org_dep, owner_dep

logger = logging.getLogger(__name__)
router = APIRouter()

STATUS_OFF, STATUS_NONE = "off", "none"
_PICTURE_URL_TTL = timedelta(hours=1)
#: How the confirmation announces itself in the conversation; the intake prompt block names it.
CONFIRMED_MARK = "GEOMETRY CHECK (confirmed by the user):"


class ConfirmedOpening(BaseModel):
    id: int
    name: str = Field(min_length=1, max_length=40)
    role: Literal["inlet", "outlet"]
    centroid_mm: list[float] | None = None
    diameter_mm: float | None = None
    width_mm: float | None = None
    height_mm: float | None = None


class ConfirmIn(BaseModel):
    input_kind: Literal["body-surface", "fluid-domain", "solid-body"]
    flow: Literal["internal", "external"]
    openings: list[ConfirmedOpening] = []
    seed_point_mm: list[float] | None = None
    size_mm: list[float] | None = None
    part: str = Field(default="", max_length=80)


async def _owned_session(session_id: uuid.UUID, owner_id: str, organization_id: str):
    from meshpipeline.persistence.repositories.session_repository import SessionRepository
    from meshpipeline.persistence.session import get_db

    async with get_db() as db:
        session = await SessionRepository().get_for_owner(db, session_id, owner_id,
                                                          organization_id=organization_id)
    if session is None:
        raise HTTPException(404, "Session not found")
    return session


def _read_json(object_key: str) -> dict | None:
    from meshpipeline.contracts.object_storage import ObjectNotFound, get_object_store

    try:
        return json.loads(get_object_store().get_bytes(object_key=object_key))
    except ObjectNotFound:
        return None


@router.get("/{session_id}/check")
async def get_check(session_id: uuid.UUID, owner_id: str = Depends(owner_dep),
                    organization_id: str = Depends(org_dep)) -> dict:
    """The check as it stands: `off` (feature disabled), `none` (not started), `pending`,
    `ready` (with the proposal and picture links), `unsupported` or `failed` (with a reason)."""
    if not gcfg.GEOMETRY_CHECK_ENABLED:
        return {"status": STATUS_OFF}
    await _owned_session(session_id, owner_id, organization_id)
    from meshpipeline.application.geometry_check import check_object_key
    from meshpipeline.contracts.object_storage import get_object_store

    payload = _read_json(check_object_key(str(session_id), "scout.json"))
    if payload is None:
        return {"status": STATUS_NONE}
    if payload.get("status") == "ready":
        store = get_object_store()
        payload["pictures"] = [
            {"name": s["name"], "facing": s.get("facing", []),
             "url": store.create_download_url(object_key=s["object_key"], expires_in=_PICTURE_URL_TTL)}
            for s in payload.get("snapshots", [])]
        payload.pop("snapshots", None)
        payload.pop("facts", None)         # the proposal is what the user acts on; facts are its source
    confirmed = _read_json(check_object_key(str(session_id), "confirmed.json"))
    if confirmed is not None:
        payload["confirmed"] = confirmed
    return payload


def confirmation_message(body: ConfirmIn) -> str:
    """The sentence the intake reads: everything the user confirmed, in plain words, with the
    numbers the builder needs. Its opening phrase is the one the intake prompt block names."""
    kind = {"body-surface": "the part's wall, hollow inside for the fluid",
            "fluid-domain": "the fluid volume itself",
            "solid-body": "a solid body"}[body.input_kind]
    through = "through it" if body.flow == "internal" else "around it"
    parts = [f"{CONFIRMED_MARK} the file is {kind}"
             + (f" ({body.part})" if body.part else "") + f"; the fluid flows {through}."]
    if body.openings:
        rows = []
        for o in body.openings:
            size = (f"{o.diameter_mm:.0f} mm across" if o.diameter_mm
                    else f"{o.width_mm:.0f} x {o.height_mm:.0f} mm" if o.width_mm and o.height_mm else "")
            at = (f" at ({o.centroid_mm[0]:.0f}, {o.centroid_mm[1]:.0f}, {o.centroid_mm[2]:.0f}) mm"
                  if o.centroid_mm and len(o.centroid_mm) == 3 else "")
            rows.append(f"{o.name} ({o.role}){', ' + size if size else ''}{at}")
        parts.append("Openings: " + "; ".join(rows) + ".")
    else:
        parts.append("No openings: the fluid flows around the whole body.")
    if body.seed_point_mm and len(body.seed_point_mm) == 3:
        p = body.seed_point_mm
        parts.append(f"A point inside the flow: ({p[0]:.0f}, {p[1]:.0f}, {p[2]:.0f}) mm.")
    if body.size_mm and len(body.size_mm) == 3:
        s = body.size_mm
        parts.append(f"Part size: {s[0]:.0f} x {s[1]:.0f} x {s[2]:.0f} mm.")
    return " ".join(parts)


def with_declaration(messages: list[dict] | None, message: str) -> list[dict]:
    """The conversation with this confirmation as its only one: an earlier confirmation is
    replaced, not joined, so a retry or a change of mind leaves one declaration for the intake."""
    kept = [m for m in (messages or [])
            if not (m.get("role") == "assistant" and str(m.get("content", "")).startswith(CONFIRMED_MARK))]
    return [*kept, {"role": "assistant", "content": message}]


def patches_from(body: ConfirmIn) -> list[dict]:
    """The session's declared patches, in the shape the intake's submit tool already takes:
    name and role, plus the size and location fields the port binding reads."""
    patches: list[dict] = []
    for o in body.openings:
        entry: dict = {"name": o.name, "type": o.role}
        if o.diameter_mm:
            entry["diameter_mm"] = float(o.diameter_mm)
        elif o.width_mm and o.height_mm:
            entry["width_mm"], entry["height_mm"] = float(o.width_mm), float(o.height_mm)
        if o.centroid_mm and len(o.centroid_mm) == 3:
            entry["near_mm"] = [float(v) for v in o.centroid_mm]
        patches.append(entry)
    if body.flow == "internal" and patches:
        patches.append({"name": "wall", "type": "wall"})
    return patches


@router.post("/{session_id}/check/confirm")
async def confirm_check(session_id: uuid.UUID, body: ConfirmIn, owner_id: str = Depends(owner_dep),
                        organization_id: str = Depends(org_dep)) -> dict:
    """Record what the user confirmed: the session's declared input kind and patches, a message
    in the conversation the intake treats as declared, and a copy in the check's folder."""
    if not gcfg.GEOMETRY_CHECK_ENABLED:
        raise HTTPException(404, "The geometry check is not enabled on this deployment")
    session = await _owned_session(session_id, owner_id, organization_id)
    if session.job_id is not None:
        raise HTTPException(409, "This session has already been dispatched")
    names = [o.name for o in body.openings]
    if len(set(names)) != len(names):
        raise HTTPException(422, "Every opening needs its own name")

    message = confirmation_message(body)
    patches = patches_from(body)
    import tempfile
    from pathlib import Path

    from meshpipeline.application.geometry_check import check_object_key
    from meshpipeline.contracts.object_storage import get_object_store
    from meshpipeline.persistence.repositories.session_repository import SessionRepository
    from meshpipeline.persistence.session import get_db

    # The stored copy goes first. If the store is down the user sees an error and nothing has
    # changed, so pressing the button again is safe; and a second press replaces, never repeats.
    record = {"confirmed_at": time.time(), "owner_id": owner_id, "message": message,
              "patches": patches, **body.model_dump()}
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
        json.dump(record, fh)
        tmp = Path(fh.name)
    try:
        get_object_store().upload_file(local_path=tmp, object_key=check_object_key(str(session_id), "confirmed.json"))
    finally:
        tmp.unlink(missing_ok=True)

    async with get_db() as db:
        # the same tenant-scoped read every request-facing module uses; never the internal one
        session = await SessionRepository().get_for_owner(db, session_id, owner_id,
                                                          organization_id=organization_id)
        if session is None:
            raise HTTPException(404, "Session not found")
        session.input_kind = body.input_kind
        session.intake_patches = patches
        session.messages = with_declaration(session.messages, message)
        await db.commit()
    logger.info("geometry check confirmed - session_id=%s openings=%d", session_id, len(body.openings))
    return {"ok": True, "message": message, "patches": patches}
