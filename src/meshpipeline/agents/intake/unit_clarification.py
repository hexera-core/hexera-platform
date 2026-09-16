# Responsibility: Ask, once, what physical unit an ambiguous geometry is in - and remember the answer.
# Boundaries: STL and VTK carry no unit, so the question is asked once per session rather than defaulted.
from __future__ import annotations

from meshpipeline.contracts.geometry_units import (
    LengthUnit,
    UnitResolutionError,
    parse_unit,
)

#: Asked once, when a session's geometry has no confirmed scale.
QUESTION = (
    "Before meshing I need to know what one unit in your geometry means, because this file does "
    "not state it. Are the coordinates in millimetres, centimetres, metres, or inches?"
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


async def record(db, *, owner_id: str, geometry_source_id, unit: LengthUnit,
                 organization_id: str = ""):
    from meshpipeline.contracts.geometry_units import ResolutionBasis
    from meshpipeline.persistence.repositories.geometry_interpretation_repository import (
        GeometryInterpretationRepository,
    )
    return await GeometryInterpretationRepository().record(
        db, owner_id=owner_id, geometry_source_id=geometry_source_id, unit=unit,
        basis=ResolutionBasis.user_confirmed, evidence="confirmed in conversation",
        organization_id=organization_id)
