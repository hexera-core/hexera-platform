# Responsibility: State what unit a COMPLETED mesh artifact is written in - one answer, recorded, never inferred.
# Boundaries: it validates and reports the declared unit; it does not scale geometry and does not read mesh files.
# Collaborates with: contracts/geometry_units.py and the engine finalize seams that declare the unit.
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from meshpipeline.contracts.geometry_units import LengthUnit

#: THE key. Named once so a second spelling cannot quietly become a second authority.
MESH_UNITS_KEY = "mesh_units"

#: The ONE unit a completed mesh may declare. Reuses the existing closed vocabulary rather than
#: introducing a second enum - `mm`, `cm` and `in` remain valid INPUT units and are simply not
#: valid answers to this question.
COMPLETED_MESH_UNIT: LengthUnit = LengthUnit.metre


class MeshUnitsError(RuntimeError):

    def __init__(self, *args, failure_class=None, dependency="mesh_manifest"):
        super().__init__(*args)
        self.failure_class = failure_class
        self.dependency = dependency


def validate_completed_mesh_unit(value: Any) -> LengthUnit:
    if isinstance(value, bool) or not isinstance(value, str):
        raise MeshUnitsError(
            f"{MESH_UNITS_KEY} must be the string {COMPLETED_MESH_UNIT.value!r}, "
            f"not {type(value).__name__}")
    if value != COMPLETED_MESH_UNIT.value:
        raise MeshUnitsError(
            f"{MESH_UNITS_KEY}={value!r} is not a completed-mesh unit; every engine emits "
            f"{COMPLETED_MESH_UNIT.value!r} and a delivered mesh is never rescaled")
    return COMPLETED_MESH_UNIT


def completed_mesh_unit(manifest: Mapping[str, Any] | None) -> LengthUnit:
    if not isinstance(manifest, Mapping):
        raise MeshUnitsError("no mesh manifest to read units from")
    if MESH_UNITS_KEY not in manifest:
        raise MeshUnitsError(
            f"the manifest does not state {MESH_UNITS_KEY}; a completed mesh whose unit is "
            "unknown cannot be shown or reviewed")
    return validate_completed_mesh_unit(manifest[MESH_UNITS_KEY])
