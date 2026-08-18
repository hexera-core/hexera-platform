# Responsibility: Read the physical unit a CAD file declares, or say honestly that it does not.
# Boundaries: evidence, not a decision: a malformed unit context is reported unresolved rather than assumed.
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from meshpipeline.contracts.coordinate_state import OCC_OUTPUT_UNIT  # noqa: F401
from meshpipeline.contracts.geometry_units import LengthUnit

#: Unit names OCC reports that map onto the supported vocabulary.
_NAME_TO_UNIT = {
    "metre": LengthUnit.metre, "meter": LengthUnit.metre,
    "millimetre": LengthUnit.millimetre, "millimeter": LengthUnit.millimetre,
    "centimetre": LengthUnit.centimetre, "centimeter": LengthUnit.centimetre,
    "inch": LengthUnit.inch,
}


@dataclass(frozen=True)
class UnitEvidence:

    resolved: bool
    unit: LengthUnit | None = None
    detail: str = ""          # short, non-private: safe to show an operator

    @classmethod
    def unresolved(cls, detail: str) -> UnitEvidence:
        return cls(False, None, detail)


def _step_evidence(path: Path) -> UnitEvidence:
    from OCP.STEPControl import STEPControl_Reader

    return _step_evidence_with_reader(STEPControl_Reader(), path)


def _step_evidence_with_reader(reader, path: Path) -> UnitEvidence:
    from OCP.IFSelect import IFSelect_RetDone
    from OCP.TColStd import TColStd_SequenceOfAsciiString

    if reader.ReadFile(str(path)) != IFSelect_RetDone:
        return UnitEvidence.unresolved("the STEP file could not be read")

    lengths = TColStd_SequenceOfAsciiString()
    angles = TColStd_SequenceOfAsciiString()
    solids = TColStd_SequenceOfAsciiString()
    try:
        reader.FileUnits(lengths, angles, solids)
    except Exception:  # noqa: BLE001 - an unreadable unit section is simply no evidence
        return UnitEvidence.unresolved("the STEP file declares no readable unit")

    names = {lengths.Value(i).ToCString().strip().lower()
             for i in range(1, lengths.Length() + 1)}
    if not names:
        return UnitEvidence.unresolved("the STEP file declares no length unit")
    if len(names) > 1:
        # Mixed representations must not be collapsed into one global reading.
        return UnitEvidence.unresolved(
            f"the STEP file declares {len(names)} different length units")

    name = next(iter(names))
    unit = _NAME_TO_UNIT.get(name)
    if unit is None:
        return UnitEvidence.unresolved(f"unsupported length unit {name!r}")

    if not _step_unit_entity_is_well_formed(reader, unit):
        # FileUnits answers "metre" for a malformed SI unit too, and the shape then transfers a
        # thousand times too large - so the declaration is only believed when the entity parses.
        return UnitEvidence.unresolved("the STEP file's unit definition is malformed")
    return UnitEvidence(True, unit, f"declared in the file as {name}")


def _step_unit_entity_is_well_formed(reader, unit: LengthUnit) -> bool:
    if unit is LengthUnit.inch:
        return True                      # conversion-based: OCC resolved an explicit factor
    if unit is LengthUnit.metre:
        return False                     # indistinguishable from the parser's default
    try:
        model = reader.StepModel()
    except Exception:  # noqa: BLE001
        return False
    want = {LengthUnit.millimetre: "MILLI", LengthUnit.centimetre: "CENTI"}[unit]
    for i in range(1, model.NbEntities() + 1):
        entity = model.Entity(i)
        if "SiUnit" not in type(entity).__name__ or "Length" not in type(entity).__name__:
            continue
        try:
            if entity.HasPrefix() and want.lower() in str(entity.Prefix()).lower():
                return True
        except Exception:  # noqa: BLE001 - an entity we cannot interrogate is not evidence
            return False
    return False


def _iges_evidence(path: Path) -> UnitEvidence:
    from OCP.IFSelect import IFSelect_RetDone
    from OCP.IGESControl import IGESControl_Reader

    reader = IGESControl_Reader()
    if reader.ReadFile(str(path)) != IFSelect_RetDone:
        return UnitEvidence.unresolved("the IGES file could not be read")
    try:
        section = reader.IGESModel().GlobalSection()
        name = section.UnitName().ToCString().strip().lower()
        value = float(section.UnitValue())
        scale = float(section.Scale())
    except Exception:  # noqa: BLE001
        return UnitEvidence.unresolved("the IGES global section could not be read")

    if scale != 1.0:
        # A non-unit model scale changes physical size on top of the unit; rather than guess how
        # the two compose, ask.
        return UnitEvidence.unresolved("the IGES file applies a model scale")
    #: UnitValue is millimetres per file unit, which is how OCC normalises the transfer.
    by_value = {1.0: LengthUnit.millimetre, 10.0: LengthUnit.centimetre,
                1000.0: LengthUnit.metre, 25.4: LengthUnit.inch}
    unit = by_value.get(round(value, 6)) or _NAME_TO_UNIT.get(name.rstrip("."))
    if unit is None:
        return UnitEvidence.unresolved(f"unsupported IGES unit {name!r}")
    return UnitEvidence(True, unit, f"declared in the file as {name or unit.value}")


def read_declared_unit(path: str | Path) -> UnitEvidence:
    p = Path(path)
    suffix = p.suffix.lower()
    if suffix in (".step", ".stp"):
        return _step_evidence(p)
    if suffix in (".iges", ".igs"):
        return _iges_evidence(p)
    return UnitEvidence.unresolved(f"{suffix or 'this format'} does not record a unit")


def parser_applied_unit(path: str | Path) -> LengthUnit:
    from OCP.STEPControl import STEPControl_Reader
    from OCP.TColStd import TColStd_SequenceOfAsciiString

    p = Path(path)
    if p.suffix.lower() in (".igs", ".iges"):
        evidence = _iges_evidence(p)
        return evidence.unit if evidence.resolved and evidence.unit else LengthUnit.metre

    reader = STEPControl_Reader()
    try:
        from OCP.IFSelect import IFSelect_RetDone

        if reader.ReadFile(str(p)) != IFSelect_RetDone:
            return LengthUnit.metre
        lengths = TColStd_SequenceOfAsciiString()
        angles = TColStd_SequenceOfAsciiString()
        solids = TColStd_SequenceOfAsciiString()
        reader.FileUnits(lengths, angles, solids)
        names = {lengths.Value(i).ToCString().strip().lower()
                 for i in range(1, lengths.Length() + 1)}
    except Exception:  # noqa: BLE001 - an unreadable unit section means OCC used its default
        return LengthUnit.metre
    if len(names) != 1:
        return LengthUnit.metre
    return _NAME_TO_UNIT.get(next(iter(names)), LengthUnit.metre)
