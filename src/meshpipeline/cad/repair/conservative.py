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
#:
#: THESE ARE POLICY CATEGORIES, NOT OPERATION NAMES. Only `fix_small_edges` happens to match an
#: operation; `fix_wireframe`, `fix_face_boundaries`, `sew_shells` and `fill_planar_holes` are the
#: kernel capabilities a conservative pass is permitted to use, and nothing looks any of them up.
#: `fix_shape_global` is absent for the same reason - it is a whole-shape application of
#: capabilities already listed here, not a sixth permission. THE CATALOGUE IS
#: `operations.OPERATIONS`; read it there, and do not extend this tuple expecting the loop to
#: notice. (A unit test pins these strings, so they are not renamed to match the catalogue either:
#: what a report has already told operators the profile may do should not change spelling because
#: an internal class did.)
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


#: OpenCASCADE's `write.step.unit` name for each unit the pipeline resolves.
_STEP_UNIT_NAMES = {"mm": "MM", "cm": "CM", "m": "M", "in": "INCH"}


def _write_step(shape, destination: Path, *, unit=None) -> None:
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

    IN THE UPLOAD'S OWN UNIT (`unit`, the unit the parser applied to the upload), not always
    millimetres. Staging reads a file's coordinates RELATIVE TO THE UNIT IT DECLARES
    (contracts/coordinate_state.to_metres), and promotion carries the upload's interpretation onto
    these bytes. A millimetre file under an inch upload's interpretation was staged 25.4 times too
    large. The writer converts from the session's millimetres into the declared unit, so the
    repaired file reads back exactly as the upload does - including under an approved override.
    """
    from OCP.IFSelect import IFSelect_RetDone
    from OCP.Interface import Interface_Static
    from OCP.STEPControl import STEPControl_AsIs, STEPControl_Writer

    name = _STEP_UNIT_NAMES.get(str(getattr(unit, "value", unit) or "mm"))
    if name is None:
        raise RepairRefused(
            f"the upload's unit {unit!r} has no STEP name here, so the repaired file could not "
            "declare it - refusing rather than writing a file that may be mis-scaled")
    writer = STEPControl_Writer()
    if not Interface_Static.SetCVal_s("write.step.unit", name):
        raise RepairRefused(
            f"OpenCASCADE refused write.step.unit={name}, so the repaired file's declared unit "
            "cannot be guaranteed - refusing rather than writing a file that may be mis-scaled")
    writer.Transfer(shape, STEPControl_AsIs)
    if writer.Write(str(destination)) != IFSelect_RetDone:
        raise RepairRefused(f"the repaired shape could not be written to {destination.name}")


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


#: How parallel their planes must be (|dot| of unit normals).
_RIM_PARALLEL_TOLERANCE = 0.999
#: How far off one axis the two centroids may be, as a fraction of the smaller loop's size (its
#: largest in-plane distance from its own centroid). A cut passage is straight; the slack covers a
#: draft angle and a slightly wandering exported rim. Centroids rather than a fitted circle's
#: centre, so a hex, a slot, a square pocket or a faceted bore is measured the same way as a round
#: one - a through-hole of ANY cross-section seals the same way when both ends are patched.
_RIM_AXIS_TOLERANCE = 0.25
#: The axial gap floor, in units of the larger loop's size. Below it the loops are coplanar - two
#: holes in one face, which must stay fillable. THERE IS NO CEILING AND NO SIZE-RATIO CAP: a 1 mm
#: hole through a 25 mm plate and a spot-face three times its bore are both genuine through-holes,
#: and both caps once let exactly those be sealed. What separates a bore from two unrelated
#: openings is the inner-wire test in `_bore_like`, not how deep or how flared the passage is.
_RIM_MIN_GAP = 1e-3


def _loop_points(wire, per_edge: int = 24):
    """Points along a loop: each edge sampled evenly in its own parameter."""
    from OCP.BRepAdaptor import BRepAdaptor_Curve
    from OCP.TopAbs import TopAbs_EDGE
    from OCP.TopExp import TopExp_Explorer
    from OCP.TopoDS import TopoDS

    points = []
    explorer = TopExp_Explorer(wire, TopAbs_EDGE)
    while explorer.More():
        curve = BRepAdaptor_Curve(TopoDS.Edge_s(explorer.Current()))
        first, last = curve.FirstParameter(), curve.LastParameter()
        for k in range(per_edge):
            p = curve.Value(first + (last - first) * k / per_edge)
            points.append((p.X(), p.Y(), p.Z()))
        explorer.Next()
    return points


def _rim_axis_point(wire, normal):
    """The loop's centroid and in-plane size - (centroid, size) - or None when it has no extent.

    The centroid is the loop's LENGTH-weighted centre (linear properties), so it does not care how
    the edges were split; the size is the farthest sampled point from it in the loop's plane.
    """
    import numpy as np
    from OCP.BRepGProp import BRepGProp
    from OCP.GProp import GProp_GProps

    props = GProp_GProps()
    BRepGProp.LinearProperties_s(wire, props)
    if props.Mass() <= 0:
        return None
    c = props.CentreOfMass()
    centre = np.array([c.X(), c.Y(), c.Z()], dtype=float)
    points = np.asarray(_loop_points(wire), dtype=float)
    if len(points) < 3:
        return None
    n = np.asarray(normal, dtype=float)
    n /= np.linalg.norm(n)
    rel = points - centre
    in_plane = rel - np.outer(rel @ n, n)
    size = float(np.max(np.linalg.norm(in_plane, axis=1)))
    if size <= 0:
        return None
    return (tuple(float(k) for k in centre), size)


def _lone_face_boundaries(shape, loops: list) -> set[int]:
    """Indices of loops that are the WHOLE OUTER BOUNDARY OF ONE FACE - a loose face, not a hole.

    THE DISCRIMINATOR, AND WHY IT IS TOPOLOGICAL. A hole cut in a face is an INNER wire of that
    face (or, when the face was split across the hole, a loop running through several faces).
    An unsewn surface export instead leaves every face's own outer wire free: opposite faces of a
    loose box are parallel, offset along their normal and on one axis, which is everything the
    geometry of a bore pair is - so geometry alone cannot tell them apart, and the earlier answer
    (demand the loops be round) dropped every hex, slot and square through-hole from the check and
    let the fill seal them. Only a loop that IS one face's entire outer wire is set aside here;
    every other loop stays a candidate rim, which is the safe direction to be wrong in.
    """
    from OCP.BRepTools import BRepTools
    from OCP.TopAbs import TopAbs_EDGE, TopAbs_FACE
    from OCP.TopExp import TopExp, TopExp_Explorer
    from OCP.TopoDS import TopoDS
    from OCP.TopTools import TopTools_IndexedDataMapOfShapeListOfShape, TopTools_IndexedMapOfShape

    ancestors = TopTools_IndexedDataMapOfShapeListOfShape()
    TopExp.MapShapesAndAncestors_s(shape, TopAbs_EDGE, TopAbs_FACE, ancestors)

    def edges_of(wire):
        found = TopTools_IndexedMapOfShape()
        TopExp.MapShapes_s(wire, TopAbs_EDGE, found)
        return found

    lone: set[int] = set()
    for index, wire in enumerate(loops):
        mine = edges_of(wire)
        if mine.Extent() == 0:
            continue
        first = mine.FindKey(1)
        if not ancestors.Contains(first):
            continue
        for face_shape in ancestors.FindFromKey(first):
            outer = BRepTools.OuterWire_s(TopoDS.Face_s(face_shape))
            if outer.IsNull():
                continue
            theirs = edges_of(outer)
            if theirs.Extent() != mine.Extent():
                continue
            explorer = TopExp_Explorer(wire, TopAbs_EDGE)
            whole = True
            while explorer.More():
                if not theirs.Contains(explorer.Current()):
                    whole = False
                    break
                explorer.Next()
            if whole:
                lone.add(index)
                break
    return lone


def _paired_rims(loops: list, shape=None) -> set[int]:
    """Indices of loops that are the two ENDS OF A MISSING TUBE, not two separate holes.

    THE PART-RUINING CASE. A through-hole whose wall is missing leaves two rims: planar, parallel,
    on one axis, offset along their own normal. Each one looks exactly like a small fillable hole,
    and patching both SEALS the hole - handing back a part with no bolt hole where the customer had
    one. Demonstrated on a 100x100x10 plate with an 8 mm bore AND with an 8 mm hex: the hex was
    sealed end to end while the check demanded round rims. The cross-section does not matter.

    WHICH LOOPS ARE CANDIDATE RIMS. With `shape`, every loop except one that is a single face's
    entire outer wire (`_lone_face_boundaries`): a hole cut in a face is an inner wire of it, while
    an unsewn box's faces are each their own free outer wire. Without `shape` (bare wires, nothing
    to ask the topology) every loop is a candidate - the guard fails toward refusing a fill.

    WHICH CANDIDATES PAIR. Planes parallel, centroids on one axis, a non-zero gap along it. No
    roundness, no size ratio, no depth ceiling - each of those once let a genuine through-hole be
    sealed (the constants above say which).

    What the right repair is for them - rebuild the wall, or ask - is a judgement, so they are
    reported and left rather than guessed at. Both the planner and the review minting call THIS
    function, so there is one definition of a bore pair.
    """
    lone = _lone_face_boundaries(shape, loops) if shape is not None else set()
    rims = {}
    for index, wire in enumerate(loops):
        if index in lone:
            continue
        plane = _loop_plane(wire)
        if plane is None:
            continue
        axis_point = _rim_axis_point(wire, plane[0])
        if axis_point is not None:
            rims[index] = (plane[0], axis_point)

    paired: set[int] = set()
    indices = sorted(rims)
    for i, left in enumerate(indices):
        for right in indices[i + 1:]:
            n1, (c1, s1) = rims[left]
            n2, (c2, s2) = rims[right]
            dot = abs(n1[0] * n2[0] + n1[1] * n2[1] + n1[2] * n2[2])
            if dot < _RIM_PARALLEL_TOLERANCE:
                continue
            delta = [c2[k] - c1[k] for k in range(3)]
            # Offset along the shared normal rather than merely apart: two coplanar holes of the
            # same size in one face are two holes, and must stay fillable.
            offset = abs(sum(delta[k] * n1[k] for k in range(3)))
            lateral = max(sum(d * d for d in delta) - offset * offset, 0.0) ** 0.5
            if lateral > _RIM_AXIS_TOLERANCE * min(s1, s2):
                continue
            if offset > _RIM_MIN_GAP * max(s1, s2):
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
                     profile: RepairProfile = RepairProfile.conservative,
                     directives: tuple[dict, ...] = ()) -> RepairReport:
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

    from meshpipeline.cad.repair.plan import RulesPlanner
    from meshpipeline.cad.repair.triage import REVIEW_RESERVED_CODES

    # PER-DEFECT REVIEW HOLDS THE JUDGEMENT DEFECTS for a person: nothing automatic aims at them
    # until an engineer's directive does.
    reserved = REVIEW_RESERVED_CODES if rcfg.CAD_REPAIR_REVIEW_ENABLED else frozenset()
    planner: object = RulesPlanner(reserved=reserved)
    if directives:
        from meshpipeline.cad.repair.directives import DirectedPlanner

        planner = DirectedPlanner(directives, RulesPlanner(reserved=reserved), reserved=reserved)
    result = run_loop(before_shape, planner=planner, tolerance_mm=tolerance)
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
    # THE ATTEMPT LIST, in the one shape the node and the corpus read. `name` is what they look
    # up; `to_dict` also carries it as `op`, so that copy is dropped rather than emitting the same
    # value under two keys.
    attempts = tuple({"name": v.op, "mutated": v.kept,
                      **{k: x for k, x in v.to_dict().items() if k != "op"}}
                     for v in result.verdicts)

    refusal = (sanity_refusal(original, final)
               or cumulative_refusal(original, final, declared_area_mm2=declared_area,
                                     declared_volume_mm3=declared_volume))
    if refusal:
        # THE ATTEMPTS GO WITH THIS REFUSAL TOO, marked as a result thrown away whole: an
        # operation the session kept is NOT kept once the after-check discards the result, so a
        # directive answered by one is reported `not_reached` (discarded_with_result), never as
        # done - and never silently dropped either.
        measurements.setdefault("operations", [dict(a) for a in attempts])
        measurements["result_discarded"] = True
        raise RepairRefused(refusal, measurements=measurements)

    if measurements["operations_kept"] == 0:
        # NOTHING KEPT MEANS NOTHING TO PROMOTE, and writing the file anyway was worse than doing
        # nothing. `promote` refuses only BYTE-IDENTICAL geometry, which a fresh
        # `STEPControl_Writer` export never is, and `status_for` calls a report with no
        # error-severity defect `repaired` - so a run that repaired nothing replaced the customer's
        # upload with a re-export of it, and a STEP round trip CHANGES TOPOLOGY (the loose-faces
        # fixture arrives as 6 one-face shells and 0 solids). Refused before the write, so there is
        # no artifact for a later step to pick up; the attempt node treats this as a decline and
        # leaves the run on the original, which is the same outcome as not having tried.
        #
        # THE ATTEMPTS GO WITH IT, because this refusal is the ONLY record such a run produces.
        # The success path's report carries them; a decline that dropped them would delete the
        # training row for every file the loop tried and could not repair - and a rolled-back
        # sequence is the more valuable example, since it says what did NOT work. The node's
        # `RepairRefused` handler logs these before returning the decline.
        measurements["operations"] = [dict(a) for a in attempts]
        raise RepairRefused(
            f"nothing was kept ({measurements['operations_attempted']} operation(s) attempted, "
            f"stopped because {result.stopped_because}), so there is nothing to promote - "
            "re-exporting the upload would change its topology without repairing it",
            measurements=measurements)

    from meshpipeline.cad.unit_evidence import parser_applied_unit

    _write_step(result.shape, dest, unit=parser_applied_unit(src))

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
        # Built once, above, so the delivered report and a refusal's measurements cannot disagree
        # about what was tried.
        operations=attempts,
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
        base = ("Repaired within the conservative caps."
                if final.valid else
                "Repaired, but the result is not fully valid - needs review.")
        if result.defects:
            # `resolved` only means no ERROR-severity defect is left, so a surviving warning can
            # sit beside a resolved run. An unqualified success sentence over a non-empty defect
            # list is how an operator is told a part is clean when it is not.
            return (f"{base} {len(result.defects)} advisory defect(s) remain (warnings, not "
                    "errors) - see the defect list.")
        return base
    if result.stopped_because == SCAN_INCOMPLETE:
        return ("Repaired as far as the inspection could see; the defect scan was partial, so "
                "this is not a clean bill of health.")
    if not any(v.kept for v in result.verdicts):
        return "Nothing could be repaired safely; a person should look at this."
    return (f"Partially repaired - the run stopped at {result.stopped_because} with "
            f"{len(result.defects)} defect(s) left.")


def _refusal(before: dict, after: dict) -> str:
    """The one place a repair RESULT is judged. Returns the reason, or "" to accept.

    A SHIM over cad/repair/caps.py, which owns the arithmetic and splits it into the per-operation
    and cumulative halves the loop needs. `repair_step_file` no longer calls it: it judges the
    result with the loop's own declarations. What remains is the payload-driven entry point the
    unit tests exercise with recorded measurement dicts - worth keeping because a two-payload
    judgement needs no kernel, and `Measure.from_dict` tolerates payloads written before area and
    volume were recorded at all.

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
