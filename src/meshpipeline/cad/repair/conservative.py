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
#:
#: SUPERSEDED AS A RECIPE, KEPT AS A POLICY. The loop (cad/repair/loop.py) chooses which of
#: cad/repair/operations.py's operations to run and in what order, so this tuple no longer
#: describes a sequence. What it still states is the SET a conservative profile may draw from.
_CONSERVATIVE_OPERATIONS: tuple[str, ...] = (
    "fix_small_edges",      # degenerate and vanishing edges the writer left behind
    "fix_wireframe",        # wire ordering, gaps between edges of the same wire
    "fix_face_boundaries",  # face/wire consistency, same-parameter and pcurve repair
    "sew_shells",           # coincident face boundaries joined into closed shells
    "fill_planar_holes",    # a FLAT patch across a FLAT closed loop - the one operation that adds
                            # geometry, because sewing cannot close a hole with nothing to stitch to
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


def _area_and_volume(shape) -> dict:
    """Surface area and enclosed volume - the two measures that move when something INSIDE moves.

    A bounding box is blind to an internal wall, a reshaped cavity or a sealed passage. These are
    not: they are what lets the acceptance checks notice a repair that changed the part where a
    customer would care most and an extent check would see nothing.
    """
    from OCP.BRepGProp import BRepGProp
    from OCP.GProp import GProp_GProps

    out = {"surface_area_mm2": 0.0, "volume_mm3": 0.0}
    try:
        surface = GProp_GProps()
        BRepGProp.SurfaceProperties_s(shape, surface)
        out["surface_area_mm2"] = abs(float(surface.Mass()))
    except Exception as exc:  # noqa: BLE001 - a measure we cannot take is reported as zero,
        logger.debug("conservative: no surface area for this shape (%s)", exc)
    try:
        volume = GProp_GProps()
        BRepGProp.VolumeProperties_s(shape, volume)
        out["volume_mm3"] = abs(float(volume.Mass()))
    except Exception as exc:  # noqa: BLE001 - and a zero is skipped by the check rather than
        # treated as a 100% change, so an unmeasurable shape is never refused for being unmeasurable
        logger.debug("conservative: no volume for this shape (%s)", exc)
    return out


def _counts(shape) -> dict:
    from meshpipeline.cad.repair.brep import _count_subshapes

    return _count_subshapes(shape)


def _is_valid(shape) -> bool:
    from meshpipeline.cad.repair.brep import _is_valid as _valid

    return _valid(shape)


def _write_step(shape, destination: Path) -> None:
    """Write the repaired shape, DECLARING the unit its coordinates are actually in.

    `write.step.unit` is a PROCESS-GLOBAL OpenCASCADE static. Left unset, the writer uses whatever
    the last caller in this process happened to set - and a file whose header declares inches
    while its coordinates are millimetres is a part wrong by a factor of 25.4. That is the worst
    failure class this pipeline has, and it would pass every check here because the shape itself
    is sound.

    OpenCASCADE transfers a STEP into its own system unit, millimetres, on read. So the shape in
    hand is in millimetres and the file must say so: then the repaired file and the original are
    interchangeable to any reader, which is exactly what promotion assumes when it carries the
    original's interpretation across. The writer is constructed BEFORE the unit is set, because
    `write.step.unit` is registered by the STEP controller's initialisation and setting it first
    silently does nothing.
    """
    from OCP.IFSelect import IFSelect_RetDone
    from OCP.Interface import Interface_Static
    from OCP.STEPControl import STEPControl_AsIs, STEPControl_Writer

    writer = STEPControl_Writer()
    if not Interface_Static.SetCVal_s("write.step.unit", "MM"):
        raise RepairRefused(
            "OpenCASCADE refused write.step.unit=MM, so the repaired file's declared unit cannot "
            "be guaranteed - refusing rather than writing a file that may be mis-scaled")
    writer.Transfer(shape, STEPControl_AsIs)
    if writer.Write(str(destination)) != IFSelect_RetDone:
        raise RepairRefused(f"the repaired shape could not be written to {destination.name}")


def _free_loops(shape) -> list:
    """The closed free-boundary wires on a shape - its holes, as loops."""
    from OCP.ShapeAnalysis import ShapeAnalysis_FreeBounds
    from OCP.TopAbs import TopAbs_WIRE
    from OCP.TopExp import TopExp_Explorer
    from OCP.TopoDS import TopoDS

    out = []
    try:
        wires = ShapeAnalysis_FreeBounds(shape).GetClosedWires()
        if wires is None:
            return []
        explorer = TopExp_Explorer(wires, TopAbs_WIRE)
        while explorer.More():
            out.append(TopoDS.Wire_s(explorer.Current()))
            explorer.Next()
    except Exception as exc:  # noqa: BLE001 - no free bounds is the common case
        logger.debug("conservative: free-bound analysis unavailable (%s)", exc)
    return out


def _span(shape) -> float:
    from OCP.Bnd import Bnd_Box
    from OCP.BRepBndLib import BRepBndLib

    box = Bnd_Box()
    BRepBndLib.Add_s(shape, box)
    if box.IsVoid():
        return 0.0
    xmin, ymin, zmin, xmax, ymax, zmax = box.Get()
    return float(((xmax - xmin) ** 2 + (ymax - ymin) ** 2 + (zmax - zmin) ** 2) ** 0.5)


def _loop_plane(wire):
    """The plane a closed loop lies in, as (normal, centroid), or None when it is not planar."""
    from OCP.BRepAdaptor import BRepAdaptor_Surface
    from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeFace
    from OCP.GeomAbs import GeomAbs_Plane

    maker = BRepBuilderAPI_MakeFace(wire, True)
    if not maker.IsDone():
        return None
    surface = BRepAdaptor_Surface(maker.Face())
    if surface.GetType() != GeomAbs_Plane:
        return None
    axis = surface.Plane().Axis()
    direction = axis.Direction()
    geometry = _centre(wire)
    if geometry is None:
        return None
    return ((direction.X(), direction.Y(), direction.Z()), geometry)


def _centre(shape):
    from OCP.Bnd import Bnd_Box
    from OCP.BRepBndLib import BRepBndLib

    box = Bnd_Box()
    BRepBndLib.Add_s(shape, box)
    if box.IsVoid():
        return None
    xmin, ymin, zmin, xmax, ymax, zmax = box.Get()
    return ((xmin + xmax) / 2, (ymin + ymax) / 2, (zmin + zmax) / 2)


#: How close two rims' spans must be to count as the same opening seen twice.
_RIM_SPAN_TOLERANCE = 0.02
#: How parallel their planes must be (|dot| of unit normals).
_RIM_PARALLEL_TOLERANCE = 0.999


def _paired_rims(loops: list) -> set[int]:
    """Indices of loops that are the two ENDS OF A MISSING TUBE, not two separate holes.

    THE PART-RUINING CASE, and the reason filling is not autonomous yet. A drilled through-hole
    whose bore wall is missing leaves two rims: planar, parallel, congruent, offset along their
    own normal. Each one looks exactly like a small fillable hole, and patching both SEALS the
    hole - handing back a part with no bolt hole where the customer had one. Demonstrated on a
    100x100x10 plate with an 8 mm bore: two loops, identical 22.627 spans, both far inside the
    size cap.

    What the right repair is for them - rebuild the cylinder, or ask - is a judgement, so they are
    reported and left rather than guessed at.
    """
    planes = {}
    for index, wire in enumerate(loops):
        plane = _loop_plane(wire)
        if plane is not None:
            planes[index] = (plane, _span(wire))

    paired: set[int] = set()
    indices = sorted(planes)
    for i, left in enumerate(indices):
        for right in indices[i + 1:]:
            (n1, c1), s1 = planes[left]
            (n2, c2), s2 = planes[right]
            if s1 <= 0 or s2 <= 0:
                continue
            # SIZE IS NOT THE TEST. A TAPERED through-hole - a countersink, a draft-angled bore -
            # has rims of different diameters, so requiring congruence let exactly that case
            # through and sealed a feature the customer drew. What makes two loops the ends of one
            # passage is that they are parallel and offset ALONG their shared normal; the sizes
            # may differ by any amount.
            dot = abs(n1[0] * n2[0] + n1[1] * n2[1] + n1[2] * n2[2])
            if dot < _RIM_PARALLEL_TOLERANCE:
                continue
            # Offset along the shared normal rather than merely apart: two coplanar holes of the
            # same size in one face are two holes, and must stay fillable.
            offset = abs(sum((c2[k] - c1[k]) * n1[k] for k in range(3)))
            lateral = sum((c2[k] - c1[k]) ** 2 for k in range(3)) ** 0.5
            if offset > max(s1, s2) * 0.01 and offset >= lateral * 0.99:
                paired.add(left)
                paired.add(right)
    return paired


def _as_solid_if_closed(shape):
    """Promote a shell to a solid ONLY if it is genuinely closed, else hand back what came in.

    THE CLOSURE CHECK IS THE POINT. `BRepBuilderAPI_MakeSolid` wraps whatever shell it is given and
    never looks at whether it is closed. A repair loop that patches one hole at a time can hand it
    a shell that still has a free boundary (one rim of a missing bore wall patched, the other not),
    and wrapping that yields a solid-typed shape that is really open - and the type is trusted by
    everything downstream (solid counts, volume accounting), so it is worse than an honest shell.

    Closed means BOTH `BRep_Tool.IsClosed_s(shell)` and no remaining free boundary
    (`ShapeAnalysis_FreeBounds`). Not promoting is not an error: the solid-count cap judges the
    result, so returning the shell unchanged is the conservative outcome.
    """
    from OCP.BRep import BRep_Tool
    from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeSolid
    from OCP.ShapeAnalysis import ShapeAnalysis_FreeBounds
    from OCP.TopAbs import TopAbs_SHELL, TopAbs_WIRE
    from OCP.TopExp import TopExp_Explorer
    from OCP.TopoDS import TopoDS

    try:
        shells = TopExp_Explorer(shape, TopAbs_SHELL)
        found = []
        while shells.More():
            found.append(TopoDS.Shell_s(shells.Current()))
            shells.Next()
        # ONE SHELL ONLY. Adding every shell to one solid fused separate assembly bodies into a
        # single purported part - and the acceptance checks allow the solid count to RISE, so
        # nothing downstream caught it. A multi-shell shape is left as it is; whether those shells
        # are one body is a question about the assembly, not something to assume.
        if len(found) != 1:
            return shape
        if not BRep_Tool.IsClosed_s(found[0]):
            return shape
        bounds = ShapeAnalysis_FreeBounds(found[0])
        for wires in (bounds.GetClosedWires(), bounds.GetOpenWires()):
            if wires is not None and TopExp_Explorer(wires, TopAbs_WIRE).More():
                return shape
        maker = BRepBuilderAPI_MakeSolid()
        maker.Add(found[0])
        solid = maker.Solid()
        if solid is None or (hasattr(solid, "IsNull") and solid.IsNull()):
            return shape
        return solid
    except Exception as exc:  # noqa: BLE001
        logger.debug("conservative: could not promote a closed shell to a solid (%s)", exc)
        return shape


def _sew_loose_bodies(shape, *, tolerance_mm: float) -> tuple:
    """Sew each body with no solid to lose; leave every body that has one untouched.

    Returns (shape, whether any body was actually joined).

    WHY PER BODY. The two whole-shape answers are both wrong on a mixed assembly. Sew it and the
    solids are destroyed, because sewing returns a shell. Skip it and a body that arrived as loose
    faces - a surface export, a translated assembly - stays loose, and the customer is handed a
    "repaired" file that still will not mesh. A STEP assembly is a compound of independent bodies,
    so each one is judged on its own.

    The caps still have the last word: a sewing that cost a solid, a face or volume is refused
    downstream exactly as before.
    """
    if _counts(shape)["solids"] == 0:
        # one open body, nothing to lose - sew it whole, as this has always done
        return _sew(shape, tolerance_mm=tolerance_mm), True

    from OCP.BRep import BRep_Builder
    from OCP.TopAbs import TopAbs_COMPOUND
    from OCP.TopoDS import TopoDS_Compound, TopoDS_Iterator

    if shape.ShapeType() != TopAbs_COMPOUND:
        # a single solid body: sewing has nothing to add and a solid to lose
        return shape, False

    children = []
    iterator = TopoDS_Iterator(shape)
    while iterator.More():
        children.append(iterator.Value())
        iterator.Next()
    if not children:
        return shape, False

    builder = BRep_Builder()
    rebuilt = TopoDS_Compound()
    builder.MakeCompound(rebuilt)
    sewed_any = False
    for child in children:
        if _counts(child)["solids"] > 0:
            builder.Add(rebuilt, child)
            continue
        joined = _sew(child, tolerance_mm=tolerance_mm)
        # JOINED, judged on this body alone. Bare faces have no shell, so sewing them makes the
        # count rise; a sewn shell of several faces makes it fall. Either is a change, and "no
        # change" is only the case where the topology is untouched.
        before_body, after_body = _counts(child), _counts(joined)
        sewed_any = sewed_any or (after_body["shells"] != before_body["shells"]
                                  or after_body["faces"] != before_body["faces"])
        builder.Add(rebuilt, joined)
    # UNCHANGED MEANS UNCHANGED. If no body was actually joined, the original shape is returned
    # rather than a re-wrapped copy of it, so a repair that did nothing cannot look like one that
    # rebuilt the assembly.
    return (rebuilt, True) if sewed_any else (shape, False)


def _sew(shape, *, tolerance_mm: float):
    """Join coincident face boundaries into shells. Only for a shape with no solid to lose.

    BRepBuilderAPI_Sewing returns a SHELL; handed a shape that already contains a solid it hands
    back the sewn shell and the solid is gone. A real-kernel test caught exactly that - a sound box
    went in with one solid and came out with none.
    """
    from OCP.BRepBuilderAPI import BRepBuilderAPI_Sewing

    # NON-MANIFOLD MODE OFF: joining three faces along one edge produces a shape no mesher will
    # accept, and calling that a repair would move the failure downstream rather than fix it.
    sewing = BRepBuilderAPI_Sewing(tolerance_mm)
    sewing.SetNonManifoldMode(False)
    sewing.Add(shape)
    sewing.Perform()
    sewn = sewing.SewedShape()
    if sewn is None or (hasattr(sewn, "IsNull") and sewn.IsNull()):
        return shape
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
    from meshpipeline.cad.repair.caps import cumulative_refusal, measure, sanity_refusal
    from meshpipeline.cad.repair.loop import run as run_loop

    src, dest = Path(source), Path(destination)
    before_shape = _read_shape(src)
    original = measure(before_shape)

    tolerance = min(rcfg.CAD_REPAIR_MAX_TOLERANCE_MM,
                    max(original.diagonal_mm, 1.0) * rcfg.CAD_REPAIR_MAX_DEVIATION_RATIO)

    result = run_loop(before_shape, tolerance_mm=tolerance)
    final = measure(result.shape)

    measurements = {
        "before": original.to_dict(), "after": final.to_dict(),
        "tolerance_requested_mm": tolerance,
        # WHY IT STOPPED, which is not inferable from the shape. "Every error cleared" and "I ran
        # out of operations" look identical in the geometry and mean opposite things to a reviewer.
        "stopped_because": result.stopped_because,
        "resolved": result.resolved,
        "complete_scan": result.complete_scan,
        "planner": result.planner_label,
        # WHAT WAS DELIBERATELY NOT ATTEMPTED AND WHY - the paired rims of a missing bore wall
        # above all. This used to be a silent skip inside the fill.
        "refusals": [dict(r) for r in result.refusals],
        "operations_attempted": len(result.verdicts),
        "operations_kept": sum(1 for v in result.verdicts if v.kept),
    }

    # THE AFTER-CHECKS STILL HAVE THE LAST WORD. The session already refused any individual
    # operation that left the caps, so reaching here with a refusal would mean a bug - but the
    # customer's part is on the other side of this function, so it is checked again on the result
    # rather than trusted from the process that produced it.
    #
    # GIVEN THE LOOP'S OWN DECLARATIONS. A caller holding only two payloads declares nothing, which
    # is right for `_refusal`; here the kept operations' declared surface is known, and judging the
    # result without it would refuse every legitimate fill at the last step.
    declared_area = sum(float(v.evidence.get("area_delta_mm2", 0.0) or 0.0)
                        for v in result.verdicts if v.kept)
    declared_volume = sum(float(v.evidence.get("volume_delta_mm3", 0.0) or 0.0)
                          for v in result.verdicts if v.kept)
    measurements["declared_area_mm2"] = declared_area
    measurements["declared_volume_mm3"] = declared_volume
    refusal = (sanity_refusal(original, final)
               or cumulative_refusal(original, final, declared_area_mm2=declared_area,
                                     declared_volume_mm3=declared_volume))
    if refusal:
        raise RepairRefused(refusal, measurements=measurements)

    _write_step(result.shape, dest)

    defects: tuple[RepairDefect, ...] = ()
    if not final.valid:
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
        # EVERY ATTEMPT, KEPT OR NOT, with what it aimed at and what it measured. A rolled-back
        # operation is evidence an operator wants and the training example a later model planner
        # needs, so the log is of attempts rather than of successes.
        operations=tuple({"name": v.op, "mutated": v.kept, **v.to_dict()}
                         for v in result.verdicts),
        summary=_summary(result, final),
        diagnostics={"profile": profile.value, "source_suffix": src.suffix.lower()},
        # WHAT IS LEFT, not what arrived. An operator reading "repaired" over the original defect
        # list is how a ruined delivery happens.
        entities=tuple(d.to_dict() for d in result.defects),
    )


def _summary(result, final) -> str:
    """One sentence an operator reads first. It must not claim more than the run achieved.

    KEYED ON `resolved`, NOT ON THE STOP REASON. `resolved` already means the loop succeeded -
    complete scan, no error defects, and nothing a failed attempt abandoned - so anything else
    has to read as partial, whatever the loop happened to stop on.
    """
    from meshpipeline.cad.repair.loop import SCAN_INCOMPLETE

    if result.resolved:
        return ("Repaired within the conservative caps."
                if final.valid else
                "Repaired, but the result is not fully valid - needs review.")
    if result.stopped_because == SCAN_INCOMPLETE:
        return ("Repaired as far as the inspection could see; the defect scan was partial, so "
                "this is not a clean bill of health.")
    if not any(v.kept for v in result.verdicts):
        return "Nothing could be repaired safely; a person should look at this."
    return (f"Partially repaired - the run stopped at {result.stopped_because} with "
            f"{len(result.defects)} defect(s) left.")


def _refusal(before: dict, after: dict) -> str:
    """The one place a repair RESULT is judged. Returns the reason, or "" to accept.

    NOW A SHIM over cad/repair/caps.py, which owns the arithmetic and splits it into the
    per-operation and cumulative halves the loop needs. Kept because the native suite and
    `devtools/validation/repair_corpus.py` drive it directly with recorded measurement payloads -
    including ones written before area and volume were recorded at all, which is why
    `Measure.from_dict` tolerates a sparse dict.

    Declares no area or volume: a caller holding only two payloads has no operation evidence to
    account for, so an unexplained change is exactly what it should refuse on.
    """
    from meshpipeline.cad.repair.caps import Measure, cumulative_refusal, sanity_refusal

    was, now = Measure.from_dict(before), Measure.from_dict(after)
    return (sanity_refusal(was, now)
            or cumulative_refusal(was, now, declared_area_mm2=0.0, declared_volume_mm3=0.0))


def status_for(report: RepairReport) -> RepairStatus:
    """What a completed repair's report amounts to. A warning is still a repair, not a failure."""
    if any(d.severity in (DefectSeverity.error, DefectSeverity.fatal) for d in report.defects):
        return RepairStatus.unrepairable
    return RepairStatus.repaired
