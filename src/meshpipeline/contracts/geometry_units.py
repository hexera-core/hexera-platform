# Responsibility: State what one coordinate unit of an uploaded geometry means in metres.
# Owns: the length-unit vocabulary, how a unit was resolved, and the immutable interpretation record.
# Boundaries: a unit is recorded here once confirmed.
# Collaborates with: cad/unit_evidence.py, which reads what a file declares, and the persistence interpretation rows.
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class LengthUnit(str, Enum):

    metre = "m"
    millimetre = "mm"
    centimetre = "cm"
    inch = "in"


#: Exact scale from one source coordinate unit to metres. `inch` is exact by definition
#: (1 in == 25.4 mm exactly), so it is written as an exact decimal rather than a rounded float.
SCALE_TO_METRES: dict[LengthUnit, float] = {
    LengthUnit.metre: 1.0,
    LengthUnit.millimetre: 1e-3,
    LengthUnit.centimetre: 1e-2,
    LengthUnit.inch: 0.0254,
}


class ResolutionBasis(str, Enum):

    file_declared = "file_declared"      # read from the file AND verified by the parser
    user_confirmed = "user_confirmed"    # the user was asked and answered


class UnitResolutionError(ValueError):
    pass


def parse_unit(answer: str) -> LengthUnit:
    if not isinstance(answer, str):
        raise UnitResolutionError("no unit was given")
    token = answer.strip().lower().rstrip(".")
    if not token:
        raise UnitResolutionError("no unit was given")
    aliases = {
        "m": LengthUnit.metre, "metre": LengthUnit.metre, "metres": LengthUnit.metre,
        "meter": LengthUnit.metre, "meters": LengthUnit.metre,
        "mm": LengthUnit.millimetre, "millimetre": LengthUnit.millimetre,
        "millimetres": LengthUnit.millimetre, "millimeter": LengthUnit.millimetre,
        "millimeters": LengthUnit.millimetre,
        "cm": LengthUnit.centimetre, "centimetre": LengthUnit.centimetre,
        "centimetres": LengthUnit.centimetre, "centimeter": LengthUnit.centimetre,
        "centimeters": LengthUnit.centimetre,
        "in": LengthUnit.inch, "inch": LengthUnit.inch, "inches": LengthUnit.inch,
    }
    if token not in aliases:
        raise UnitResolutionError(f"{answer!r} is not one of: metres, millimetres, "
                                  "centimetres, inches")
    return aliases[token]


def scale_to_metres(unit: LengthUnit) -> float:
    return SCALE_TO_METRES[unit]


@dataclass(frozen=True)
class GeometryInterpretation:

    interpretation_id: str
    owner_id: str
    geometry_source_id: str
    unit: LengthUnit
    scale_to_metres: float
    basis: ResolutionBasis
    evidence: str = ""          # short, typed, non-private: why this unit is believed

    def identity(self) -> dict:
        return {
            "geometry_source_id": self.geometry_source_id,
            "unit": self.unit.value,
            "scale_to_metres": self.scale_to_metres,
            "basis": self.basis.value,
        }
