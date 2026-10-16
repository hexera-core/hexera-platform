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


def _fill_planar_holes(shape, *, tolerance_mm: float, diagonal: float) -> tuple:
    """Patch every FLAT closed free boundary. Returns (shape, what was filled, what was left).

    PLANAR ONLY, AND SIZE-CAPPED, because this is the one operation here that adds surface the
    customer never drew. Across a flat loop there is exactly one surface that can go there, so the
    patch is determined rather than invented. A non-planar loop has infinitely many - that is a
    judgement about the part's shape, and `BRepBuilderAPI_MakeFace(wire, OnlyPlane=True)` simply
    declines it, which is the behaviour we want: left alone, reported, and routed to a person.
    """
    from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeFace, BRepBuilderAPI_Sewing

    loops = _free_loops(shape)
    if not loops:
        return shape, [], []

    filled: list[dict] = []
    left: list[dict] = []
    patches = []
    rims = _paired_rims(loops)
    for position, wire in enumerate(loops):
        span = _span(wire)
        ratio = (span / diagonal) if diagonal > 0 else 0.0
        if position in rims:
            # SEALING THIS WOULD DELETE A FEATURE. Two rims of a missing tube wall, not two holes.
            left.append({"reason": "paired_rims", "span": round(span, 6),
                         "span_ratio": round(ratio, 6),
                         "detail": ("this loop pairs with another of the same size on a parallel "
                                    "plane - a missing tube wall, and patching both would seal a "
                                    "through-hole")})
            continue
        if diagonal > 0 and ratio > rcfg.CAD_REPAIR_MAX_HOLE_SPAN_RATIO:
            # NOT A HOLE, A MISSING WALL. A flat lid over one is a different part.
            left.append({"reason": "too_large", "span": round(span, 6),
                         "span_ratio": round(ratio, 6)})
            continue
        maker = BRepBuilderAPI_MakeFace(wire, True)
        if not maker.IsDone():
            left.append({"reason": "not_planar", "span": round(span, 6),
                         "span_ratio": round(ratio, 6)})
            continue
        patches.append(maker.Face())
        filled.append({"span": round(span, 6), "span_ratio": round(ratio, 6)})

    if not patches:
        return shape, filled, left

    sewing = BRepBuilderAPI_Sewing(tolerance_mm)
    sewing.SetNonManifoldMode(False)
    sewing.Add(shape)
    for patch in patches:
        sewing.Add(patch)
    sewing.Perform()
    sewn = sewing.SewedShape()
    if sewn is None or (hasattr(sewn, "IsNull") and sewn.IsNull()):
        return shape, [], left + [{"reason": "sewing_failed"}]
    return _as_solid_if_closed(sewn), filled, left


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


def _fix(shape, *, tolerance_mm: float):
    """Run the conservative operation set.

    Returns (shape, holes filled, holes left alone, whether anything was sewn). The last of those
    is REPORTED rather than inferred: a loose body can arrive as bare faces with no shell at all,
    so sewing it makes the shell count go UP, and a report that reads sewing off a count then
    omits an operation that changed the customer's file.

    ShapeFix_Shape is given a tolerance to WORK AT, which is not a promise about what it leaves
    behind - it may raise a sub-shape's tolerance well past this to make the shape close. That is
    precisely why the caps are measured on the RESULT rather than trusted from the request.
    """
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

    # SKIP THE SEWING, NOT THE PATCHING. An earlier version returned early for any shape that had
    # a solid, which also skipped hole filling - so a part with a solid AND an opening elsewhere
    # was never patched, and a real corpus came back "no_change" on every such file. Sewing is the
    # operation that must not run on a closed body, because it returns a shell and the solid is
    # lost; patching is judged on its own evidence.
    sewn, sewed = _sew_loose_bodies(fixed, tolerance_mm=tolerance_mm)

    if not rcfg.CAD_REPAIR_FILL_PLANAR_HOLES:
        # STILL REPORTED. A repair that leaves the defect it was run for must say so: an operator
        # reading "repaired" over a part that still has a hole in it is how a ruined delivery
        # happens.
        return sewn, [], [{"reason": "filling_disabled", "span": round(_span(wire), 6)}
                          for wire in _free_loops(sewn)], sewed

    filled_shape, filled, left = _fill_planar_holes(
        sewn, tolerance_mm=tolerance_mm, diagonal=_diagonal_mm(sewn))
    return filled_shape, filled, left, sewed


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

    src, dest = Path(source), Path(destination)
    before_shape = _read_shape(src)
    before = {
        "valid": _is_valid(before_shape),
        "diagonal_mm": _diagonal_mm(before_shape),
        "max_tolerance_mm": _max_tolerance_mm(before_shape),
        **_counts(before_shape),
        **_area_and_volume(before_shape),
    }

    tolerance = min(rcfg.CAD_REPAIR_MAX_TOLERANCE_MM,
                    max(before["diagonal_mm"], 1.0) * rcfg.CAD_REPAIR_MAX_DEVIATION_RATIO)
    fixed_shape, filled, unfilled, sewed = _fix(before_shape, tolerance_mm=tolerance)

    after = {
        "valid": _is_valid(fixed_shape),
        "diagonal_mm": _diagonal_mm(fixed_shape),
        "max_tolerance_mm": _max_tolerance_mm(fixed_shape),
        **_counts(fixed_shape),
        **_area_and_volume(fixed_shape),
    }
    measurements = {"before": before, "after": after, "tolerance_requested_mm": tolerance,
                    "operations": list(_CONSERVATIVE_OPERATIONS),
                    # WHAT SURFACE WAS ADDED, and what was left alone and why. A repair that
                    # invented geometry has to say so and say how much, because a reviewer
                    # approving it is approving the invention.
                    "holes_filled": filled,
                    "holes_left": unfilled}

    # THE AFTER-CHECKS. Each one refuses a specific way a "successful" repair can still be the
    # wrong answer. They run on the RESULT because an OpenCASCADE tolerance is a request.
    refusal = _refusal(before, after)
    if refusal:
        raise RepairRefused(refusal, measurements=measurements)

    # REPORTED BY THE OPERATION ITSELF. Sewing runs per body, so neither "the shape had a solid"
    # nor any shell-count comparison tells you whether it ran: bare faces have no shell to count
    # down from. The operation says what it did, and the report repeats it.
    performed = [op for op in _CONSERVATIVE_OPERATIONS
                 if op != "sew_shells" or sewed]
    if "fill_planar_holes" in performed and not filled:
        performed.remove("fill_planar_holes")

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
        # ONLY WHAT RAN. Listing the whole profile as `mutated` claimed a sewing pass on a sound
        # single-solid part that deliberately skipped it, which is an inaccurate account of a
        # repair an operator is being asked to approve.
        operations=tuple({"name": op, "mutated": True} for op in performed),
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

    # THE BOUNDING BOX CANNOT SEE INSIDE. An internal wall moved, a cavity reshaped or a passage
    # sealed changes nothing about the part's overall extent, so a deviation check that only
    # measures the box accepted repairs that altered the part where it matters most. Surface area
    # and volume do move for those, and a patched opening moves them legitimately - which is why
    # the allowance here is the hole-span cap rather than the far tighter deviation ratio.
    for name, allowance in (("surface_area_mm2", rcfg.CAD_REPAIR_MAX_HOLE_SPAN_RATIO),
                            ("volume_mm3", rcfg.CAD_REPAIR_MAX_HOLE_SPAN_RATIO)):
        was, now = float(before.get(name) or 0.0), float(after.get(name) or 0.0)
        if was <= 0:
            continue
        changed = abs(now - was) / was
        if changed > allowance:
            return (f"the repair changed the part's {name.split('_')[0]} by {changed:.4f} of its "
                    f"original, over the {allowance} allowance - something moved inside the part")
    if before["valid"] and not after["valid"]:
        return "the repair turned a valid shape into an invalid one"
    return ""


def status_for(report: RepairReport) -> RepairStatus:
    """What a completed repair's report amounts to. A warning is still a repair, not a failure."""
    if any(d.severity in (DefectSeverity.error, DefectSeverity.fatal) for d in report.defects):
        return RepairStatus.unrepairable
    return RepairStatus.repaired
