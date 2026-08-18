# Responsibility: Declare the coordinate state of the native tier's own fixtures.
# Boundaries: the native tier states its geometry's unit rather than inferring it, exactly as production does.
from __future__ import annotations

from meshpipeline.contracts.coordinate_state import from_occ_transfer, from_source_file
from meshpipeline.contracts.geometry_units import (
    GeometryInterpretation,
    LengthUnit,
    ResolutionBasis,
    scale_to_metres,
)

#: The native fixtures are authored in metres.
FIXTURE_UNIT = LengthUnit.metre


def _interpretation(unit: LengthUnit = FIXTURE_UNIT) -> GeometryInterpretation:
    return GeometryInterpretation(
        interpretation_id="native-fixture", owner_id="native", geometry_source_id="native-src",
        unit=unit, scale_to_metres=scale_to_metres(unit),
        basis=ResolutionBasis.user_confirmed)


def surface_state(unit: LengthUnit = FIXTURE_UNIT):
    return from_source_file(_interpretation(unit))


def cad_state(unit: LengthUnit = FIXTURE_UNIT):
    return from_occ_transfer(_interpretation(unit))


def prepared_surface_for(path, unit: LengthUnit = FIXTURE_UNIT):
    from pathlib import Path

    from meshpipeline.cad.prepared_surface import PreparedSurface

    return PreparedSurface(path=Path(path), source_id="native-src",
                           interpretation_id="native-fixture", consumed=surface_state(unit))
