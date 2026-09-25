# Responsibility: Ask, once, what physical unit an ambiguous geometry is in - and remember the answer.
# Boundaries: asked once per session whenever no unit has been CONFIRMED, whatever the format states.
from __future__ import annotations

from meshpipeline.contracts.geometry_units import (
    LengthUnit,
    UnitResolutionError,
    parse_unit,
)

#: Asked once, when a session's geometry has no confirmed scale.
#:
#: IT USED TO SAY "because this file does not state it", AND THAT IS FALSE ON EVERY STEP FILE THAT
#: DECLARES SI METRE. `needs_confirmation` below fires on the absence of a CONFIRMED interpretation,
#: not on the absence of a declaration, and `cad/unit_evidence.py` deliberately declines to believe
#: several declarations it can read perfectly well: a STEP whose length unit is metre is unresolved
#: because OCC's `FileUnits` answers "metre" for a MALFORMED unit entity too and the two are
#: indistinguishable (`_step_unit_entity_is_well_formed`, where a wrong answer transfers the shape a
#: thousand times too large), a file declaring two different length units is unresolved because
#: mixed representations must not be collapsed into one global reading, and an IGES carrying a model
#: scale is unresolved because the scale and the unit compose in a way nothing here may guess. Each
#: of those files DOES state a unit, in its own text, and each of them arrives at this question -
#: `api/v1/upload.py:208-217` records an interpretation only when the evidence RESOLVED.
#:
#: WHAT A FILE DECLARES AND WHAT ITS COORDINATES ACTUALLY ARE ARE TWO DIFFERENT FACTS, which is why
#: the fix is not "skip the question when the file declares a unit". The declaration is precisely
#: the thing that could not be believed, and skipping on it would mesh the malformed-SI case at
#: 1000x in silence - a mesh that looks plausible and is wrong, which is what REFUSAL below exists
#: to refuse. So the question stays and the claim goes: it now states the absence it really has -
#: nothing has confirmed the scale - plus the disjunction that covers every file that can get here,
#: asserting neither half of it about the file in front of the customer.
QUESTION = (
    "Before meshing I need to know what one unit in your geometry means, because nothing has "
    "confirmed the scale of this file: it either records no unit, or records one I could not "
    "verify. Are the coordinates in millimetres, centimetres, metres, or inches?"
)

#: Asked again when the answer was not one of the four. Never guesses, never proceeds.
REFUSAL = (
    "I could not read that as a unit, and I will not guess - meshing at the wrong scale produces "
    "a result that looks plausible and is wrong. Please answer with one of: millimetres, "
    "centimetres, metres, or inches."
)


def needs_confirmation(session) -> bool:
    return bool(getattr(session, "geometry_source_id", None)) and not getattr(
        session, "geometry_interpretation_id", None)


def already_asked(intake_gate) -> bool:
    return bool((intake_gate or {}).get("unit_question"))


def classify(message: str) -> LengthUnit | None:
    try:
        return parse_unit(message)
    except UnitResolutionError:
        return None


def asked(intake_gate) -> dict:
    gate = dict(intake_gate or {})
    gate["unit_question"] = {"asked": True}
    return gate


def answered(intake_gate) -> dict:
    gate = dict(intake_gate or {})
    gate.pop("unit_question", None)
    return gate


async def record(db, *, owner_id: str, geometry_source_id, unit: LengthUnit):
    from meshpipeline.contracts.geometry_units import ResolutionBasis
    from meshpipeline.persistence.repositories.geometry_interpretation_repository import (
        GeometryInterpretationRepository,
    )
    return await GeometryInterpretationRepository().record(
        db, owner_id=owner_id, geometry_source_id=geometry_source_id, unit=unit,
        basis=ResolutionBasis.user_confirmed, evidence="confirmed in conversation")
