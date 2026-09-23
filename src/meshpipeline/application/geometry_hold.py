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
#: What the confirm sends as the user's turn so the intake picks up where the hold left it.
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


def hold_applies(session_id: str) -> bool:
    """The rule, read against the store: off when the check is disabled or no check exists."""
    import meshpipeline.settings.geometry_check as gcfg

    if not gcfg.GEOMETRY_CHECK_ENABLED:
        return False
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
    return should_hold(read_check(session_id), naming_requested(session_id), confirmed)


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


__all__ = ["CONTINUE_TEXT", "DRAWING_MARK", "HOLD_REPLY", "hold_applies", "interpretation_payload",
           "purpose_from", "queue_naming", "should_hold"]
