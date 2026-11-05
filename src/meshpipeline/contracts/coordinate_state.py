# Responsibility: Name the boundary a geometry conversion applies at, so a transform is never applied twice.
# Owns: the origin vocabulary and the prepared-coordinate value that records which conversion has already happened.
# Boundaries: it records state; it performs no conversion and reads no file.
# Collaborates with: contracts/geometry_units.py and the engine staging seams.
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from meshpipeline.contracts.geometry_units import (
    GeometryInterpretation,
    LengthUnit,
    scale_to_metres,
)

#: OpenCASCADE transfers into its system unit, so a shape it returns is millimetres whatever the
#: file declared. Stated here rather than imported from the parser layer: a contract describes the
#: boundary, it does not depend on who reads the file.
OCC_OUTPUT_UNIT = LengthUnit.millimetre


class CoordinateStateError(ValueError):
    pass


class CoordinateOrigin(str, Enum):

    #: straight from the uploaded file - STL vertices, VTK points
    source_file = "source_file"
    #: produced by an OpenCASCADE transfer, already normalised to OCC's system unit
    occ_transfer = "occ_transfer"


@dataclass(frozen=True)
class PreparedCoordinates:

    origin: CoordinateOrigin
    current_unit: LengthUnit
    interpretation: GeometryInterpretation
    #: For parser output only: the unit the PARSER already applied. OpenCASCADE converts a STEP
    #: file's declared unit into its own system unit during transfer, so a user override has to be
    #: expressed relative to that - never applied on top of it.
    parser_applied_unit: LengthUnit | None = None

    def __post_init__(self) -> None:
        if (self.origin is CoordinateOrigin.occ_transfer
                and self.current_unit is not OCC_OUTPUT_UNIT):
            raise CoordinateStateError(
                f"post-transfer coordinates are {OCC_OUTPUT_UNIT.value}, not "
                f"{self.current_unit.value}: this state was not produced by a real OpenCASCADE "
                "transfer, so the conversion it describes cannot be trusted")

    @property
    def to_metres(self) -> float:
        if self.parser_applied_unit is None:
            return scale_to_metres(self.current_unit)
        return (self.interpretation.scale_to_metres
                / scale_to_metres(self.parser_applied_unit)
                * scale_to_metres(self.current_unit))

    @property
    def already_normalised(self) -> bool:
        return self.origin is CoordinateOrigin.occ_transfer


def from_source_file(interpretation: GeometryInterpretation) -> PreparedCoordinates:
    return PreparedCoordinates(CoordinateOrigin.source_file, interpretation.unit, interpretation)


def from_occ_transfer(interpretation: GeometryInterpretation,
                      parser_applied: LengthUnit | None = None) -> PreparedCoordinates:
    return PreparedCoordinates(CoordinateOrigin.occ_transfer, OCC_OUTPUT_UNIT, interpretation,
                               parser_applied_unit=parser_applied or interpretation.unit)
