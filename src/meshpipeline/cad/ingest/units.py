# Responsibility: Read the unit a mesh format states, for the few that record one in the file (3MF, Rhino).
# Owns: each format's unit vocabulary, mapped onto the supported LengthUnit.
# Boundaries: evidence only, like cad/unit_evidence.py: a unit outside the vocabulary is unresolved (asked), never rounded.
# Collaborates with: cad/unit_evidence.read_declared_unit (the one entry point callers use).
from __future__ import annotations

import re
import zipfile
from pathlib import Path

from meshpipeline.contracts.geometry_units import LengthUnit

_3MF_UNITS = {"millimeter": LengthUnit.millimetre, "centimeter": LengthUnit.centimetre,
              "meter": LengthUnit.metre, "inch": LengthUnit.inch}
_3MF_UNIT_ATTR = re.compile(rb"<(?:\w+:)?model\b[^>]*?\bunit\s*=\s*[\"']([A-Za-z]+)[\"']", re.S)
_3MF_MODEL_TAG = re.compile(rb"<(?:\w+:)?model\b")
_3DM_UNITS = {"Millimeters": LengthUnit.millimetre, "Centimeters": LengthUnit.centimetre,
              "Meters": LengthUnit.metre, "Inches": LengthUnit.inch}
#: A .3dm read at upload for its unit loads the whole model; above this size the unit is asked.
_3DM_UNIT_READ_MAX_BYTES = 256 * 1024 * 1024


def declared_unit(path: Path, key: str):
    """UnitEvidence for the formats this module knows, None for every other format."""
    from meshpipeline.cad.unit_evidence import UnitEvidence

    if key == "3mf":
        return _3mf_unit(path, UnitEvidence)
    if key == "3dm":
        return _3dm_unit(path, UnitEvidence)
    return None


def _3mf_unit(path: Path, UnitEvidence):
    from meshpipeline.cad.ingest.readers import _3mf_root_part

    try:
        with zipfile.ZipFile(path) as zf:
            with zf.open(_3mf_root_part(zf)) as fh:
                head = fh.read(16384)
    except Exception:  # noqa: BLE001 - an unreadable archive states nothing
        return UnitEvidence.unresolved("the 3MF file could not be read")
    tag = _3MF_MODEL_TAG.search(head)
    if tag is None:
        return UnitEvidence.unresolved("the 3MF model part has no model element")
    m = _3MF_UNIT_ATTR.search(head, tag.start())
    if m is None or m.start() != tag.start():
        # the 3MF specification: a model that names no unit is in millimetres
        return UnitEvidence(True, LengthUnit.millimetre,
                            "a 3MF model with no unit attribute is in millimetres")
    name = m.group(1).decode("ascii", "replace").lower()
    unit = _3MF_UNITS.get(name)
    if unit is None:
        return UnitEvidence.unresolved(f"unsupported 3MF unit {name!r}")
    return UnitEvidence(True, unit, f"declared in the file as {name}")


def _3dm_unit(path: Path, UnitEvidence):
    if path.stat().st_size > _3DM_UNIT_READ_MAX_BYTES:
        return UnitEvidence.unresolved("the Rhino file is too large to read its unit at upload")
    try:
        import rhino3dm

        model = rhino3dm.File3dm.Read(str(path))
        system = str(model.Settings.ModelUnitSystem).rsplit(".", 1)[-1] if model else ""
    except Exception:  # noqa: BLE001
        return UnitEvidence.unresolved("the Rhino file could not be read")
    unit = _3DM_UNITS.get(system)
    if unit is None:
        return UnitEvidence.unresolved(f"unsupported Rhino unit system {system or 'none'!r}")
    return UnitEvidence(True, unit, f"declared in the file as {system.lower()}")
