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


_WORDS = {LengthUnit.metre: ("metres", "m"), LengthUnit.millimetre: ("millimetres", "mm"),
          LengthUnit.centimetre: ("centimetres", "cm"), LengthUnit.inch: ("inches", "in")}


def export_units_words(source_unit: Any = None) -> str:
    """THE ONE SENTENCE every surface uses for what unit the delivered files are in - the result
    screen, the report and the chat - so none of them can say another (the 2 Nov dry run: the chat
    told the knuckle's user "the coordinates will remain in millimetres" while every file was in
    metres). `source_unit` is the part's own unit as the user confirmed it, when known: the
    conversion is then said with its factor. Places and sizes we show the user (the check, the plan,
    the result's notes) stay in millimetres, as measured on the part."""
    out_words, out_abbr = _WORDS[COMPLETED_MESH_UNIT]
    try:
        unit = LengthUnit(source_unit) if source_unit else None
    except ValueError:
        unit = None
    said = f"Lengths in the delivered files are in {out_words}"
    if unit is None:
        said += " (converted from your part's own unit)."
    elif unit is COMPLETED_MESH_UNIT:
        said += ", the unit your part is in."
    else:
        from meshpipeline.contracts.geometry_units import scale_to_metres
        words, abbr = _WORDS[unit]
        said += (f": your part is in {words}, so every coordinate is converted "
                 f"(1 {abbr} = {scale_to_metres(unit):g} {out_abbr}).")
    return (said + " Places and sizes shown on the check and in the result's notes stay in "
            "millimetres, as measured on your part. Use consistent units for loads and materials in "
            "your solver (for example N, m, Pa).")


def completed_mesh_unit(manifest: Mapping[str, Any] | None) -> LengthUnit:
    if not isinstance(manifest, Mapping):
        raise MeshUnitsError("no mesh manifest to read units from")
    if MESH_UNITS_KEY not in manifest:
        raise MeshUnitsError(
            f"the manifest does not state {MESH_UNITS_KEY}; a completed mesh whose unit is "
            "unknown cannot be shown or reviewed")
    return validate_completed_mesh_unit(manifest[MESH_UNITS_KEY])
