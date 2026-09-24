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
from fastapi.responses import Response
from pydantic import BaseModel, Field, field_validator

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
    role: Literal["inlet", "outlet", "not_an_opening"]
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
    # a body in a flow: which way the fluid travels, how long the part is along it, how far the
    # far field reaches in those lengths, and whether the part stands on the ground
    flow_axis: Literal["+x", "-x", "+y", "-y", "+z", "-z", "unknown"] | None = None
    reference_length_mm: float | None = Field(default=None, gt=0)
    extents: dict[str, float] | None = None
    grounded: bool = False

    @field_validator("extents")
    @classmethod
    def _extents_are_positive_lengths(cls, v):
        if v is None:
            return v
        import math
        allowed = {"upstream", "downstream", "lateral", "vertical"}
        bad = sorted(k for k in v if k not in allowed)
        if bad:
            raise ValueError(f"unknown far-field margin(s): {', '.join(bad)}")
        for k, x in v.items():
            if not (isinstance(x, (int, float)) and math.isfinite(x) and x > 0):
                raise ValueError(f"the {k} margin must be a positive number of body lengths")
        return {k: float(x) for k, x in v.items()}


# The hold's words and rule live with the application; the route re-exports them for its callers.
from meshpipeline.application.geometry_hold import (  # noqa: E402
    CONTINUE_TEXT,
    DRAWING_MARK,
    HOLD_REPLY,
    purpose_from,
    should_hold,
)

REEXPORTED = (CONTINUE_TEXT, DRAWING_MARK, HOLD_REPLY, purpose_from, should_hold)


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
    from meshpipeline.application.geometry_check import check_object_key, naming_requested
    from meshpipeline.contracts.object_storage import get_object_store

    payload = _read_json(check_object_key(str(session_id), "scout.json"))
    if payload is None:
        return {"status": STATUS_NONE}
    from meshpipeline.contracts.geometry_fields import form_spec
    payload["fields"] = form_spec()
    payload.setdefault("named", payload.get("status") == "ready")
    # the naming starts with the user's first answer: until then the stage must say it is
    # waiting for the chat, not that the model is working
    try:
        payload["naming_requested"] = naming_requested(str(session_id)) is not None
    except Exception as exc:  # noqa: BLE001 - a marker we cannot read is "not asked yet", never a broken check
        logger.warning("geometry check: naming marker unreadable (%s) - session_id=%s", exc, session_id)
        payload["naming_requested"] = False
    if payload.get("status") in ("ready", "scouted"):
        # the stage opens on a scouted check too, with the code's labels; the pictures are for
        # the card, which only shows once the check is ready
        if payload.get("status") == "ready":
            store = get_object_store()
            payload["pictures"] = [
                {"name": s["name"], "facing": s.get("facing", []),
                 "url": store.create_download_url(object_key=s["object_key"], expires_in=_PICTURE_URL_TTL)}
                for s in payload.get("snapshots", [])]
        payload.pop("snapshots", None)
        payload.pop("facts", None)         # the proposal is what the user acts on; facts are its source
        payload["skin"] = bool(payload.pop("skin_key", None))   # whether the 3D stage can open
    confirmed = _read_json(check_object_key(str(session_id), "confirmed.json"))
    if confirmed is not None:
        payload["confirmed"] = confirmed
    return payload


@router.get("/{session_id}/check/skin")
async def get_check_skin(session_id: uuid.UUID, owner_id: str = Depends(owner_dep),
                         organization_id: str = Depends(org_dep)) -> Response:
    """The part's skin as the viewer draws it - the structure a delivered surface has - for the
    stage the user turns the part in. 404 when the check stored none.

    The stored bytes are handed through as they are: the worker already wrote the JSON the viewer
    reads, so decoding and re-encoding it here would only make copies. The skin is drawn at a
    picture's tessellation (a few thousand triangles, a few hundred kilobytes; the elbow is
    4,844 and 0.23 MB) and the app's gzip middleware compresses it on the way out."""
    if not gcfg.GEOMETRY_CHECK_ENABLED:
        raise HTTPException(404, "The geometry check is not enabled on this deployment")
    await _owned_session(session_id, owner_id, organization_id)
    from meshpipeline.application.geometry_check import check_object_key
    from meshpipeline.contracts.object_storage import ObjectNotFound, get_object_store

    try:
        raw = get_object_store().get_bytes(object_key=check_object_key(str(session_id), "skin.json"))
    except ObjectNotFound:
        raise HTTPException(404, "No skin has been stored for this session's geometry check") from None
    return Response(content=raw, media_type="application/json")


def confirmation_message(body: ConfirmIn) -> str:
    """The sentence the intake reads: everything the user confirmed, in plain words, with the
    numbers the builder needs. Its opening phrase is the one the intake prompt block names."""
    kind = {"body-surface": "the part's wall, hollow inside for the fluid",
            "fluid-domain": "the fluid volume itself",
            "solid-body": "a solid body"}[body.input_kind]
    through = "through it" if body.flow == "internal" else "around it"
    parts = [f"{CONFIRMED_MARK} the file is {kind}"
             + (f" ({body.part})" if body.part else "") + f"; the fluid flows {through}."]
    ports = [o for o in body.openings if o.role != "not_an_opening"]
    if body.flow == "external":
        from meshpipeline.contracts.geometry_fields import external_declaration
        parts.append("No openings: the fluid flows around the whole body.")
        parts.extend(external_declaration(body))
    elif ports:
        rows = []
        for o in ports:
            size = (f"{o.diameter_mm:.0f} mm across" if o.diameter_mm
                    else f"{o.width_mm:.0f} x {o.height_mm:.0f} mm" if o.width_mm and o.height_mm else "")
            at = (f" at ({o.centroid_mm[0]:.0f}, {o.centroid_mm[1]:.0f}, {o.centroid_mm[2]:.0f}) mm"
                  if o.centroid_mm and len(o.centroid_mm) == 3 else "")
            rows.append(f"{o.name} ({o.role}){', ' + size if size else ''}{at}")
        parts.append("Openings: " + "; ".join(rows) + ".")
        skipped = [str(o.id) for o in body.openings if o.role == "not_an_opening"]
        if skipped:
            parts.append(f"Sticker{'s' if len(skipped) > 1 else ''} {', '.join(skipped)}: not an opening (a hole or a face the fluid does not pass).")
    else:
        # the fluid flows through the part but no port was confirmed: the intake must ask
        parts.append("No openings were confirmed on the picture"
                     + (" (every sticker was marked not an opening)" if body.openings else "")
                     + "; ask the user where the fluid enters and leaves.")
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
    if body.flow == "external":
        return patches
    for o in body.openings:
        if o.role == "not_an_opening":
            continue
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
    # THE INTAKE PICKS UP FROM HERE. The hold left the conversation waiting on this button; one
    # ordinary chat turn, in the user's name, lets the intake ask what is still missing. A turn
    # that fails leaves the confirmation in place; the user's next message runs it again.
    nxt = None
    try:
        from meshpipeline.api.schemas.chat import ChatMessageIn
        from meshpipeline.api.v1.chat import chat_message
        turn = await chat_message(ChatMessageIn(session_id=session_id, content=CONTINUE_TEXT),
                                  owner_id=owner_id, organization_id=organization_id)
        nxt = turn.model_dump(mode="json")
    except Exception as exc:  # noqa: BLE001 - the confirmation stands; the next message resumes
        logger.warning("geometry check: the intake could not continue after confirm (%s: %s) - session_id=%s",
                       type(exc).__name__, exc, session_id)
    return {"ok": True, "message": message, "patches": patches, "continued_with": CONTINUE_TEXT, "next": nxt}
