# Responsibility: Serve the geometry check to the console - what the scout proposed and the
# pictures it drew - and record what the user confirmed on it, in the session the intake reads.
# A check is served through the application's time box, so a step whose worker died reads as
# failed with a reason and a way on (a retry, or the chat), never as a spinner that never ends.
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
    #: THE SCALE THE NUMBERS WERE READ UNDER: metres per unit of the file, as the scout assumed
    #: it when the form was drawn (served on the proposal, sent back with it). A triangle file's
    #: numbers are read as millimetres until the unit is confirmed; when the confirmed unit
    #: differs, every length above is re-read in it before anything is declared.
    scale_to_m: float | None = Field(default=None, gt=0)
    #: THE UNIT THE USER SAYS THE FILE IS IN, from the unit box beside the sizes on the stage. One
    #: that differs from what the session holds (the file's own declaration, or an earlier
    #: answer) is recorded as the user's, exactly as a chat answer is.
    unit: Literal["mm", "cm", "m", "in"] | None = None

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
    session = await _owned_session(session_id, owner_id, organization_id)
    from meshpipeline.application import geometry_hold as gh
    from meshpipeline.application.geometry_check import (
        check_object_key,
        naming_pending,
        naming_requested,
        retry_step,
        time_boxed,
    )
    from meshpipeline.contracts.object_storage import get_object_store
    from meshpipeline.persistence.session import get_db

    stored = _read_json(check_object_key(str(session_id), "scout.json"))
    if stored is None:
        return {"status": STATUS_NONE}
    async with get_db() as db:
        interpretation = await gh.interpretation_payload(db, session, owner_id, organization_id)
    # the naming starts with the user's first answer: until then the stage must say it is
    # waiting for the chat, not that the model is working
    try:
        requested = naming_requested(str(session_id))
    except Exception as exc:  # noqa: BLE001 - a marker we cannot read is "not asked yet", never a broken check
        logger.warning("geometry check: naming marker unreadable (%s) - session_id=%s", exc, session_id)
        requested = None
    # THE TIME BOX. A scout still pending past the worker's hard limit, or a naming asked for that
    # long ago and never answered, is served as failed with a plain reason and the step to run
    # again - the read path is the one place that cannot be forgotten, and it stores nothing, so
    # a worker that was merely slow still lands its result.
    payload = time_boxed(stored, requested) or stored
    from meshpipeline.contracts.geometry_fields import form_spec
    payload["fields"] = form_spec()
    payload.setdefault("named", payload.get("status") == "ready")
    payload["naming_requested"] = naming_pending(requested)
    # a triangle file carries no unit: until the chat has settled it, the sizes on the stage are
    # in a unit nobody has named, and the stage says so instead of confirming them
    payload["unit_needed"] = unit_needed(session)
    if payload.get("status") == "failed":
        payload.setdefault("retry", retry_step(payload))
    if payload.get("status") in ("ready", "scouted") or payload.get("facts"):
        # the stage opens on a scouted check too, with the code's labels, and keeps them on a
        # naming that failed; the pictures are for the card, which only shows once the check
        # is ready
        if isinstance(payload.get("proposal"), dict):
            # the scale the proposal's numbers were read under, for the confirm to re-read them,
            # and the unit they are read in - on whose word - for the unit box beside the sizes
            if (payload.get("facts") or {}).get("scale_to_m"):
                payload["proposal"]["scale_to_m"] = float(payload["facts"]["scale_to_m"])
            payload["proposal"]["unit"], payload["proposal"]["unit_basis"] = unit_in_effect(interpretation)
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


def unit_needed(session) -> bool:
    """Whether the session's file still has no unit - none declared in it, none confirmed in the
    chat - so nothing measured on it can be confirmed yet."""
    return bool(getattr(session, "geometry_source_id", None)) and not getattr(session, "geometry_interpretation_id", None)


def unit_in_effect(interpretation: dict | None) -> tuple[str, str]:
    """The unit the file's numbers are read in, and on whose word: the interpretation the session
    holds (`file_declared` by the file, `user_confirmed` in the chat or on the stage), else the
    scout's assumption of millimetres - which the stage shows beside the part's size, so a 117 m
    blade read as 117 mm is corrected where the sizes are."""
    if interpretation:
        return str(interpretation["unit"]), str(interpretation["basis"])
    from meshpipeline.contracts.geometry_units import LengthUnit

    return LengthUnit.millimetre.value, "assumed"      # what both scouts assume when nothing says


def in_confirmed_unit(body: ConfirmIn, scale_to_metres: float | None) -> ConfirmIn:
    """The confirmation with every length re-read in the unit the chat confirmed. The numbers on
    the form are the file's own, read under the scale the scout assumed (`scale_to_m`, drawn
    with the form and sent back with it); when the unit confirmed since differs, a "466 mm"
    opening in a file drawn in metres is 466,000 mm, and the ports must be bound at that size.
    Untouched when the form carried no scale, or the two agree - the naming re-reads the facts
    the same way, and a form drawn from re-read facts carries the confirmed scale already."""
    if scale_to_metres is None or not body.scale_to_m:
        return body
    k = float(scale_to_metres) / float(body.scale_to_m)
    if abs(k - 1.0) <= 1e-9:
        return body
    from meshpipeline.application.geometry_check import rescaled_lengths

    plain = body.model_dump()
    plain.pop("scale_to_m")
    return ConfirmIn(**rescaled_lengths(plain, k), scale_to_m=float(scale_to_metres))


UNIT_FIRST = ("Say what unit the file is in first - in the box beside the sizes, or in the chat: "
              "millimetres, centimetres, metres or inches. The sizes cannot be confirmed in a unit "
              "nobody has named.")


STILL_RUNNING = ("This geometry check is still running. It can be run again once it reports failed - "
                 "which it does by itself when its time is up.")


class RetryIn(BaseModel):
    #: the step to run again, which must be the one the check reports for a retry; left out, that one
    step: Literal["scout", "naming"] | None = None


@router.post("/{session_id}/check/retry")
async def retry_check(session_id: uuid.UUID, body: RetryIn | None = None,
                      owner_id: str = Depends(owner_dep), organization_id: str = Depends(org_dep)) -> dict:
    """Run a step of the check again - the scout when the part was never measured, the naming
    when it was - and serve the check as it then stands. The way on from a drawing that took
    too long or a naming that never answered; the other way on is the chat, which asks what
    the picture would have settled."""
    if not gcfg.GEOMETRY_CHECK_ENABLED:
        raise HTTPException(404, "The geometry check is not enabled on this deployment")
    session = await _owned_session(session_id, owner_id, organization_id)
    if session.job_id is not None:
        raise HTTPException(409, "This session has already been dispatched")
    from meshpipeline.application import geometry_check as gc
    from meshpipeline.application import geometry_hold as gh
    from meshpipeline.persistence.session import get_db

    sid = str(session_id)
    stored = gc.read_check(sid)
    if stored is None:
        raise HTTPException(404, "No geometry check has been started for this session")
    try:
        requested = gc.naming_requested(sid)
    except Exception:  # noqa: BLE001 - as the read treats it: not asked yet
        requested = None
    # ONLY A CHECK REPORTED FAILED IS RUN AGAIN, and only its own step: a scout or a naming still
    # within its time is a worker still at work, and a second one would race it for the record;
    # a ready check has nothing to run again. The time box is what turns a lost worker into a
    # failed check, so a retry is never refused for good.
    reported = gc.time_boxed(stored, requested) or stored
    step = gc.retry_step(reported) if reported.get("status") == gc.STATUS_FAILED else None
    if step is None:
        raise HTTPException(409, STILL_RUNNING if reported.get("status") in (gc.STATUS_PENDING, gc.STATUS_SCOUTED)
                            else "This geometry check has nothing to run again")
    asked = body.step if body is not None else None
    if asked and asked != step:
        raise HTTPException(409, f"The step to run again on this check is the {step}, not the {asked}")
    async with get_db() as db:
        interpretation = await gh.interpretation_payload(db, session, owner_id, organization_id)
        source = await _source_payload(db, session, owner_id, organization_id)
    if step == gc.RETRY_SCOUT:
        if source is None:
            raise HTTPException(409, "The uploaded file could not be found for this session")
        # the words handed to an earlier naming go back with the new scout's facts
        gc.clear_naming_request(sid)
        gc.start_scout(sid, owner_id, source=source, interpretation=interpretation, where="retry")
    else:
        purpose = str((requested or {}).get("purpose_text") or gh.purpose_from(session.messages))
        if not gc.restart_naming(sid, owner_id, purpose_text=purpose, interpretation=interpretation):
            raise HTTPException(409, "The part has not been measured yet, so its openings cannot be "
                                     "named; run the scout again first")
    logger.info("geometry check %s retried - session_id=%s", step, session_id)
    return await get_check(session_id, owner_id, organization_id)


async def _source_payload(db, session, owner_id: str, organization_id: str) -> dict | None:
    """The uploaded file as the scout reads it, from the session's source row, or None."""
    source_id = getattr(session, "geometry_source_id", None)
    if not source_id:
        return None
    from meshpipeline.contracts.geometry_source import GeometrySourceRef
    from meshpipeline.persistence.repositories.geometry_source_repository import GeometrySourceRepository

    row = await GeometrySourceRepository().get_for_owner(db, source_id, owner_id, organization_id=organization_id)
    return GeometrySourceRef.from_row(row).to_payload() if row is not None else None


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
    # The kind is stated verbatim, in the intake's own enum, and it is the same word the session
    # stores. A solid body the fluid flows around stays "solid-body": the engine gate reads that as
    # a body surface for any fluid purpose (engines/purposes.kinds_admitted_as), and a later
    # structural request on the same solid still finds the kind gmsh needs.
    parts = [f"{CONFIRMED_MARK} the file is {kind}"
             + (f" ({body.part})" if body.part else "")
             + f", input_kind {body.input_kind}; the fluid flows {through}."]
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
    import tempfile
    from pathlib import Path

    from meshpipeline.application import geometry_hold as gh
    from meshpipeline.application.geometry_check import check_object_key

    # THE UNIT, SETTLED BEFORE ANYTHING IS DECLARED. A unit corrected in the box beside the sizes
    # counts as the user's, exactly as a chat answer does - a STEP file that says millimetres for
    # a blade drawn in metres is caught here. A form drawn while the numbers were read under
    # another scale is re-read in the unit that stands, so a user who proceeds before the naming
    # has re-read the facts never declares a size at the wrong scale. The unit is RECORDED below,
    # in the same transaction as the rest of the confirmation and after the stored copy, so a
    # confirmation that fails changes nothing at all.
    from meshpipeline.contracts.geometry_units import LengthUnit, scale_to_metres
    from meshpipeline.contracts.object_storage import get_object_store
    from meshpipeline.persistence.repositories.session_repository import SessionRepository
    from meshpipeline.persistence.session import get_db

    async with get_db() as db:
        interpretation = await gh.interpretation_payload(db, session, owner_id, organization_id)
    current = str(interpretation.get("unit")) if interpretation else None
    corrected = body.unit if body.unit and body.unit != current else None
    if corrected is None and interpretation is None:
        # the numbers on the form are the file's own, read as millimetres until somebody says
        # otherwise; a declaration in a unit nobody named would bind the ports at the wrong scale
        raise HTTPException(409, UNIT_FIRST)
    scale = scale_to_metres(LengthUnit(corrected)) if corrected else float(interpretation["scale_to_metres"])  # type: ignore[index]
    body = in_confirmed_unit(body, scale)
    message = confirmation_message(body)
    patches = patches_from(body)

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

    try:
        async with get_db() as db:
            # the same tenant-scoped read every request-facing module uses; never the internal one
            session = await SessionRepository().get_for_owner(db, session_id, owner_id,
                                                              organization_id=organization_id)
            if session is None:
                raise HTTPException(404, "Session not found")
            if corrected:
                await gh.record_unit(db, session, owner_id, organization_id, corrected)
                logger.info("geometry check: the file's unit set to %s on the stage - session_id=%s",
                            corrected, session_id)
            session.input_kind = body.input_kind
            session.intake_patches = patches
            session.messages = with_declaration(session.messages, message)
            await db.commit()
    except BaseException:
        # THE STORED COPY GOES BACK: the session never took the declaration (nor the unit), so a
        # copy that stayed would make the next read report a confirmation that did not land, and
        # the console would stop waiting for one. Best effort - the store may be what broke.
        try:
            get_object_store().delete_object(object_key=check_object_key(str(session_id), "confirmed.json"))
        except Exception:  # noqa: BLE001
            logger.warning("geometry check: could not take back the stored confirmation - session_id=%s", session_id)
        raise
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
