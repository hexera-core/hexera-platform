# Responsibility: Inspect CAD B-rep files for repair-relevant defects.
# Boundaries: diagnostics only; never mutates geometry and never exports repaired shapes.
from __future__ import annotations

import logging
from pathlib import Path

from meshpipeline.cad.repair.contracts import (
    DefectCode,
    DefectSeverity,
    RepairDefect,
    RepairMeasurement,
    RepairReport,
)

logger = logging.getLogger(__name__)


class _CadReadError(ValueError):
    """Expected input failure while reading or transferring a CAD B-rep."""


def _kernel_failures() -> tuple[type[BaseException], ...]:
    """Input failures the CAD kernel raises rather than returns.

    OpenCASCADE reports some corrupt files through a return code and others by
    raising, and both are facts about the file, not defects in this code. A
    programmer error still propagates, so the two stay distinguishable.
    """
    try:
        from OCP.Standard import Standard_Failure
    except ImportError:
        return (_CadReadError,)
    return (_CadReadError, Standard_Failure)


def _reader_for(path: Path):
    from OCP.IGESControl import IGESControl_Reader
    from OCP.STEPControl import STEPControl_Reader

    return (
        IGESControl_Reader()
        if path.suffix.lower() in (".iges", ".igs")
        else STEPControl_Reader()
    )


def _read_shape(path: Path):
    if not path.exists():
        raise FileNotFoundError(path)
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


def _summarise_defects(found) -> tuple[RepairDefect, ...]:
    """One RepairDefect per defect KIND, carrying the entities it was found on.

    The located detail lives in `RepairReport.entities`; this is the summary a list view reads.
    Both come from the same findings, so they cannot disagree about what is wrong.
    """
    by_code: dict = {}
    for entity_defect in found:
        key = entity_defect.code
        bucket = by_code.setdefault(key, {"severity": entity_defect.severity, "entities": [],
                                          "messages": []})
        bucket["entities"].append(f"{entity_defect.entity_type}:{entity_defect.entity_index}")
        if entity_defect.message not in bucket["messages"]:
            bucket["messages"].append(entity_defect.message)
        # The worst severity for this kind wins the summary row.
        if _SEVERITY_RANK[entity_defect.severity] > _SEVERITY_RANK[bucket["severity"]]:
            bucket["severity"] = entity_defect.severity
    return tuple(
        RepairDefect(code=code, severity=bucket["severity"],
                     message=" ".join(bucket["messages"])[:400],
                     count=len(bucket["entities"]),
                     details={"entities": bucket["entities"][:50],
                              "entity_count": len(bucket["entities"])})
        for code, bucket in by_code.items())


_SEVERITY_RANK = {DefectSeverity.info: 0, DefectSeverity.warning: 1,
                  DefectSeverity.error: 2, DefectSeverity.fatal: 3}


def inspect_brep_file(path: Path) -> RepairReport:
    p = Path(path)
    suffix = p.suffix.lower()
    fmt = "iges" if suffix in (".iges", ".igs") else "step"
    entities: tuple[dict, ...] = ()
    try:
        shape = _read_shape(p)
        counts = _count_subshapes(shape)
        is_valid = _is_valid(shape)

        # WHERE IT IS BROKEN, not just whether. `IsValid()` is one bit about the whole part and
        # cannot be acted on by an operator or aimed at by a repair; this attributes the damage to
        # entities, with their size and position (cad/repair/localize.py).
        from meshpipeline.cad.repair.localize import localize, summarise
        try:
            found = localize(shape)
        except Exception as exc:  # noqa: BLE001
            # EVIDENCE, NEVER LOAD-BEARING. Localisation reaches deep into the kernel and a shape
            # it cannot walk must still get an inspection: the report then carries no entities,
            # which triage already reads as "nothing to aim at" rather than "nothing wrong".
            logger.warning("inspect_brep: could not localise defects (%s) - reporting the "
                           "whole-shape result only", exc)
            found = ()
        entities = tuple(d.to_dict() for d in found)
        located = summarise(found)

        defects = _summarise_defects(found)
        if counts["faces"] == 0:
            defects = (
                RepairDefect(
                    code=DefectCode.invalid_brep,
                    severity=DefectSeverity.fatal,
                    message="The CAD file carries no faces to mesh.",
                    details={"reason": "no_faces"},
                ),
            )
        elif not is_valid and not found:
            # INVALID, AND WE CANNOT SAY WHY. This is a real and distinct answer: the kernel
            # objects to the part but attributes the objection to no entity, so there is nothing
            # for an automatic repair to aim at and nothing to tell a customer beyond the fact.
            # Triage treats exactly this case as one for a person, which is why it must not be
            # conflated with the located defects above.
            defects = (
                RepairDefect(
                    code=DefectCode.invalid_brep,
                    severity=DefectSeverity.error,
                    message=("OpenCASCADE reports the B-rep as invalid but attributes it to no "
                             "entity, so the cause is not localised."),
                    details={"reason": "unattributed"},
                ),
            )

        measurements = {"format": fmt, "is_valid": is_valid, **counts, "located": located}
        if not defects:
            summary = "No repair needed."
        elif counts["faces"] == 0:
            summary = "Automatic repair was not safe for this geometry."
        else:
            _worst = max((d.severity for d in defects), key=lambda sv: _SEVERITY_RANK[sv])
            _where = located.get("by_region", {})
            _inside = _where.get("interior", 0)
            summary = (
                f"{located['total']} located problem(s) across "
                f"{len(located['by_code'])} kind(s)"
                + (f", {_inside} inside the part" if _inside else "")
                + ("." if _worst is not DefectSeverity.fatal
                   else "; automatic repair was not safe for this geometry.")
            ) if found else "Repair recommended before meshing."
    except _kernel_failures() as exc:
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
        entities=entities,
    )
