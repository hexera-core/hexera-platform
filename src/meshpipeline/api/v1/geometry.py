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


#: The first words of the holding line the chat gives while the part is being drawn. The intake
#: prompt names it so nothing in it is ever read as declared.
DRAWING_MARK = "GEOMETRY CHECK (drawing your part):"
HOLD_REPLY = (
    f"{DRAWING_MARK} thanks - I'm drawing your part now. It will appear beside this chat in a few "
    "seconds with a numbered sticker on every opening I found. Check the names, fix anything "
    "wrong, and press Proceed. Then I'll ask only what is still missing."
)
CONTINUE_TEXT = "I confirmed the geometry check. Go on."


def should_hold(stored: dict | None, requested: dict | None, confirmed: dict | None) -> bool:
    """Whether this chat turn is the one the naming waits for: a check exists and is not
    terminal, nobody has asked the model yet, and nothing was confirmed. Pure, so the rule is
    testable without a store."""
    if stored is None or requested is not None or confirmed is not None:
        return False
    return stored.get("status") in ("pending", "scouted")


def purpose_from(messages: list[dict] | None) -> str:
    """Everything the user has said so far, for the model: the answer to the opening question,
    and the unit answer when there was one."""
    return "\n".join(str(m.get("content", "")).strip() for m in (messages or [])
                      if m.get("role") == "user" and str(m.get("content", "")).strip())[:2000]


async def hold_for_naming(session, owner_id: str, organization_id: str):
    """Called by the chat turn once the user's message is stored. When this is the first answer
    the naming waits for, hand the user's words to the naming step, leave the holding line in the
    conversation, and return it; otherwise None and the intake runs as usual."""
    if not gcfg.GEOMETRY_CHECK_ENABLED:
        return None
    from meshpipeline.application.geometry_check import (
        check_object_key,
        mark_naming_requested,
        naming_requested,
        read_check,
    )
    from meshpipeline.contracts.geometry_check import enqueue_naming

    sid = str(session.id)
    stored = read_check(sid)
    if not should_hold(stored, naming_requested(sid), _read_json(check_object_key(sid, "confirmed.json"))):
        return None
    purpose = purpose_from(session.messages)
    queued = enqueue_naming(session_id=sid, owner_id=owner_id, purpose_text=purpose,
                            interpretation=await _interpretation_payload(session, owner_id, organization_id))
    if not queued:
        return None
    mark_naming_requested(sid, purpose)
    from meshpipeline.persistence.repositories.session_repository import SessionRepository
    from meshpipeline.persistence.session import get_db

    async with get_db() as db:
        await SessionRepository().append_message(db, session.id, "assistant", HOLD_REPLY)
        await db.commit()
    logger.info("geometry naming queued from the first answer - session_id=%s", sid)
    return HOLD_REPLY


async def _interpretation_payload(session, owner_id: str, organization_id: str) -> dict | None:
    """The unit the user confirmed for this session's file, as the worker reads it, or None."""
    iid = getattr(session, "geometry_interpretation_id", None)
    if not iid:
        return None
    from dataclasses import asdict

    from meshpipeline.contracts.geometry_source import GeometryInterpretationRef
    from meshpipeline.persistence.repositories.geometry_interpretation_repository import (
        GeometryInterpretationRepository,
    )
    from meshpipeline.persistence.session import get_db

    async with get_db() as db:
        recorded = await GeometryInterpretationRepository().get_for_owner(
            db, iid, owner_id, organization_id=organization_id)
    return asdict(GeometryInterpretationRef.from_domain(recorded)) if recorded else None


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
    from meshpipeline.contracts.geometry_fields import form_spec
    payload["fields"] = form_spec()
    payload.setdefault("named", payload.get("status") == "ready")
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
