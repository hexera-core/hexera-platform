# Responsibility: Inspect CAD B-rep files for repair-relevant defects.
# Boundaries: diagnostics only; never mutates geometry and never exports repaired shapes.
from __future__ import annotations

from pathlib import Path

from meshpipeline.cad.repair.contracts import (
    DefectCode,
    DefectSeverity,
    RepairDefect,
    RepairMeasurement,
    RepairReport,
)


class _CadReadError(ValueError):
    """Expected input failure while reading or transferring a CAD B-rep."""


def _reader_for(path: Path):
    from OCP.IGESControl import IGESControl_Reader
    from OCP.STEPControl import STEPControl_Reader

    return (
        IGESControl_Reader()
        if path.suffix.lower() in (".iges", ".igs")
        else STEPControl_Reader()
    )


def _read_shape(path: Path):
    from OCP.IFSelect import IFSelect_RetDone

    reader = _reader_for(path)
    if reader.ReadFile(str(path)) != IFSelect_RetDone:
        raise _CadReadError(f"OpenCASCADE could not read CAD file: {path.name}")
    if reader.TransferRoots() <= 0:
        raise _CadReadError(f"OpenCASCADE could not transfer roots from CAD file: {path.name}")
    shape = reader.OneShape()
    if shape is None or (hasattr(shape, "IsNull") and shape.IsNull()):
        raise _CadReadError(f"OpenCASCADE produced an empty shape for CAD file: {path.name}")
    return shape


def _count_subshapes(shape) -> dict:
    from OCP.TopAbs import (
        TopAbs_EDGE,
        TopAbs_FACE,
        TopAbs_SHELL,
        TopAbs_SOLID,
        TopAbs_VERTEX,
    )
    from OCP.TopExp import TopExp_Explorer

    def count(kind) -> int:
        n = 0
        explorer = TopExp_Explorer(shape, kind)
        while explorer.More():
            n += 1
            explorer.Next()
        return n

    return {
        "solids": count(TopAbs_SOLID),
        "shells": count(TopAbs_SHELL),
        "faces": count(TopAbs_FACE),
        "edges": count(TopAbs_EDGE),
        "vertices": count(TopAbs_VERTEX),
    }


def _is_valid(shape) -> bool:
    from OCP.BRepCheck import BRepCheck_Analyzer

    return bool(BRepCheck_Analyzer(shape, True).IsValid())


def inspect_brep_file(path: Path) -> RepairReport:
    p = Path(path)
    suffix = p.suffix.lower()
    fmt = "iges" if suffix in (".iges", ".igs") else "step"
    try:
        shape = _read_shape(p)
        counts = _count_subshapes(shape)
        is_valid = _is_valid(shape)
        defects = ()
        if not is_valid:
            defects = (
                RepairDefect(
                    code=DefectCode.invalid_brep,
                    severity=DefectSeverity.error,
                    message="OpenCASCADE reported the B-rep as invalid.",
                ),
            )
        measurements = {"format": fmt, "is_valid": is_valid, **counts}
        summary = "Repair recommended before meshing." if defects else "No repair needed."
    except _CadReadError as exc:
        defects = (
            RepairDefect(
                code=DefectCode.invalid_brep,
                severity=DefectSeverity.fatal,
                message="OpenCASCADE could not read this CAD file.",
                details={"error": type(exc).__name__},
            ),
        )
        measurements = {
            "format": fmt,
            "is_valid": False,
            "solids": 0,
            "shells": 0,
            "faces": 0,
            "edges": 0,
            "vertices": 0,
        }
        summary = "Automatic repair was not safe for this geometry."

    return RepairReport(
        defects=defects,
        measurements=tuple(RepairMeasurement(name=k, value=v) for k, v in measurements.items()),
        operations=({"name": "inspect_brep", "mutated": False},),
        summary=summary,
        diagnostics={"path_suffix": suffix},
    )
