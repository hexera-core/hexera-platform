# Responsibility: Change the unit a session's file is read in after it was settled - "the file is
# in metres", said in the chat once the sizes were confirmed as millimetres - and re-read every
# length that was derived under the old unit, so one unit holds everywhere after.
# Owns: the change itself (the user's unit recorded and bound), the re-read of what the user
# confirmed on the geometry check (the declaration the intake reads, the session's declared
# patches), the withdrawal of a proposed run whose numbers were the old unit's, and the plain
# sentence that tells the user what changed.
# Boundaries: it writes through the session repository it is handed, inside the caller's
# transaction, and reads the stored confirmation; the stored confirmation itself is never
# rewritten - it keeps the scale it was confirmed under, and every re-read starts from it, so two
# changes in a row never compound. It never writes the intake gate: it hands the gate it wants
# back, and the intake authority writes it. It asks nothing: the caller decided the user named a
# unit.
# Collaborates with: agents/intake/message.py (the chat), api/v1/geometry.py (the stage),
# application/geometry_confirmation.py (the words and patches), application/geometry_hold.py.
from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass

from meshpipeline.contracts.geometry_units import LengthUnit, scale_to_metres
from meshpipeline.contracts.unit_plausibility import UNIT_WORDS, length_in, length_words

logger = logging.getLogger(__name__)

#: Why a proposed run was withdrawn, as the approval snapshot records it.
WITHDRAWN_REASON = "the file's unit changed after the run was proposed"


@dataclass(frozen=True)
class UnitChange:
    """What a change of unit did, for the reply and the log."""

    old_unit: str | None
    new_unit: str
    #: the part's longest side under the old unit and the new one, in metres, when it is known
    longest_old_m: float | None = None
    longest_new_m: float | None = None
    #: the reference length along the flow, re-read, in metres (a body in a flow only)
    reference_length_m: float | None = None
    #: the geometry check's confirmation was re-read in the new unit
    reread: bool = False
    #: a run proposed under the old unit was withdrawn
    approval_withdrawn: bool = False
    #: the intake gate as it must now be written - the proposal withdrawn, an open unit question
    #: answered - or None when it stands as it was. Written by the intake authority, which alone
    #: writes the gate.
    gate: dict | None = None

    def reply(self) -> str:
        """What the chat says: the unit, the part's length in it, what was re-read, what stands."""
        new = LengthUnit(self.new_unit)
        said = f"Noted: the file is in {UNIT_WORDS[new]}"
        if self.longest_new_m:
            said += f", so the part is {length_in(self.longest_new_m, new)} long"
            if self.longest_old_m and self.old_unit:
                said += f", not {length_in(self.longest_old_m, LengthUnit(self.old_unit))}"
        said += "."
        if self.reread:
            said += (f" I've re-read every size you confirmed on the picture in {UNIT_WORDS[new]}"
                     + (f" - the reference length is now {length_words(self.reference_length_m)}"
                        if self.reference_length_m else "")
                     + ". Everything else you confirmed stands.")
        if self.approval_withdrawn:
            said += (" The run I summarised used the old sizes, so I've withdrawn it; say go on and I'll "
                     "set it up again with these.")
        else:
            said += " Say go on when you're ready."
        return said


def withdraw_live_approval(gate: dict | None) -> tuple[dict, bool]:
    """The intake gate with a live run proposal withdrawn: its numbers were the old unit's, so it
    must never dispatch. Returns the gate and whether anything was withdrawn."""
    from meshpipeline.agents.intake import approval as ap

    out = dict(gate or {})
    if ap.is_live(out.get("approval")):
        out["approval"] = ap.invalidate(out.get("approval"), WITHDRAWN_REASON)
        return out, True
    return out, False


class UnitChangeError(RuntimeError):
    """The confirmed sizes could not be read, so the unit is not changed at all: a new unit bound
    over old sizes is exactly the mismatch this module exists to prevent."""


def _stored(session_id: str, name: str, *, strict: bool = False) -> dict | None:
    """A stored object of the check, or None when there is none. `strict`: a store that cannot
    be read raises instead of reading as "nothing stored" - for the confirmation, whose sizes
    must follow the unit or the unit must not change."""
    from meshpipeline.application.geometry_check import check_object_key
    from meshpipeline.contracts.object_storage import ObjectNotFound, get_object_store

    try:
        return json.loads(get_object_store().get_bytes(object_key=check_object_key(session_id, name)))
    except ObjectNotFound:
        return None
    except Exception as exc:  # noqa: BLE001 - said, then raised or read as nothing
        logger.warning("unit change: could not read %s for %s (%s)", name, session_id, exc)
        if strict:
            raise UnitChangeError(f"the confirmed geometry check could not be read ({type(exc).__name__})") from exc
        return None


def _longest_file_units(session_id: str) -> float | None:
    """The part's longest side in the file's own numbers, from the scout's measurement."""
    facts = (_stored(session_id, "scout.json") or {}).get("facts") or {}
    size, scale = facts.get("size_mm"), facts.get("scale_to_m")
    try:
        if size and len(size) == 3 and scale:
            return max(float(v) for v in size) * 0.001 / float(scale)
    except (TypeError, ValueError):
        return None
    return None


async def change_unit(db, session, *, owner_id: str, organization_id: str, unit: LengthUnit,
                      session_repo, where: str = "chat") -> UnitChange | None:
    """Record `unit` as the user's for this session's file and re-read everything derived under
    the one it replaces. None when no length changes: the session has no file, or already holds
    that unit (recorded as the user's word when it was only the file's declaration)."""
    from meshpipeline.application import geometry_hold as gh
    from meshpipeline.application.geometry_confirmation import replace_declaration, reread_record
    from meshpipeline.contracts.geometry_units import ResolutionBasis
    from meshpipeline.persistence.repositories.geometry_interpretation_repository import (
        GeometryInterpretationRepository,
    )

    if not getattr(session, "geometry_source_id", None):
        return None
    current = await gh.interpretation_payload(db, session, owner_id, organization_id)
    old_unit = str(current["unit"]) if current else None
    if old_unit == unit.value and current and str(current.get("basis")) == ResolutionBasis.user_confirmed.value:
        return None
    sid = str(session.id)
    new_scale = scale_to_metres(unit)
    old_scale = float(current["scale_to_metres"]) if current else None
    # THE CONFIRMED SIZES ARE READ AND RE-READ BEFORE ANYTHING IS WRITTEN: a store that cannot be
    # read, or a confirmation whose scale nobody knows, stops the change here (UnitChangeError),
    # so the unit is never bound over sizes left in the old one. A confirmation stored without its
    # scale was made in the unit in force then - the one this change replaces.
    again = None
    if old_unit != unit.value:
        confirmed = _stored(sid, "confirmed.json", strict=True)
        if confirmed is not None:
            again = reread_record(confirmed, scale_to_metres=new_scale, unit=unit.value, confirmed_scale=old_scale)
            if again is None:
                raise UnitChangeError("the confirmed geometry check does not say what scale its sizes were read under")
    recorded = await GeometryInterpretationRepository().record(
        db, owner_id=owner_id, geometry_source_id=session.geometry_source_id, unit=unit,
        basis=ResolutionBasis.user_confirmed,
        evidence=f"{'confirmed' if old_unit == unit.value else 'changed'} by the user in the {where}",
        organization_id=organization_id)
    await session_repo.bind_geometry_interpretation(db, session.id, uuid.UUID(recorded.interpretation_id))
    if old_unit == unit.value:
        # the file's own declaration, now the user's word too: nothing to re-read, and the stage
        # stops offering the other reading - but the conversation goes on as an ordinary turn
        logger.info("unit change: %s confirmed by the user in the %s - session=%s", unit.value, where, session.id)
        return None

    longest_file = _longest_file_units(sid)
    reread = False
    reference_m = None
    if again is not None:
        await session_repo.set_intake_patches(db, session.id, again["patches"])
        await session_repo.set_messages(db, session.id,
                                        replace_declaration(list(session.messages or []), again["message"]))
        reread = True
        if again.get("reference_length_mm"):
            reference_m = float(again["reference_length_mm"]) / 1000.0

    gate, withdrawn = withdraw_live_approval(getattr(session, "intake_gate", None))
    rewrite = withdrawn or bool(gate.get("unit_question"))
    gate.pop("unit_question", None)                 # an open unit question is answered by this

    change = UnitChange(
        old_unit=old_unit, new_unit=unit.value,
        longest_old_m=longest_file * old_scale if longest_file and old_scale else None,
        longest_new_m=longest_file * new_scale if longest_file else None,
        reference_length_m=reference_m, reread=reread, approval_withdrawn=withdrawn,
        gate=gate if rewrite else None)
    logger.info("unit change: %s -> %s in the %s - session=%s reread=%s withdrawn=%s",
                old_unit, unit.value, where, sid, reread, withdrawn)
    return change


__all__ = ["WITHDRAWN_REASON", "UnitChange", "UnitChangeError", "change_unit", "withdraw_live_approval"]
