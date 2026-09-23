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
    f"{DRAWING_MARK} thanks - I'm drawing your part now. It will appear beside this chat in a few "
    "seconds with a numbered sticker on every opening I found. Check the names, fix anything "
    "wrong, and press Proceed. Then I'll ask only what is still missing."
)
#: What the conversation says to a message sent while the part is still being drawn.
WAIT_REPLY = (
    f"{DRAWING_MARK} still drawing your part - it will appear beside this chat in a moment. "
    "Check the openings there and press Proceed; I'll carry on from that."
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
    asked_at = float(requested.get("requested_at") or 0.0)
    if (now if now is not None else _time.time()) - asked_at <= WAIT_GRACE_S:
        return "wait"
    return None


def should_hold(stored: dict | None, requested: dict | None, confirmed: dict | None) -> bool:
    """Whether this chat turn is the one that hands the user's words to the naming."""
    return hold_decision(stored, requested, confirmed) == "queue"


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
    )
    from meshpipeline.contracts.object_storage import ObjectNotFound, get_object_store

    try:
        get_object_store().get_bytes(object_key=check_object_key(session_id, "confirmed.json"))
        confirmed: dict | None = {}
    except ObjectNotFound:
        confirmed = None
    return hold_decision(read_check(session_id), naming_requested(session_id), confirmed)


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


__all__ = ["CONTINUE_TEXT", "DRAWING_MARK", "HOLD_REPLY", "WAIT_GRACE_S", "WAIT_REPLY", "hold_applies",
           "hold_decision", "interpretation_payload", "purpose_from", "queue_naming", "should_hold",
           "withdraw_naming"]
