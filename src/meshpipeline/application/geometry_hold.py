# Responsibility: Decide, for one chat turn, whether the geometry check is waiting on this answer -
# and if so hand the user's words to the naming step and say what the conversation shows while
# the part is drawn.
# Boundaries: the rule (`should_hold`) is pure; the rest reads the check's stored markers and the
# session's confirmed unit and queues the naming through the contract seam. It takes no lock and
# writes no message: the intake message authority does both, under the session's row lock, so a
# message and a confirmation never interleave.
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

#: The first words of the holding line the chat gives while the part is being drawn. The intake
#: prompt names it so nothing in it is ever read as declared.
DRAWING_MARK = "GEOMETRY CHECK (drawing your part):"
HOLD_REPLY = (
    f"{DRAWING_MARK} thanks - I'm drawing your part now. It will appear beside this chat "
    "with a numbered sticker on every opening I found. Check the names, fix anything "
    "wrong, and press Proceed. Then I'll ask only what is still missing."
)
#: What the conversation says to a message sent while the part is still being drawn.
WAIT_REPLY = (
    f"{DRAWING_MARK} still drawing your part - it will appear beside this chat in a moment. "
    "Check the openings there and press Proceed; I'll carry on from that."
)
#: THE SAME TWO LINES FOR A WHOLE THERMAL MODEL (an ECXML upload, contracts/intake_formats
#: check_mode "thermal"). Nothing on its panel is an opening to check: the file names every part,
#: its power and the room air, so the line says what the file is and what to press. The model's
#: own headline (application/thermal_view.headline) leads once the check has read the file.
THERMAL_HOLD_REPLY = f"{DRAWING_MARK} {{headline}} Check the parts beside this chat and press Mesh this model."
#: ...before the check has read the file: there is no headline yet to give.
THERMAL_OPENING_REPLY = (
    f"{DRAWING_MARK} opening your thermal model now. It will appear beside this chat: "
    "check the parts there and press Mesh this model."
)
THERMAL_WAIT_REPLY = (
    f"{DRAWING_MARK} still opening your thermal model - it will appear beside this chat in a "
    "moment. Check the parts there and press Mesh this model; I'll carry on from that."
)
#: What the confirm sends as the user's turn so the intake picks up where the hold left it.
CONTINUE_TEXT = "I confirmed the geometry check. Go on."
#: How long after the naming was requested a message is still answered with the wait line. A
#: naming that never comes (a broken store, a dead worker) must not trap the conversation: past
#: this the intake answers as it always did.
WAIT_GRACE_S = 180.0


def hold_decision(stored: dict | None, requested: dict | None, confirmed: dict | None,
                  now: float | None = None) -> str | None:
    """What this chat turn does about the geometry check: "queue" - the first answer, hand the
    words to the naming and hold; "wait" - the naming is already running, hold again; None -
    let the intake answer (no check, a finished or failed one, a confirmed one, or a naming that
    has taken too long). Pure, so the rule is testable without a store."""
    import time as _time

    if stored is None or confirmed is not None:
        return None
    if stored.get("status") not in ("pending", "scouted"):
        return None
    if requested is None:
        return "queue"
    if requested.get("withdrawn"):
        # the naming gave up waiting for the scout: asked again once the part is measured, and
        # never held again for a scout that is still not there
        return "queue" if stored.get("status") == "scouted" else None
    asked_at = float(requested.get("requested_at") or 0.0)
    if (now if now is not None else _time.time()) - asked_at <= WAIT_GRACE_S:
        return "wait"
    return None


def should_hold(stored: dict | None, requested: dict | None, confirmed: dict | None) -> bool:
    """Whether this chat turn is the one that hands the user's words to the naming."""
    return hold_decision(stored, requested, confirmed) == "queue"


def hold_reply(thermal: bool, headline: str = "") -> str:
    """The holding line for this upload: the generic one (a picture with numbered openings to
    check), or a thermal model's - its own headline when the check has read the file, else the
    plain "opening your thermal model" line."""
    if not thermal:
        return HOLD_REPLY
    said = str(headline or "").strip()
    if not said:
        return THERMAL_OPENING_REPLY
    return THERMAL_HOLD_REPLY.format(headline=said if said.endswith(".") else said + ".")


def wait_reply(thermal: bool) -> str:
    """What a message sent while the check is still drawing is answered with."""
    return THERMAL_WAIT_REPLY if thermal else WAIT_REPLY


def stored_thermal(session_id: str) -> tuple[bool, str]:
    """Whether the stored check reads the upload as a whole thermal model, and its one-line
    headline as the check read it from the file ("Set-top box, vented: a Flotherm thermal model
    with 8 parts, 8.7 W in total and 35 °C room air."). (False, "") while the check has not read
    the file yet - or cannot be read."""
    from meshpipeline.contracts.intake_formats import THERMAL_CHECK

    try:
        from meshpipeline.application.geometry_check import read_check

        stored = read_check(session_id) or {}
    except Exception as exc:  # noqa: BLE001 - a line without the headline is still a true line
        logger.info("geometry hold: the stored check could not be read (%s)", type(exc).__name__)
        return False, ""
    view = stored.get("thermal") if isinstance(stored.get("thermal"), dict) else {}
    return stored.get("check_mode") == THERMAL_CHECK, str((view or {}).get("headline") or "")


def purpose_from(messages: list[dict] | None) -> str:
    """Everything the user has said so far, for the model: the answer to the opening question,
    and the unit answer when there was one."""
    return "\n".join(str(m.get("content", "")).strip() for m in (messages or [])
                     if m.get("role") == "user" and str(m.get("content", "")).strip())[:2000]


def hold_applies(session_id: str) -> str | None:
    """The rule, read against the store: "queue", "wait" or None; None when the check is
    disabled or no check exists."""
    import meshpipeline.settings.geometry_check as gcfg

    if not gcfg.GEOMETRY_CHECK_ENABLED:
        return None
    from meshpipeline.application.geometry_check import (
        check_object_key,
        naming_requested,
        read_check,
        time_boxed,
    )
    from meshpipeline.contracts.object_storage import ObjectNotFound, get_object_store

    try:
        get_object_store().get_bytes(object_key=check_object_key(session_id, "confirmed.json"))
        confirmed: dict | None = {}
    except ObjectNotFound:
        confirmed = None
    # read through the same time box the API reads through: a scout whose worker died, or a
    # naming that never answered, is a failed check, and a failed check never holds the chat
    requested = naming_requested(session_id)
    return hold_decision(time_boxed(read_check(session_id), requested), requested, confirmed)


def measured_size_mm(session_id: str) -> list[float] | None:
    """The part's size as the scout measured it - the file's own numbers read as millimetres -
    for the unit question to show in each candidate unit. None when the check is off, has not
    measured the part yet, read the unit from the file, or cannot be read: the question is
    then asked without the hint, and proposes nothing."""
    import meshpipeline.settings.geometry_check as gcfg

    if not gcfg.GEOMETRY_CHECK_ENABLED:
        return None
    from meshpipeline.application.geometry_check import read_check

    try:
        facts = (read_check(session_id) or {}).get("facts") or {}
        size = facts.get("size_mm") if facts.get("unit_assumed", True) else None
        if not isinstance(size, list) or len(size) != 3:
            return None
        return [float(v) for v in size]
    except Exception as exc:  # noqa: BLE001 - a hint we cannot read is a question without one
        logger.info("geometry check: measured size unavailable for the unit question (%s)", exc)
        return None


def measured_unit_inference(session_id: str, words: str = "") -> dict | None:
    """The unit the check reads a unit-less file as - from what the part is (its shape and flow,
    the naming's name and estimate, the user's words) - with how sure that is and the other
    reading (application/geometry_check.unit_inference). The confirmed form wins over the scout's
    guess for the flow, the ground and the part's name. None when there is nothing measured to
    read, or the file stated its unit."""
    import meshpipeline.settings.geometry_check as gcfg

    if not gcfg.GEOMETRY_CHECK_ENABLED:
        return None
    import json

    from meshpipeline.application.geometry_check import check_object_key, read_check, unit_inference
    from meshpipeline.contracts.object_storage import ObjectNotFound, get_object_store

    try:
        stored = read_check(session_id) or {}
        facts = stored.get("facts") or {}
        if not facts.get("unit_assumed", True):
            return None
        proposal = dict(stored.get("proposal") or {})
        if not proposal.get("size_mm"):
            proposal["size_mm"] = facts.get("size_mm")
        try:
            confirmed = json.loads(get_object_store().get_bytes(
                object_key=check_object_key(session_id, "confirmed.json")))
        except ObjectNotFound:
            confirmed = None
        if isinstance(confirmed, dict):
            for key in ("flow", "grounded", "up_axis", "part"):
                if confirmed.get(key) not in (None, ""):
                    proposal[key] = confirmed[key]
        return unit_inference(proposal, facts, words)
    except Exception as exc:  # noqa: BLE001 - a reading we cannot make is a question without one
        logger.info("geometry check: unit reading unavailable for the unit question (%s)", exc)
        return None


async def interpretation_payload(db, session, owner_id: str, organization_id: str) -> dict | None:
    """The unit the user confirmed for this session's file, as the worker reads it, or None."""
    iid = getattr(session, "geometry_interpretation_id", None)
    if not iid:
        return None
    from dataclasses import asdict

    from meshpipeline.contracts.geometry_source import GeometryInterpretationRef
    from meshpipeline.persistence.repositories.geometry_interpretation_repository import (
        GeometryInterpretationRepository,
    )

    recorded = await GeometryInterpretationRepository().get_for_owner(
        db, iid, owner_id, organization_id=organization_id)
    return asdict(GeometryInterpretationRef.from_domain(recorded)) if recorded else None


async def unit_fixing_format(db, session, owner_id: str, organization_id: str):
    """The intake format of the session's upload when its converter fixes the unit of the
    geometry (contracts/intake_formats `fixed_unit`: an ECXML model is written in millimetres),
    else None. A source that cannot be read is None - the unit is then handled as any other."""
    from meshpipeline.contracts.intake_formats import unit_fixing_format as _fixing

    suffix = await _upload_suffix(db, session, owner_id, organization_id)
    return _fixing(suffix) if suffix is not None else None


async def upload_check_mode(db, session, owner_id: str, organization_id: str) -> str:
    """How the geometry check reads the session's upload (contracts/intake_formats check_mode):
    "thermal" for a whole thermal model, "" for the generic check - and "" when there is no
    upload or it cannot be read."""
    from meshpipeline.contracts.intake_formats import format_for_suffix

    suffix = await _upload_suffix(db, session, owner_id, organization_id)
    fmt = format_for_suffix(suffix) if suffix else None
    return str(getattr(fmt, "check_mode", "") or "")


async def _upload_suffix(db, session, owner_id: str, organization_id: str) -> str | None:
    """The suffix the session's upload was recorded under; None when there is none or it cannot
    be read."""
    sid = getattr(session, "geometry_source_id", None)
    if not sid or db is None:
        return None
    from meshpipeline.persistence.repositories.geometry_source_repository import GeometrySourceRepository

    try:
        row = await GeometrySourceRepository().get_for_owner(db, sid, owner_id,
                                                             organization_id=organization_id)
    except Exception as exc:  # noqa: BLE001 - a lookup that fails locks nothing
        logger.warning("geometry unit: the upload's format could not be read (%s) - session_id=%s",
                       type(exc).__name__, getattr(session, "id", None))
        return None
    return str(getattr(row, "suffix_hint", "") or "") if row is not None else None


async def ensure_fixed_unit(db, session, owner_id: str, organization_id: str) -> dict | None:
    """THE UNIT A CONVERTER FIXED, bound to the session when nothing is: an ECXML upload's
    geometry is millimetres by construction, so it is recorded as the file's (as the upload
    records any declared unit) rather than asked. Returns the interpretation now in force when
    the upload's unit is fixed, else None. The upload records it already; this covers an upload
    whose unit could not be read then - and a session that holds ANOTHER unit (a "metres" taken
    before the unit was locked): the fixed one is bound back over it, and a run proposed under
    the wrong one is withdrawn by the intake authority, in the caller's transaction."""
    import uuid
    from dataclasses import asdict

    fmt = await unit_fixing_format(db, session, owner_id, organization_id)
    if fmt is None:
        return None
    current = await interpretation_payload(db, session, owner_id, organization_id)
    if current is not None and str(current.get("unit")) == fmt.fixed_unit:
        return current
    from meshpipeline.contracts.geometry_source import GeometryInterpretationRef
    from meshpipeline.contracts.geometry_units import LengthUnit, ResolutionBasis
    from meshpipeline.persistence.repositories.geometry_interpretation_repository import (
        GeometryInterpretationRepository,
    )
    from meshpipeline.persistence.repositories.session_repository import SessionRepository

    recorded = await GeometryInterpretationRepository().record(
        db, owner_id=owner_id, geometry_source_id=session.geometry_source_id,
        unit=LengthUnit(fmt.fixed_unit), basis=ResolutionBasis.file_declared,
        evidence=f"fixed by the conversion: {fmt.fixed_unit_reason}"[:256],
        organization_id=organization_id)
    await SessionRepository().bind_geometry_interpretation(db, session.id, uuid.UUID(recorded.interpretation_id))
    logger.info("geometry unit: %s fixed by the %s conversion, recorded%s - session_id=%s",
                fmt.fixed_unit, fmt.key, f" over {current.get('unit')}" if current else "", session.id)
    if current is not None:
        from meshpipeline.agents.intake.message import withdraw_proposal_for_unit_change

        if await withdraw_proposal_for_unit_change(db, session):
            logger.info("geometry unit: a run proposed under %s was withdrawn - session_id=%s",
                        current.get("unit"), session.id)
    return asdict(GeometryInterpretationRef.from_domain(recorded))


async def record_unit(db, session, owner_id: str, organization_id: str, unit: str) -> dict:
    """The unit corrected on the stage, recorded exactly as the chat records an answer: a
    user-confirmed interpretation bound to the session. A STEP file that says millimetres for a
    part drawn in metres is caught here, where the sizes are, not after the run. The intake gate
    is the intake authority's: its open unit question is moot once the interpretation is bound,
    which is the first thing every unit turn checks, and a live run proposal is withdrawn by it
    here, in this transaction. Returns the interpretation as the worker reads it."""
    import uuid
    from dataclasses import asdict

    from meshpipeline.contracts.geometry_source import GeometryInterpretationRef
    from meshpipeline.contracts.geometry_units import LengthUnit, ResolutionBasis
    from meshpipeline.persistence.repositories.geometry_interpretation_repository import (
        GeometryInterpretationRepository,
    )
    from meshpipeline.persistence.repositories.session_repository import SessionRepository

    recorded = await GeometryInterpretationRepository().record(
        db, owner_id=owner_id, geometry_source_id=session.geometry_source_id, unit=LengthUnit(unit),
        basis=ResolutionBasis.user_confirmed, evidence="confirmed on the geometry stage",
        organization_id=organization_id)
    await SessionRepository().bind_geometry_interpretation(db, session.id, uuid.UUID(recorded.interpretation_id))
    # A RUN PROPOSED WITH THE OLD SIZES is withdrawn in this same transaction, by the intake
    # authority: left to the chat turn after the confirmation, a turn that failed would leave it
    # live for a later "yes" to dispatch at the old scale.
    from meshpipeline.agents.intake.message import withdraw_proposal_for_unit_change

    if await withdraw_proposal_for_unit_change(db, session):
        logger.info("geometry check: a proposed run was withdrawn by the unit change - session_id=%s", session.id)
    return asdict(GeometryInterpretationRef.from_domain(recorded))


def queue_naming(session_id: str, owner_id: str, purpose_text: str, interpretation: dict | None) -> bool:
    """Hand the words and the unit to the naming step and leave the marker that says so. False
    when nothing is configured to run it, so the caller lets the intake carry on."""
    from meshpipeline.application.geometry_check import mark_naming_requested
    from meshpipeline.contracts.geometry_check import enqueue_naming

    if not enqueue_naming(session_id=session_id, owner_id=owner_id, purpose_text=purpose_text,
                          interpretation=interpretation):
        return False
    mark_naming_requested(session_id, purpose_text)
    logger.info("geometry naming queued from the first answer - session_id=%s", session_id)
    return True


def withdraw_naming(session_id: str) -> None:
    """The compensation for a hold whose turn could not be stored: the marker goes, so the next
    turn asks again. The naming already queued still runs; a second one later merely writes the
    same `ready` again."""
    from meshpipeline.application.geometry_check import clear_naming_request

    clear_naming_request(session_id)
    logger.warning("geometry naming request withdrawn after a failed turn - session_id=%s", session_id)


__all__ = ["CONTINUE_TEXT", "DRAWING_MARK", "HOLD_REPLY", "THERMAL_HOLD_REPLY", "THERMAL_OPENING_REPLY",
           "THERMAL_WAIT_REPLY", "WAIT_GRACE_S", "WAIT_REPLY", "ensure_fixed_unit", "hold_applies",
           "hold_decision", "hold_reply", "interpretation_payload", "measured_size_mm", "purpose_from",
           "queue_naming", "record_unit", "should_hold", "stored_thermal", "unit_fixing_format",
           "upload_check_mode", "wait_reply", "withdraw_naming"]
