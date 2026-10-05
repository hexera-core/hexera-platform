# Responsibility: Attempt a bounded OpenCASCADE repair of a CAD B-rep and prove the result stayed
#                 inside its caps - or refuse it.
# Owns: the operation set a conservative profile may run, the after-checks, and the refusal.
# Boundaries: it writes a NEW file and never touches the input. It does not decide whether a job
#             should be repaired, does not upload anything, and does not record an attempt.
# Collaborates with: settings/cad_repair.py (the caps), cad/repair/contracts.py (the report).
from __future__ import annotations

import logging
from pathlib import Path

import meshpipeline.settings.cad_repair as rcfg
from meshpipeline.cad.repair.contracts import (
    DefectCode,
    DefectSeverity,
    RepairDefect,
    RepairMeasurement,
    RepairProfile,
    RepairReport,
    RepairStatus,
)

logger = logging.getLogger(__name__)


class RepairRefused(RuntimeError):
    """The repair ran but its result may not be used.

    SEPARATE FROM A FAILURE. A refusal means the kernel produced something and we are declining
    it: the geometry moved too far, a tolerance was inflated past the cap, faces disappeared. The
    customer's file is unharmed and the honest next step is an operator's judgement, not a retry.
    """

    def __init__(self, message: str, *, measurements: dict | None = None) -> None:
        super().__init__(message)
        self.measurements = dict(measurements or {})


class RepairUnavailable(RuntimeError):
    """Repair cannot run here at all - the switch is off, or the kernel is absent.

    OUR limitation, never a statement about the file. It must never reach a customer as a verdict
    on their geometry.
    """


#: What a conservative profile is ALLOWED to do, named so a report can say it and a reviewer can
#: check it. Each entry is an OpenCASCADE operation that closes what is already there.
#:
#: WHAT IS DELIBERATELY ABSENT: face removal, defeaturing, and any unbounded tolerance inflation.
#: Those change what the part IS rather than repairing how it was written, and that is a judgement
#: about the customer's intent which no automatic pass gets to make.
_CONSERVATIVE_OPERATIONS: tuple[str, ...] = (
    "fix_small_edges",      # degenerate and vanishing edges the writer left behind
    "fix_wireframe",        # wire ordering, gaps between edges of the same wire
    "fix_face_boundaries",  # face/wire consistency, same-parameter and pcurve repair
    "sew_shells",           # coincident face boundaries joined into closed shells
)


def _occ():
    try:
        from OCP.BRepBuilderAPI import BRepBuilderAPI_Sewing  # noqa: F401
        from OCP.ShapeFix import ShapeFix_Shape  # noqa: F401
    except ImportError as exc:  # pragma: no cover - exercised in the native tier
        raise RepairUnavailable(
            "the CAD kernel (OCP/OpenCASCADE) is not installed in this process") from exc


def _diagonal_mm(shape) -> float:
    from OCP.Bnd import Bnd_Box
    from OCP.BRepBndLib import BRepBndLib

    box = Bnd_Box()
    BRepBndLib.Add_s(shape, box)
    if box.IsVoid():
        return 0.0
    xmin, ymin, zmin, xmax, ymax, zmax = box.Get()
    return float(((xmax - xmin) ** 2 + (ymax - ymin) ** 2 + (zmax - zmin) ** 2) ** 0.5)


def _max_tolerance_mm(shape) -> float:
    from OCP.ShapeAnalysis import ShapeAnalysis_ShapeTolerance

    return float(ShapeAnalysis_ShapeTolerance().Tolerance(shape, 1))


def _counts(shape) -> dict:
    from meshpipeline.cad.repair.brep import _count_subshapes

    return _count_subshapes(shape)


def _is_valid(shape) -> bool:
    from meshpipeline.cad.repair.brep import _is_valid as _valid

    return _valid(shape)


def _write_step(shape, destination: Path) -> None:
    from OCP.IFSelect import IFSelect_RetDone
    from OCP.STEPControl import STEPControl_AsIs, STEPControl_Writer

    writer = STEPControl_Writer()
    writer.Transfer(shape, STEPControl_AsIs)
    if writer.Write(str(destination)) != IFSelect_RetDone:
        raise RepairRefused(f"the repaired shape could not be written to {destination.name}")


def _fix(shape, *, tolerance_mm: float):
    """Run the conservative operation set: a ShapeFix pass, and sewing ONLY where it belongs.

    ShapeFix_Shape is given a tolerance to WORK AT, which is not a promise about what it leaves
    behind - it may raise a sub-shape's tolerance well past this to make the shape close. That is
    precisely why the caps below are measured on the RESULT rather than trusted from the request.

    SEWING IS FOR SHAPES THAT ARE NOT CLOSED YET. BRepBuilderAPI_Sewing joins coincident face
    boundaries and returns a shell; handed a shape that already contains a solid it returns the
    sewn shell and the solid is GONE. A real-kernel test caught exactly that - a sound box went in
    with one solid and came out with none - which is damage, not repair. So the sewing pass runs
    only when there is no solid to lose, and the refusal below treats a lost solid as damage
    whatever produced it.
    """
    from OCP.BRepBuilderAPI import BRepBuilderAPI_Sewing
    from OCP.ShapeFix import ShapeFix_Shape

    fixer = ShapeFix_Shape(shape)
    fixer.SetPrecision(tolerance_mm)
    fixer.SetMaxTolerance(tolerance_mm)
    # TOPOLOGY MAY BE REPAIRED, GEOMETRY MAY NOT BE REWRITTEN. OpenCASCADE can rebuild surfaces
    # and curves to force a fit; that changes what the customer drew rather than how it was
    # written down, so the conservative profile leaves it off.
    fixer.FixSolidMode = 1
    fixer.FixFreeShellMode = 1
    fixer.FixFreeFaceMode = 1
    fixer.FixSameParameterMode = 1
    fixer.FixVertexPositionMode = 0
    fixer.Perform()
    fixed = fixer.Shape()

    if _counts(fixed)["solids"] > 0:
        # Already closed. ShapeFix has done the conservative work; sewing from here could only
        # take the solid away.
        return fixed

    sewing = BRepBuilderAPI_Sewing(tolerance_mm)
    # NON-MANIFOLD MODE OFF: joining three faces along one edge produces a shape no mesher will
    # accept, and calling that a repair would move the failure downstream rather than fix it.
    sewing.SetNonManifoldMode(False)
    sewing.Add(fixed)
    sewing.Perform()
    sewn = sewing.SewedShape()
    if sewn is None or (hasattr(sewn, "IsNull") and sewn.IsNull()):
        return fixed
    return sewn


def repair_step_file(source: Path, destination: Path, *,
                     profile: RepairProfile = RepairProfile.conservative) -> RepairReport:
    """Repair `source` into `destination`, or refuse. The input is never modified.

    Raises RepairUnavailable when repair is switched off or the kernel is missing, and
    RepairRefused when the result exceeded its caps - the two are not the same answer and must not
    be reported as one.
    """
    if not rcfg.CAD_REPAIR_ENABLED:
        # REFUSED BEFORE ANY KERNEL CALL. A deployment that has not opted in cannot mutate a
        # customer's CAD by any path through this module.
        raise RepairUnavailable(
            "CAD repair is switched off in this deployment (CAD_REPAIR_ENABLED)")
    if profile is not RepairProfile.conservative:
        raise RepairUnavailable(
            f"only the conservative profile is implemented; {profile.value} needs an operator")
    _occ()

    from meshpipeline.cad.repair.brep import _read_shape

    src, dest = Path(source), Path(destination)
    before_shape = _read_shape(src)
    before = {
        "valid": _is_valid(before_shape),
        "diagonal_mm": _diagonal_mm(before_shape),
        "max_tolerance_mm": _max_tolerance_mm(before_shape),
        **_counts(before_shape),
    }

    tolerance = min(rcfg.CAD_REPAIR_MAX_TOLERANCE_MM,
                    max(before["diagonal_mm"], 1.0) * rcfg.CAD_REPAIR_MAX_DEVIATION_RATIO)
    fixed_shape = _fix(before_shape, tolerance_mm=tolerance)

    after = {
        "valid": _is_valid(fixed_shape),
        "diagonal_mm": _diagonal_mm(fixed_shape),
        "max_tolerance_mm": _max_tolerance_mm(fixed_shape),
        **_counts(fixed_shape),
    }
    measurements = {"before": before, "after": after, "tolerance_requested_mm": tolerance,
                    "operations": list(_CONSERVATIVE_OPERATIONS)}

    # THE AFTER-CHECKS. Each one refuses a specific way a "successful" repair can still be the
    # wrong answer. They run on the RESULT because an OpenCASCADE tolerance is a request.
    refusal = _refusal(before, after)
    if refusal:
        raise RepairRefused(refusal, measurements=measurements)

    _write_step(fixed_shape, dest)

    defects: tuple[RepairDefect, ...] = ()
    if not after["valid"]:
        # Not a refusal: the shape improved and is honest about not being fully valid. An operator
        # decides whether it is good enough to mesh, with this on the screen.
        defects = (
            RepairDefect(
                code=DefectCode.invalid_brep,
                severity=DefectSeverity.warning,
                message="The repaired shape is still not fully valid; a person should look at it.",
            ),
        )
    return RepairReport(
        defects=defects,
        measurements=tuple(RepairMeasurement(name=k, value=v) for k, v in measurements.items()),
        operations=tuple({"name": op, "mutated": True} for op in _CONSERVATIVE_OPERATIONS),
        summary=("Repaired within the conservative caps."
                 if after["valid"] else
                 "Repaired, but the result is not fully valid - needs review."),
        diagnostics={"profile": profile.value, "source_suffix": src.suffix.lower()},
    )


def _refusal(before: dict, after: dict) -> str:
    """The one place a repair result is judged. Returns the reason, or "" to accept."""
    if after["faces"] == 0:
        return "the repair left no faces to mesh"
    if after["solids"] < before["solids"]:
        # A SOLID IS WHAT MAKES A PART A VOLUME. Losing one turns a closed body into a bag of
        # surfaces, which most meshers will either refuse or quietly mesh as something else. It is
        # damage however it happened, so it is refused here rather than only prevented upstream.
        return (f"the repair lost {before['solids'] - after['solids']} solid(s) - a closed body "
                "became loose surfaces, which is damage rather than repair")
    if after["faces"] < before["faces"] and not rcfg.CAD_REPAIR_ALLOW_FACE_REMOVAL:
        # DELETING A FACE IS DEFEATURING, which is a judgement about what the part is for. A
        # conservative pass that quietly removed one has changed the customer's design.
        return (f"the repair removed {before['faces'] - after['faces']} face(s); deleting "
                "geometry is an operator decision, not a conservative repair")
    if after["max_tolerance_mm"] > rcfg.CAD_REPAIR_MAX_TOLERANCE_MM:
        return (f"the repair left a tolerance of {after['max_tolerance_mm']:.4f} mm, over the "
                f"{rcfg.CAD_REPAIR_MAX_TOLERANCE_MM} mm cap - the kernel agreed to stop "
                "complaining rather than fixing the geometry")
    diagonal = before["diagonal_mm"]
    if diagonal > 0:
        moved = abs(after["diagonal_mm"] - diagonal) / diagonal
        if moved > rcfg.CAD_REPAIR_MAX_DEVIATION_RATIO:
            return (f"the repair moved the part's overall size by {moved:.5f} of its diagonal, "
                    f"over the {rcfg.CAD_REPAIR_MAX_DEVIATION_RATIO} cap")
    if before["valid"] and not after["valid"]:
        return "the repair turned a valid shape into an invalid one"
    return ""


def status_for(report: RepairReport) -> RepairStatus:
    """What a completed repair's report amounts to. A warning is still a repair, not a failure."""
    if any(d.severity in (DefectSeverity.error, DefectSeverity.fatal) for d in report.defects):
        return RepairStatus.unrepairable
    return RepairStatus.repaired
