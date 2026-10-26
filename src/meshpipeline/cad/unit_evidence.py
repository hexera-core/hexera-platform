# Responsibility: Read the physical unit a CAD file declares, or say honestly that it does not.
# Boundaries: evidence, not a decision: a malformed unit context is reported unresolved rather than assumed.
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from meshpipeline.contracts.coordinate_state import OCC_OUTPUT_UNIT  # noqa: F401
from meshpipeline.contracts.geometry_units import SCALE_TO_METRES, LengthUnit

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


def unit_of_scale(metres_per_unit: float) -> LengthUnit | None:
    """The supported unit a declared scale is, to a part in a million; None for any other (a
    foot, a micrometre), which is asked rather than rounded to the nearest one we know."""
    for unit, scale in SCALE_TO_METRES.items():
        if abs(metres_per_unit - scale) <= 1e-6 * scale:
            return unit
    return None


def _step_evidence(path: Path) -> UnitEvidence:
    """What the file's geometric contexts assign as their length unit, read from its text.

    OpenCASCADE's FileUnits answers "metre" for a malformed unit too - a bad prefix, a missing
    name - and the shape then transfers a thousand times too large; that is why a declared metre
    used to be distrusted outright, and every file drawn in metres (a turbine blade, a car in an
    ANSA export, a cascade) was read as millimetres. The entity itself is parsed here instead: a
    well-formed SI_UNIT($,.METRE.) is metres, a CONVERSION_BASED_UNIT('METRE', 1 x m) is metres,
    and only an entity that does not parse is refused."""
    from meshpipeline.cad.step_units import declared_lengths

    try:
        found = declared_lengths(path)
    except OSError:
        return UnitEvidence.unresolved("the STEP file could not be read")
    if not found:
        return UnitEvidence.unresolved("the STEP file declares no length unit")
    if any(d.metres is None for d in found):
        return UnitEvidence.unresolved("the STEP file's unit definition is malformed")
    if len(found) > 1:
        # Mixed representations must not be collapsed into one global reading.
        return UnitEvidence.unresolved(
            f"the STEP file declares {len(found)} different length units")
    declared = found[0]
    unit = unit_of_scale(float(declared.metres or 0.0))
    if unit is None:
        return UnitEvidence.unresolved(
            f"unsupported length unit {declared.name.lower()!r} ({declared.metres:g} m)")
    return UnitEvidence(True, unit, f"declared in the file as {declared.name.lower()}")


@dataclass(frozen=True)
class StatedMostly:
    """The length unit most of a mixed STEP file's geometric contexts assign, and in words how
    the file divides - the first reading for a file that states more than one unit."""

    unit: LengthUnit
    detail: str


def stated_mostly(path: str | Path) -> StatedMostly | None:
    """The unit most of a STEP file's geometric contexts assign, when it states more than one and
    every one of them is well-formed and one we carry; None otherwise (one unit, none, a malformed
    one, or a tie). Evidence for a first reading only: a mixed file is still asked about, because
    which unit the user means for the part as a whole is theirs to say."""
    from meshpipeline.cad.step_units import declared_length_uses

    p = Path(path)
    if p.suffix.lower() not in (".step", ".stp"):
        return None
    try:
        uses = declared_length_uses(p)
    except OSError:
        return None
    if len(uses) < 2 or any(d.metres is None for d, _n in uses):
        return None
    counted: dict[LengthUnit, int] = {}
    for declared, n in uses:
        unit = unit_of_scale(float(declared.metres or 0.0))
        if unit is None:
            return None
        counted[unit] = counted.get(unit, 0) + n
    ranked = sorted(counted.items(), key=lambda kv: -kv[1])
    if len(ranked) < 2:
        return None
    (top, n_top), (_second, n_second) = ranked[0], ranked[1]
    if n_top == n_second:
        return None
    rest = " and ".join(_UNIT_WORDS[u] for u, _n in ranked[1:])
    return StatedMostly(top, f"the file states more than one length unit ({_UNIT_WORDS[top]} for "
                             f"most of its parts, {rest} for the rest)")


#: How a unit is said in a sentence.
_UNIT_WORDS = {LengthUnit.metre: "metres", LengthUnit.millimetre: "millimetres",
               LengthUnit.centimetre: "centimetres", LengthUnit.inch: "inches"}


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
    from meshpipeline.cad.ingest.canonical import read_sidecar
    from meshpipeline.contracts.intake_formats import format_for_key, format_for_suffix

    p = Path(path)
    suffix = p.suffix.lower()
    converted = read_sidecar(p)
    if converted is not None:
        # A file cad/ingest wrote: its own unit label is the converter's, not the user's. What
        # counts is what the SOURCE stated (a BREP states nothing, though its STEP says mm),
        # which the conversion recorded beside it.
        declared = converted.get("declared_unit") or {}
        if declared.get("resolved") and declared.get("unit"):
            try:
                return UnitEvidence(True, LengthUnit(declared["unit"]),
                                    str(declared.get("detail") or ""))
            except ValueError:
                pass
        source = format_for_key(str(converted.get("source_format", "")))
        return UnitEvidence.unresolved(
            str(declared.get("detail") or "")
            or f"the uploaded {source.label if source else 'file'} does not record a unit")
    if suffix in (".step", ".stp"):
        if _faceted(p):
            # a mesh wrapped as STEP: the unit context is the converter's default, not evidence
            from meshpipeline.cad.ingest.canonical import _faceted_unit_detail

            return UnitEvidence.unresolved(_faceted_unit_detail(p))
        return _step_evidence(p)
    if suffix in (".iges", ".igs"):
        return _iges_evidence(p)
    fmt = format_for_suffix(suffix)
    if fmt is not None and fmt.declares_units:
        from meshpipeline.cad.ingest.units import declared_unit

        evidence = declared_unit(p, fmt.key)
        if evidence is not None:
            return evidence
    return UnitEvidence.unresolved(f"{suffix or 'this format'} does not record a unit")


def _faceted(path: Path) -> bool:
    from meshpipeline.cad.ingest.step_facets import is_faceted_step

    try:
        return is_faceted_step(path) is not None
    except (OSError, ValueError):
        return False


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
    if len(names) == 1 and (named := _NAME_TO_UNIT.get(next(iter(names)))) is not None:
        return named
    if names:
        # OCC transfers each representation in its own context's unit, by the conversion the
        # context states - whatever the unit is CALLED. So a file that lists more than one, or
        # one OCC reports only by an exporter's own name for it (Inventor's '__CONSTANT UNIT
        # inch'), comes out in consistent millimetres; what was applied is what the text
        # declares - for a file that mixes well-formed units, the unit most of its parts state.
        # Reporting OCC's metre fallback here read an Inventor hub 50 mm across as 1.3 mm (and,
        # with no unit confirmed, as 50 um, where the check's mesher refused the deflection).
        evidence = _step_evidence(p)
        if evidence.resolved and evidence.unit:
            return evidence.unit
        mostly = stated_mostly(p)
        return mostly.unit if mostly is not None else LengthUnit.metre
    # NO LENGTH UNIT AT ALL: OpenCASCADE applies no conversion, so the file's numbers come out as
    # its system unit unchanged - millimetres. (A malformed unit is different: OCC names it, as
    # its metre fallback, and scales by it.) Reporting metres here read a 10-unit part as 10 um.
    try:
        from meshpipeline.cad.step_units import declared_lengths

        if not declared_lengths(p):
            return LengthUnit.millimetre
    except OSError:
        pass
    return LengthUnit.metre
