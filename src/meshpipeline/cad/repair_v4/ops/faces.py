# Responsibility: The face-rebuild family - ops that REPLACE geometry: rebuild a face, fill a loop,
#                 re-approximate, snap to a canonical surface, and refit a cluster into one face.
# Owns: five `Op`s (`OPS`) and their kernel functions (`op_rebuild_face` ... `op_refit_cluster`).
# Boundaries: kernel functions run in a worker (OCP imported lazily); the `Op` classes run in the
#             parent and only move ids and paths. Choosing WHICH faces is the caller's (sites, Task
#             17; search, Task 18). Registry wiring is integration's: this module only exposes `OPS`.
"""Face rebuild ops - the most geometry-altering ops in the library, so every one of them MEASURES what it did.

NO FIDELITY GATE (owner decision 2026-11-24: clean = valid + meshable). The deviation an op causes, the area it moves
and the span of a filled loop are measured and returned in the record under `"measured"` (with the part's
`bound_mm` = min(0.1 mm, 0.001 x diagonal) beside them for reference), but no op declines because of them, and
`tol_mm` is not capped. An op still declines for what makes its OWN result unusable: an invalid result, a loop that
is not a loop, a fill or fit the kernel could not build. Declining returns `{"declined": reason}` and writes
nothing.

DEVIATION IS MEASURED, NOT ASSUMED: sample points of one face (UV grid classified inside the
trimmed face, plus points along its edges) are measured to the other face with
BRepExtrema_DistShapeShape. Replacing ops measure both ways (old -> new and new -> old); the numbers
come back in the record under `"measured"`.

IDENTITY. Every replacement goes through ShapeBuild_ReShape.Apply on the WHOLE part, so every
untouched face, edge and vertex stays the SAME TShape (`ws_kernel.history_record`'s IsSame
contract); only the replaced face and its ancestors are rebuilt, and the ReShape's own
BRepTools_History says so. A rebuilt face is Modified 1:1 and keeps its stable id.

DECLARED ADDS. `fill_loop` and `refit_cluster` create a face the history cannot attribute to an
old face, so the workspace names it fresh. The op then DECLARES it (`Diff.declared_added`), because
the fidelity check measures declared adds one-sided (original -> repaired) and an undeclared face
reads as an unexplained violation. `refit_cluster` also declares the cluster as removed. The
declared ids are only known after `apply_history` has minted them, so `apply` fills them in on the
returned Diff from the kernel's `added_face_indices`.
"""

from __future__ import annotations

import math
import os
import time
from typing import Any

from meshpipeline.cad.repair_v4.kernel_calls import register as register_kernel_fn
from meshpipeline.cad.repair_v4.ops import OpDeclined, OpFailed, run_kernel_op, validate_call
from meshpipeline.cad.repair_v4.types import Site, StableId
from meshpipeline.cad.repair_v4.worker_pool import WorkerPool
from meshpipeline.cad.repair_v4.workspace import Diff, Workspace


class _Declined(Exception):
    """Raised inside a kernel function for an OCCT step that did not complete; the function's
    wrapper turns it into `{"declined": reason}` and removes any half-written output."""


def _declines(fn: Any) -> Any:
    import functools

    @functools.wraps(fn)
    def wrapper(payload: dict) -> dict:
        try:
            return fn(payload)
        except _Declined as why:
            return _decline(payload, str(why))
    return wrapper


#: Absolute ceiling of the fidelity bound, mm.
BOUND_MAX_MM = 0.1
#: Relative ceiling: the bound is at most this fraction of the part's bounding-box diagonal.
BOUND_DIAGONAL_RATIO = 0.001
#: Sampling density of the deviation measurement (UV grid is N x N per face).
SAMPLES_PER_AXIS = 12
#: refit_cluster fits a GRID_NODES x GRID_NODES grid of points.
GRID_NODES = 24


def fidelity_bound(diagonal: float) -> float:
    return min(BOUND_MAX_MM, BOUND_DIAGONAL_RATIO * diagonal)


# --- kernel side: measuring ---------------------------------------------------------------------


def _face_points(face: Any, n: int = SAMPLES_PER_AXIS) -> list[Any]:
    """Points ON `face`: a UV grid kept where the trimmed face contains it, plus its edge points."""
    from OCP.BRepAdaptor import BRepAdaptor_Surface
    from OCP.BRepClass import BRepClass_FaceClassifier
    from OCP.gp import gp_Pnt2d
    from OCP.TopAbs import TopAbs_IN, TopAbs_ON

    points: list[Any] = []
    surface = BRepAdaptor_Surface(face, True)
    u0, u1, v0, v1 = (surface.FirstUParameter(), surface.LastUParameter(),
                      surface.FirstVParameter(), surface.LastVParameter())
    for i in range(n + 1):
        for j in range(n + 1):
            u, v = u0 + (u1 - u0) * i / n, v0 + (v1 - v0) * j / n
            state = BRepClass_FaceClassifier(face, gp_Pnt2d(u, v), 1e-7).State()
            if state in (TopAbs_IN, TopAbs_ON):
                points.append(surface.Value(u, v))
    return points + _edge_points(face)


def _edge_points(face: Any) -> list[Any]:
    from OCP.BRepAdaptor import BRepAdaptor_Curve
    from OCP.TopAbs import TopAbs_EDGE
    from OCP.TopExp import TopExp_Explorer
    from OCP.TopoDS import TopoDS

    points: list[Any] = []
    explorer = TopExp_Explorer(face, TopAbs_EDGE)
    while explorer.More():
        curve = BRepAdaptor_Curve(TopoDS.Edge_s(explorer.Current()))
        a, b = curve.FirstParameter(), curve.LastParameter()
        points.extend(curve.Value(a + (b - a) * k / 20) for k in range(21))
        explorer.Next()
    return points


def _max_distance(points: list[Any], target: Any) -> float:
    """Largest distance from any of `points` to `target` (a shape)."""
    from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeVertex
    from OCP.BRepExtrema import BRepExtrema_DistShapeShape

    worst = 0.0
    for point in points:
        extrema = BRepExtrema_DistShapeShape(BRepBuilderAPI_MakeVertex(point).Vertex(), target)
        if not extrema.IsDone():
            return math.inf
        worst = max(worst, float(extrema.Value()))
    return worst


def _distance_to_surface(points: list[Any], surface: Any) -> float:
    """Largest distance from any of `points` to the UNTRIMMED `surface`."""
    from OCP.GeomAPI import GeomAPI_ProjectPointOnSurf

    worst = 0.0
    for point in points:
        projection = GeomAPI_ProjectPointOnSurf(point, surface)
        if projection.NbPoints() == 0:
            return math.inf
        worst = max(worst, float(projection.LowerDistance()))
    return worst


def _two_way(old_faces: list[Any], new_faces: list[Any]) -> dict:
    """Old -> new and new -> old deviation between two groups of faces, mm."""
    from OCP.BRep import BRep_Builder
    from OCP.TopoDS import TopoDS_Compound

    def compound(faces: list[Any]) -> Any:
        c = TopoDS_Compound()
        builder = BRep_Builder()
        builder.MakeCompound(c)
        for f in faces:
            builder.Add(c, f)
        return c

    old_c, new_c = compound(old_faces), compound(new_faces)
    return {"old_to_new": max(_max_distance(_face_points(f), new_c) for f in old_faces),
            "new_to_old": max(_max_distance(_face_points(f), old_c) for f in new_faces)}


def _area(shape: Any) -> float:
    from OCP.BRepGProp import BRepGProp
    from OCP.GProp import GProp_GProps

    props = GProp_GProps()
    BRepGProp.SurfaceProperties_s(shape, props)
    return float(abs(props.Mass()))


def _invalid_faces(shape: Any) -> int:
    """How many faces of `shape` are invalid IN CONTEXT: one analyzer over the whole shape, asked
    about each face, so orientation against neighbours and shell/solid membership count (an
    analyzer built on a face alone cannot see them)."""
    from OCP.BRepCheck import BRepCheck_Analyzer
    from OCP.TopAbs import TopAbs_FACE
    from OCP.TopExp import TopExp_Explorer

    analyzer = BRepCheck_Analyzer(shape)
    bad = 0
    explorer = TopExp_Explorer(shape, TopAbs_FACE)
    while explorer.More():
        if not analyzer.IsValid(explorer.Current()):
            bad += 1
        explorer.Next()
    return bad


def _free_edge_count(shape: Any) -> int:
    from OCP.TopAbs import TopAbs_EDGE, TopAbs_FACE
    from OCP.TopExp import TopExp
    from OCP.TopTools import TopTools_IndexedDataMapOfShapeListOfShape

    index = TopTools_IndexedDataMapOfShapeListOfShape()
    TopExp.MapShapesAndAncestors_s(shape, TopAbs_EDGE, TopAbs_FACE, index)
    return sum(1 for i in range(1, index.Extent() + 1) if index.FindFromIndex(i).Size() == 1)


def _is_valid(shape: Any) -> bool:
    from OCP.BRepCheck import BRepCheck_Analyzer

    return bool(BRepCheck_Analyzer(shape).IsValid())


# --- kernel side: replacing ---------------------------------------------------------------------


def _faces_of(old: Any, indices: list[int]) -> list[Any]:
    from OCP.TopoDS import TopoDS

    from meshpipeline.cad.repair_v4 import ws_kernel

    index = ws_kernel._maps(old)["face"]
    return [TopoDS.Face_s(index.FindKey(i)) for i in indices]


def _isolate(wires: list[Any], part: Any) -> list[Any]:
    """Copies of `wires` (geometry shared, topology new) of edges of `part`, each given back the
    pcurves its original has on the faces of `part` (a plain copy loses them, which leaves the
    neighbours invalid on any curved surface).

    WHY. Putting a face on a new carrier means adding pcurves to its boundary edges and raising
    their tolerance to cover the gap - an in-place change to TShapes the OLD part shares. That would
    make `old` stop matching the workspace it was read for (fingerprints include tolerance) and
    would silently change the neighbours. So the work is done on copies; `_match_pairs` then tells
    the ReShape which old edge/vertex each copy stands for, so the neighbours are rebuilt around
    the copies (and reported Modified, truthfully)."""
    from OCP.BRep import BRep_Builder
    from OCP.BRepBuilderAPI import BRepBuilderAPI_Copy
    from OCP.TopoDS import TopoDS, TopoDS_Compound, TopoDS_Iterator

    compound = TopoDS_Compound()
    builder = BRep_Builder()
    builder.MakeCompound(compound)
    for wire in wires:
        builder.Add(compound, wire)
    copier = BRepBuilderAPI_Copy(compound, False, False)
    _restore_pcurves(compound, copier, part)
    copies: list[Any] = []
    children = TopoDS_Iterator(copier.Shape())
    while children.More():
        copies.append(TopoDS.Wire_s(children.Value()))
        children.Next()
    return copies


def _restore_pcurves(compound: Any, copier: Any, part: Any) -> None:
    from OCP.BRep import BRep_Builder, BRep_Tool
    from OCP.TopAbs import TopAbs_EDGE, TopAbs_FACE, TopAbs_FORWARD
    from OCP.TopExp import TopExp
    from OCP.TopoDS import TopoDS
    from OCP.TopTools import TopTools_IndexedDataMapOfShapeListOfShape, TopTools_IndexedMapOfShape

    ancestors = TopTools_IndexedDataMapOfShapeListOfShape()
    TopExp.MapShapesAndAncestors_s(part, TopAbs_EDGE, TopAbs_FACE, ancestors)
    edges = TopTools_IndexedMapOfShape()
    TopExp.MapShapes_s(compound, TopAbs_EDGE, edges)
    builder = BRep_Builder()
    for i in range(1, edges.Extent() + 1):
        original = TopoDS.Edge_s(edges.FindKey(i).Oriented(TopAbs_FORWARD))
        if not ancestors.Contains(original):
            continue
        copy = TopoDS.Edge_s(copier.ModifiedShape(original).Oriented(TopAbs_FORWARD))
        for face in ancestors.FindFromKey(original):
            face = TopoDS.Face_s(face)
            pcurve = BRep_Tool.CurveOnSurface_s(original, face, 0.0, 1.0)
            if pcurve is not None and BRep_Tool.CurveOnSurface_s(copy, face, 0.0, 1.0) is None:
                builder.UpdateEdge(copy, pcurve, face, BRep_Tool.Tolerance_s(original))


def _match_pairs(old_wires: list[Any], candidate: Any) -> list[tuple[Any, Any]]:
    """`(old, new)` for every edge and vertex of `old_wires` that `candidate` still has at the same
    place (to 1e-6 mm of the edge's ends and middle, or of the vertex). Edges whose geometry the op
    changed match nothing: they are legitimately gone, and their replacements are new."""
    from OCP.BRep import BRep_Tool
    from OCP.BRepAdaptor import BRepAdaptor_Curve
    from OCP.TopAbs import TopAbs_EDGE, TopAbs_FORWARD, TopAbs_VERTEX
    from OCP.TopExp import TopExp
    from OCP.TopoDS import TopoDS
    from OCP.TopTools import TopTools_IndexedMapOfShape

    def unique(shapes: list[Any], kind: Any) -> list[Any]:
        index = TopTools_IndexedMapOfShape()
        for shape in shapes:
            TopExp.MapShapes_s(shape, kind, index)
        return [index.FindKey(i).Oriented(TopAbs_FORWARD) for i in range(1, index.Extent() + 1)]

    def edge_key(edge: Any) -> tuple:
        curve = BRepAdaptor_Curve(TopoDS.Edge_s(edge))
        a, b = curve.FirstParameter(), curve.LastParameter()
        ends = sorted([tuple(round(c, 6) for c in (curve.Value(a).X(), curve.Value(a).Y(), curve.Value(a).Z())),
                       tuple(round(c, 6) for c in (curve.Value(b).X(), curve.Value(b).Y(), curve.Value(b).Z()))])
        mid = curve.Value((a + b) / 2)
        return (*ends[0], *ends[1], round(mid.X(), 5), round(mid.Y(), 5), round(mid.Z(), 5))

    def vertex_key(vertex: Any) -> tuple:
        p = BRep_Tool.Pnt_s(TopoDS.Vertex_s(vertex))
        return (round(p.X(), 6), round(p.Y(), 6), round(p.Z(), 6))

    pairs: list[tuple[Any, Any]] = []
    for kind, key in ((TopAbs_EDGE, edge_key), (TopAbs_VERTEX, vertex_key)):
        wanted = {key(shape): shape for shape in unique(old_wires, kind)}
        for shape in unique([candidate], kind):
            original = wanted.get(key(shape))
            if original is not None and not original.IsSame(shape):
                pairs.append((original, shape))
    return pairs


def _face_on_surface(wires: list[Any], surface: Any) -> Any:
    """A face on carrier `surface` bounded by `wires`; ShapeFix_Face adds the pcurves by projection
    and fixes the wire orientation. Pass copies (`_isolate`): this changes the edges it is given."""
    from OCP.BRep import BRep_Builder
    from OCP.ShapeFix import ShapeFix_Face
    from OCP.TopLoc import TopLoc_Location
    from OCP.TopoDS import TopoDS, TopoDS_Face

    builder = BRep_Builder()
    candidate = TopoDS_Face()
    builder.MakeFace(candidate, surface, TopLoc_Location(), 1e-7)
    for wire in wires:
        builder.Add(candidate, wire)
    fixer = ShapeFix_Face(candidate)
    fixer.Perform()  # True only when it CHANGED something; the face's validity is checked by the caller
    return TopoDS.Face_s(fixer.Face())


def _wires_of(face: Any) -> list[Any]:
    from OCP.TopAbs import TopAbs_WIRE
    from OCP.TopExp import TopExp_Explorer
    from OCP.TopoDS import TopoDS

    out: list[Any] = []
    explorer = TopExp_Explorer(face, TopAbs_WIRE)
    while explorer.More():
        out.append(TopoDS.Wire_s(explorer.Current()))
        explorer.Next()
    return out


def _apply_replacement(old: Any, replace: list[tuple[Any, Any]]) -> tuple[Any, Any]:
    """The part with each `(old, new)` sub-shape swapped, and the history that says so. Untouched
    sub-shapes stay the SAME TShapes.

    ReShape.Apply only records history for shapes it walks, and a replaced face's own boundary is
    never walked, so a replacement that the history does not yet mention is added to it here
    (`AddModified`): the op reports its own mapping, as `ws_kernel.history_record` allows."""
    from OCP.ShapeBuild import ShapeBuild_ReShape

    context = ShapeBuild_ReShape()
    for old_shape, new_shape in replace:
        context.Replace(old_shape, new_shape)
    new = context.Apply(old)
    history = context.History()
    for old_shape, new_shape in replace:
        if history.Modified(old_shape).Size() == 0 and not history.IsRemoved(old_shape):
            history.AddModified(old_shape, new_shape)
    return new, history


def _guard(old: Any, new: Any, *, geometry_fixed: bool) -> str | None:
    """Why `new` may not replace `old`, or None. Checked on the WHOLE part, in context: the solid
    count is kept, no new free edge appears, a valid part stays valid and an invalid one gets no
    more invalid faces. When `geometry_fixed` (the op swaps a carrier within a tolerance, so area
    and volume must stand still) Task 11's `split.guard` is run as well (solid count and validity; its area and
    volume noise bounds were removed with the other fidelity gates)."""
    from OCP.BRepCheck import BRepCheck_Analyzer

    from meshpipeline.cad.repair_v4.ops import split

    if geometry_fixed:
        reason = split.guard(old, new)
        if reason is not None:
            return reason
    if split._solid_count(old) != split._solid_count(new):
        return "solid_count: the op changed the number of solids"
    if _free_edge_count(new) > _free_edge_count(old):
        return "cracked: the result has more free edges than the part it replaces"
    if BRepCheck_Analyzer(old).IsValid():
        if not BRepCheck_Analyzer(new).IsValid():
            return "invalidated: the result is not BRepCheck-valid"
    elif _invalid_faces(new) > _invalid_faces(old):
        return "invalidated: the result has more invalid faces than the part it replaces"
    return None


def _first_valid(old: Any, build: Any, *, geometry_fixed: bool = False) -> tuple[Any, str]:
    """`build(flipped)` for flipped False then True; the first result `_guard` accepts, and `""`,
    else `None` and the last reason. A replacement face can come out inside-out against its
    neighbours, which only the whole-part check sees."""
    reason = "no candidate"
    for flipped in (False, True):
        result = build(flipped)
        if result is None:
            continue
        refusal = _guard(old, result[0], geometry_fixed=geometry_fixed)
        if refusal is None:
            return result, ""
        reason = refusal
    return None, reason


def _finish(old: Any, new: Any, history: Any, payload: dict, *, op: str, params: dict, start: float,
            measured: dict, added_faces: list[Any] | None = None) -> dict:
    """Write `new`, then the history record (always via `ws_kernel.history_record`)."""
    from meshpipeline.cad.repair_v4 import ws_kernel

    # The new faces' kernel indices in `new`; history_record proves the written file reads back
    # with the same order, so they are the indices the workspace will see. They ride in `params`
    # (which apply_history carries to the Diff) so `run_kernel_op` can be used as it is.
    in_new = ws_kernel._maps(new)["face"]
    params = {**params, "measured": measured,
              "added_face_indices": [int(in_new.FindIndex(f)) for f in (added_faces or [])]}
    out = payload["out_path"]
    try:
        ws_kernel.write_brep(new, out)
        return ws_kernel.history_record(
            old, new, history, op=op, params=params, old_path=payload["brep_path"], new_path=out,
            quantum=payload["quantum"], seconds=time.perf_counter() - start)
    except BaseException:
        if os.path.exists(out):
            os.unlink(out)
        raise


def _decline(payload: dict, reason: str) -> dict:
    if os.path.exists(payload["out_path"]):
        os.unlink(payload["out_path"])
    return {"declined": reason}


# --- kernel side: the ops -----------------------------------------------------------------------


def _pcurve_print(pcurve: Any, first: float, last: float) -> tuple:
    """A comparable mark of a pcurve segment: its range and seven points along it."""
    marks = [first, last]
    for k in range(7):
        point = pcurve.Value(first + (last - first) * k / 6)
        marks.extend((round(point.X(), 9), round(point.Y(), 9)))
    return tuple(marks)


def _fix_wires_in_uv(face: Any, precision_mm: float) -> tuple[list[Any], bool]:
    """ShapeFix_Wire.FixSelfIntersection on each wire of `face`; (the wires, whether any changed).

    The self-intersection is a property of the wire in the face's UV domain, and ShapeFix_Wire acts
    on it there for a plane but does nothing on a B-spline carrier in this OCCT (observed on flat,
    tilted and curved B-spline faces with the loop clearly detected by ShapeAnalysis_Wire). So the
    wires' pcurves are put on a UV-identity plane, fixed there, and the changed edges are rebuilt
    on the real carrier from the fixed pcurves; an edge the tool left alone is reused as the very
    same TShape, so neighbours keep sharing it.

    UNITS. The proxy lives in UV, so the tool's precision is in UV units. `precision_mm` is turned
    into UV units with the carrier's local scale (mm per unit of u or v, the larger of the two, so
    the 3D meaning of the precision is never looser than asked)."""
    from OCP.BRep import BRep_Tool
    from OCP.BRepAdaptor import BRepAdaptor_Surface
    from OCP.BRepBuilderAPI import (
        BRepBuilderAPI_MakeEdge,
        BRepBuilderAPI_MakeFace,
        BRepBuilderAPI_MakeWire,
    )
    from OCP.BRepLib import BRepLib
    from OCP.Geom import Geom_Plane
    from OCP.gp import gp_Pln
    from OCP.ShapeFix import ShapeFix_Wire
    from OCP.TopAbs import TopAbs_EDGE, TopAbs_WIRE
    from OCP.TopExp import TopExp_Explorer
    from OCP.TopoDS import TopoDS

    surface = BRep_Tool.Surface_s(face)
    adaptor = BRepAdaptor_Surface(face, True)
    u0, u1, v0, v1 = (adaptor.FirstUParameter(), adaptor.LastUParameter(),
                      adaptor.FirstVParameter(), adaptor.LastVParameter())
    um, vm = (u0 + u1) / 2, (v0 + v1) / 2
    du, dv = (u1 - u0) * 1e-3, (v1 - v0) * 1e-3
    if not (du > 0.0 and dv > 0.0):  # snapshot v1, bug32314 face 203: a UV domain with no extent was a ZeroDivisionError
        raise _Declined(f"degenerate_face: the face's UV domain has no extent (u {u0:g}..{u1:g}, v {v0:g}..{v1:g})")
    mm_per_u = adaptor.Value(um + du, vm).Distance(adaptor.Value(um, vm)) / du
    mm_per_v = adaptor.Value(um, vm + dv).Distance(adaptor.Value(um, vm)) / dv
    precision = precision_mm / max(mm_per_u, mm_per_v, 1e-12)
    proxy_plane = Geom_Plane(gp_Pln())
    changed = False
    new_wires: list[Any] = []
    wires = TopExp_Explorer(face, TopAbs_WIRE)
    while wires.More():
        wire = TopoDS.Wire_s(wires.Current())
        wires.Next()
        proxy_of: list[tuple[Any, tuple]] = []  # (real edge, pcurve fingerprint)
        proxy_wire = BRepBuilderAPI_MakeWire()
        edges = TopExp_Explorer(wire, TopAbs_EDGE)
        while edges.More():
            edge = TopoDS.Edge_s(edges.Current())
            edges.Next()
            pcurve = BRep_Tool.CurveOnSurface_s(edge, face, 0.0, 1.0)
            if pcurve is None:
                return _wires_of(face), False
            first, last = BRep_Tool.Range_s(edge, face)
            made = BRepBuilderAPI_MakeEdge(pcurve, proxy_plane, first, last)
            if not made.IsDone():
                raise _Declined("proxy_failed: a pcurve could not be made into an edge")
            proxy = made.Edge()
            if not BRepLib.BuildCurves3d_s(proxy):
                raise _Declined("proxy_failed: no 3D curve for a proxy edge")
            if edge.Orientation() != proxy.Orientation():
                proxy = TopoDS.Edge_s(proxy.Oriented(edge.Orientation()))
            proxy_of.append((edge, _pcurve_print(pcurve, first, last)))
            proxy_wire.Add(proxy)
        if not proxy_wire.IsDone():
            new_wires.append(wire)
            continue
        proxy_face = BRepBuilderAPI_MakeFace(proxy_plane, proxy_wire.Wire(), True).Face()
        fixer = ShapeFix_Wire(proxy_wire.Wire(), proxy_face, precision)
        fixer.ModifyTopologyMode = True
        if not fixer.FixSelfIntersection():
            new_wires.append(wire)
            continue
        changed = True
        rebuilt = BRepBuilderAPI_MakeWire()
        fixed_edges = TopExp_Explorer(fixer.Wire(), TopAbs_EDGE)
        while fixed_edges.More():
            fixed = TopoDS.Edge_s(fixed_edges.Current())
            fixed_edges.Next()
            # The tool may hand back an untouched edge as a new TShape, or change one in place, so
            # "untouched" is judged by the pcurve itself, not by the TShape.
            fixed_curve = BRep_Tool.CurveOnSurface_s(fixed, proxy_face, 0.0, 1.0)
            fixed_print = _pcurve_print(fixed_curve, *BRep_Tool.Range_s(fixed, proxy_face))
            kept = next((real for real, mark in proxy_of if mark == fixed_print), None)
            if kept is not None:
                rebuilt.Add(kept)
                continue
            pcurve = BRep_Tool.CurveOnSurface_s(fixed, proxy_face, 0.0, 1.0)
            first, last = BRep_Tool.Range_s(fixed, proxy_face)
            made = BRepBuilderAPI_MakeEdge(pcurve, surface, first, last)
            if not made.IsDone() or not BRepLib.BuildCurves3d_s(made.Edge()):
                raise _Declined("rebuild_failed: a fixed edge could not be rebuilt on the carrier")
            rebuilt.Add(made.Edge())
        if not rebuilt.IsDone():
            raise _Declined("rebuild_failed: the fixed edges do not form a wire")
        new_wires.append(rebuilt.Wire())
    return new_wires, changed


def _swap_in_shell(old: Any, shell: Any, drop: list[Any], add: Any,
                   pairs: list[tuple[Any, Any]]) -> tuple[Any, Any]:
    """The part with `shell` rebuilt without the faces in `drop` and with `add` in it. The shell's
    other faces are the same TShapes, so ReShape.Apply keeps everything else as it was."""
    from OCP.BRep import BRep_Builder
    from OCP.TopoDS import TopoDS_Iterator, TopoDS_Shell

    builder = BRep_Builder()
    new_shell = TopoDS_Shell()
    builder.MakeShell(new_shell)
    children = TopoDS_Iterator(shell)
    while children.More():
        if not any(children.Value().IsSame(d) for d in drop):
            builder.Add(new_shell, children.Value())
        children.Next()
    builder.Add(new_shell, add)
    return _apply_replacement(old, [(shell, new_shell), *pairs])


def _shell_of(part: Any, faces: list[Any]) -> Any | None:
    """The one shell of `part` that holds every face of `faces`, or None."""
    from OCP.TopAbs import TopAbs_FACE, TopAbs_SHELL
    from OCP.TopExp import TopExp
    from OCP.TopoDS import TopoDS
    from OCP.TopTools import TopTools_IndexedDataMapOfShapeListOfShape

    shells = TopTools_IndexedDataMapOfShapeListOfShape()
    TopExp.MapShapesAndAncestors_s(part, TopAbs_FACE, TopAbs_SHELL, shells)
    found = None
    for face in faces:
        if not shells.Contains(face) or shells.FindFromKey(face).Size() == 0:
            return None
        shell = TopoDS.Shell_s(shells.FindFromKey(face).First())
        if found is not None and not found.IsSame(shell):
            return None
        found = shell
    return found


def _surface_kind(face: Any) -> str:
    from OCP.BRepAdaptor import BRepAdaptor_Surface

    return str(BRepAdaptor_Surface(face, True).GetType()).rsplit("_", 1)[-1]


def _find_in(part: Any, face: Any) -> Any:
    """The face of `part` that is `face` (ReShape.Apply may hand back an equivalent TShape)."""
    from OCP.TopAbs import TopAbs_FACE
    from OCP.TopExp import TopExp_Explorer

    explorer = TopExp_Explorer(part, TopAbs_FACE)
    while explorer.More():
        if explorer.Current().IsSame(face):
            return explorer.Current()
        explorer.Next()
    raise RuntimeError("the added face is not in the new part")


def _edges_in_order(wire: Any) -> list[Any]:
    """The edges of `wire` in wire order (BRepTools_WireExplorer), oriented as in the wire."""
    from OCP.BRepTools import BRepTools_WireExplorer

    out: list[Any] = []
    explorer = BRepTools_WireExplorer(wire)
    while explorer.More():
        out.append(explorer.Current())
        explorer.Next()
    return out


def _outer_edges(face: Any) -> list[Any]:
    from OCP.BRepTools import BRepTools
    from OCP.TopAbs import TopAbs_EDGE
    from OCP.TopExp import TopExp_Explorer
    from OCP.TopoDS import TopoDS

    out: list[Any] = []
    explorer = TopExp_Explorer(BRepTools.OuterWire_s(face), TopAbs_EDGE)
    while explorer.More():
        out.append(TopoDS.Edge_s(explorer.Current()))
        explorer.Next()
    return out


def _fill_face(wire_edges: list[Any], part: Any, continuity: str, tol: float,
               reference: list[Any] | None = None) -> tuple[Any | None, str]:
    """`(face, achieved)`: a face bounded by `wire_edges` (the very edges given; pass copies, see
    `_isolate`) and what was actually achieved. `reference` is the same list as it is in `part`,
    where the neighbouring faces are looked up.

    Planar when the loop lies within `tol` of a plane (BRepLib_FindSurface; `achieved` says
    "planar", and a G1 request is NOT honoured there: nothing constrains the tangent); otherwise
    BRepOffsetAPI_MakeFilling, constrained G1 to the face across each edge where `continuity` is
    "G1" and such a face exists (`achieved` says on how many edges). Achieved continuity is
    reported, not measured: no tangent-angle sampling is done."""
    from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeFace, BRepBuilderAPI_MakeWire
    from OCP.BRepLib import BRepLib_FindSurface
    from OCP.BRepOffsetAPI import BRepOffsetAPI_MakeFilling
    from OCP.GeomAbs import GeomAbs_C0, GeomAbs_G1
    from OCP.ShapeFix import ShapeFix_Face
    from OCP.TopAbs import TopAbs_EDGE, TopAbs_FACE
    from OCP.TopExp import TopExp
    from OCP.TopoDS import TopoDS
    from OCP.TopTools import TopTools_IndexedDataMapOfShapeListOfShape

    wire_maker = BRepBuilderAPI_MakeWire()
    for edge in wire_edges:
        wire_maker.Add(edge)
    if not wire_maker.IsDone():
        return None, "none"
    wire = wire_maker.Wire()
    finder = BRepLib_FindSurface(wire, tol, True)
    if finder.Found():
        made = BRepBuilderAPI_MakeFace(finder.Surface(), wire)
        if not made.IsDone():
            return None, "none"
        fixer = ShapeFix_Face(made.Face())
        fixer.Perform()  # True only when it changed something; validity is checked by the caller
        return TopoDS.Face_s(fixer.Face()), "planar (tangency not constrained)"
    filler = BRepOffsetAPI_MakeFilling()
    neighbours = TopTools_IndexedDataMapOfShapeListOfShape()
    TopExp.MapShapesAndAncestors_s(part, TopAbs_EDGE, TopAbs_FACE, neighbours)
    tangent = 0
    for edge, original in zip(wire_edges, reference or wire_edges, strict=True):
        across = None
        if continuity == "G1" and neighbours.Contains(original):
            across = next((TopoDS.Face_s(f) for f in neighbours.FindFromKey(original)), None)
        if across is not None:
            filler.Add(edge, across, GeomAbs_G1, True)
            tangent += 1
        else:
            filler.Add(edge, GeomAbs_C0, True)
    filler.Build()
    if not filler.IsDone():
        return None, "none"
    achieved = f"G1 on {tangent} of {len(wire_edges)} edges" if continuity == "G1" else "G0"
    return TopoDS.Face_s(filler.Shape()), achieved


@register_kernel_fn("op_fill_loop")
@_declines
def _fill_loop(payload: dict) -> dict:
    """Close a free loop of edges with one new face and add it to the shell that has the hole."""
    from OCP.Bnd import Bnd_Box
    from OCP.BRepBndLib import BRepBndLib
    from OCP.ShapeAnalysis import ShapeAnalysis_FreeBounds
    from OCP.TopAbs import TopAbs_EDGE, TopAbs_FACE
    from OCP.TopExp import TopExp, TopExp_Explorer
    from OCP.TopoDS import TopoDS
    from OCP.TopTools import TopTools_HSequenceOfShape, TopTools_IndexedDataMapOfShapeListOfShape

    from meshpipeline.cad.repair_v4 import ws_kernel

    start = time.perf_counter()
    old = ws_kernel.read_brep(payload["brep_path"])
    diagonal = ws_kernel.part_diagonal(payload, old)  # lane C13
    bound = fidelity_bound(diagonal)
    edge_index = ws_kernel._maps(old)["edge"]
    edges = [TopoDS.Edge_s(edge_index.FindKey(i)) for i in payload["edges"]]

    box = Bnd_Box()
    sequence = TopTools_HSequenceOfShape()
    for edge in edges:
        BRepBndLib.Add_s(edge, box)
        sequence.Append(edge)
    x0, y0, z0, x1, y1, z1 = box.Get()
    span = math.sqrt((x1 - x0) ** 2 + (y1 - y0) ** 2 + (z1 - z0) ** 2)
    # NO span limit (it was 5% of the diagonal): a big loop is filled and its span reported in `measured`

    ancestors = TopTools_IndexedDataMapOfShapeListOfShape()
    TopExp.MapShapesAndAncestors_s(old, TopAbs_EDGE, TopAbs_FACE, ancestors)
    if any(ancestors.FindFromKey(e).Size() != 1 for e in edges):
        return _decline(payload, "not_free: every edge of the loop must border exactly one face")
    connected = TopTools_HSequenceOfShape()
    ShapeAnalysis_FreeBounds.ConnectEdgesToWires_s(sequence, 1e-7, True, connected)
    if connected.Length() != 1 or not connected.Value(1).Closed():
        return _decline(payload, "not_a_loop: the edges do not form exactly one closed wire")
    ordered: list[Any] = []
    explorer = TopExp_Explorer(connected.Value(1), TopAbs_EDGE)
    while explorer.More():
        ordered.append(TopoDS.Edge_s(explorer.Current()))
        explorer.Next()
    neighbour = TopoDS.Face_s(ancestors.FindFromKey(edges[0]).First())
    shell = _shell_of(old, [neighbour])
    if shell is None:
        return _decline(payload, "no_shell: the loop's neighbouring face is not in a shell")

    from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeWire

    loop_wire = BRepBuilderAPI_MakeWire()
    for edge in ordered:
        loop_wire.Add(edge)
    if not loop_wire.IsDone():
        return _decline(payload, "not_a_loop: the edges could not be joined into a wire")
    (copied,) = _isolate([loop_wire.Wire()], old)
    copies = [TopoDS.Edge_s(e) for e in _edges_in_order(copied)]
    originals = [TopoDS.Edge_s(e) for e in _edges_in_order(loop_wire.Wire())]
    chosen: dict = {}

    def build(flipped: bool) -> Any:
        wire_edges = [TopoDS.Edge_s(e.Reversed()) for e in copies] if flipped else copies
        face, achieved = _fill_face(wire_edges, old, payload["continuity"], bound, originals)
        if face is None or not _is_valid(face):
            return None
        chosen["face"], chosen["achieved"] = face, achieved
        return _swap_in_shell(old, shell, [], face, _match_pairs([loop_wire.Wire()], face))

    built, reason = _first_valid(old, build)
    if built is None:
        return _decline(payload, f"no_valid_fill: {reason}")
    new, history = built
    fill = chosen["face"]
    # How far the fill strays from the surfaces around the loop is MEASURED and reported, not gated.
    # (Original faces are untouched by construction and are not re-measured.)
    from OCP.BRep import BRep_Tool

    carriers = [BRep_Tool.Surface_s(TopoDS.Face_s(f)) for e in edges for f in ancestors.FindFromKey(e)]
    worst = max(min(_distance_to_surface([p], c) for c in carriers) for p in _face_points(fill))
    measured = {"fill_to_surroundings": worst, "span_mm": span, "span_ratio": span / diagonal,
                "bound_mm": bound, "fill_area_mm2": _area(fill), "fill_surface": _surface_kind(fill),
                "continuity_requested": payload["continuity"],
                "continuity_achieved": chosen["achieved"]}
    return _finish(old, new, history, payload, op="fill_loop",
                   params={"continuity": payload["continuity"]}, start=start, measured=measured,
                   added_faces=[_find_in(new, fill)])


@register_kernel_fn("op_rebuild_face")
@_declines
def _rebuild_face(payload: dict) -> dict:
    """Remove self-intersections from a face's wires (ShapeFix_Wire.FixSelfIntersection), then, for
    G1 or when the face is still invalid, re-surface it by filling its outer boundary."""

    from meshpipeline.cad.repair_v4 import ws_kernel

    start = time.perf_counter()
    old = ws_kernel.read_brep(payload["brep_path"])
    bound = fidelity_bound(ws_kernel.part_diagonal(payload, old))  # lane C13
    (face,) = _faces_of(old, payload["faces"])
    was_valid = _is_valid(face)
    from OCP.BRep import BRep_Tool

    wires, changed = _fix_wires_in_uv(face, min(bound, 1e-3))
    candidate = _face_on_surface(_isolate(wires, old), BRep_Tool.Surface_s(face))
    refilled, achieved = False, "carrier kept (G0 boundary, surface untouched)"
    if payload["continuity"] == "G1" or not _is_valid(candidate):
        same_edges = len(_outer_edges(candidate)) == len(_outer_edges(face))
        filled, how = _fill_face(_outer_edges(candidate), old, payload["continuity"], bound,
                                 _outer_edges(face) if same_edges else None)
        if filled is not None and _is_valid(filled):
            candidate, refilled, achieved = filled, True, how
    if not changed and not refilled and was_valid:
        return _decline(payload, "no_change: the face is valid and has no self-intersection")
    if not _is_valid(candidate):
        return _decline(payload, "unfixable: the face is still invalid after FixSelfIntersection "
                                 "and a re-fill; nothing was changed")

    def build(flipped: bool) -> Any:
        return _apply_replacement(
            old, [(face, candidate.Reversed() if flipped else candidate),
                  *_match_pairs(_wires_of(face), candidate)])

    built, reason = _first_valid(old, build)
    if built is None:
        return _decline(payload, f"no_valid_rebuild: {reason}")
    new, history = built
    area_old, area_new = _area(face), _area(candidate)
    measured: dict = {"bound_mm": bound, "wire_fixed": changed, "refilled": refilled,
                      "area_old_mm2": area_old, "area_new_mm2": area_new,
                      "area_change_ratio": abs(area_new - area_old) / max(area_old, 1e-12),
                      "continuity_requested": payload["continuity"],
                      "continuity_achieved": achieved}
    # MEASURED, not gated: the trim is what a rebuild moves - how far the OLD boundary sat from the new face. (The
    # distance of the new face to the carrier it was built on is ~0 by construction and is only reported.)
    measured["boundary_shift"] = _max_distance(_edge_points(face), candidate)
    if refilled:
        measured.update(_two_way([face], [candidate]))
    else:
        measured["carrier_distance_reported"] = _distance_to_surface(
            _face_points(candidate), BRep_Tool.Surface_s(face))
    return _finish(old, new, history, payload, op="rebuild_face",
                   params={"continuity": payload["continuity"]}, start=start, measured=measured)


def _replace_surfaces(payload: dict, *, op: str, params: dict, start: float, old: Any, bound: float,
                      surfaces: list[Any], faces: list[Any], extra: dict) -> dict:
    """Swap each of `faces` for the same boundary on its new carrier, measure, and write."""
    # ONE isolation for all the faces: adjacent faces share edges, and two independent copies of a
    # shared edge would crack the seam between them.
    wires_by_face = [_wires_of(f) for f in faces]
    flat = _isolate([w for ws in wires_by_face for w in ws], old)
    candidates, taken = [], 0
    for ws, surface in zip(wires_by_face, surfaces, strict=True):
        candidates.append(_face_on_surface(flat[taken:taken + len(ws)], surface))
        taken += len(ws)
    pairs: list[tuple[Any, Any]] = []
    for f, c in zip(faces, candidates, strict=True):
        pairs.extend(p for p in _match_pairs(_wires_of(f), c) if not any(p[0].IsSame(q[0]) for q in pairs))
    if not all(_is_valid(c) for c in candidates):
        return _decline(payload, f"no_valid_face: the new carrier does not give a valid face ({op})")

    def build(flipped: bool) -> Any:
        return _apply_replacement(
            old, [*((f, c.Reversed() if flipped else c) for f, c in zip(faces, candidates, strict=True)),
                  *pairs])

    built, reason = _first_valid(old, build, geometry_fixed=True)
    if built is None:
        return _decline(payload, f"no_valid_replacement: {reason}")
    new, history = built
    area_old, area_new = sum(_area(f) for f in faces), sum(_area(c) for c in candidates)
    measured = {"bound_mm": bound, **extra, **_two_way(faces, candidates),
                "area_change_ratio": abs(area_new - area_old) / max(area_old, 1e-12)}
    return _finish(old, new, history, payload, op=op, params=params, start=start, measured=measured)


@register_kernel_fn("op_reapproximate")
@_declines
def _reapproximate(payload: dict) -> dict:
    """Re-approximate the faces' surfaces within `tol_mm` at degree <= `max_degree`
    (ShapeCustom_BSplineRestriction). Only the carrier is taken from the restricted copy; the
    faces keep their own edges."""
    from OCP.BRep import BRep_Tool
    from OCP.BRepTools import BRepTools_Modifier
    from OCP.GeomAbs import GeomAbs_C1
    from OCP.ShapeCustom import ShapeCustom_BSplineRestriction
    from OCP.TopoDS import TopoDS

    from meshpipeline.cad.repair_v4 import ws_kernel

    start = time.perf_counter()
    old = ws_kernel.read_brep(payload["brep_path"])
    bound = fidelity_bound(ws_kernel.part_diagonal(payload, old))  # lane C13
    faces = _faces_of(old, payload["faces"])
    surfaces: list[Any] = []
    for face in faces:
        restriction = ShapeCustom_BSplineRestriction(
            True, False, False, payload["tol_mm"], payload["tol_mm"], GeomAbs_C1, GeomAbs_C1,
            payload["max_degree"], 100, True, True)
        modifier = BRepTools_Modifier(face, restriction)
        if not modifier.IsDone():
            return _decline(payload, "approximation_failed: the restriction did not complete")
        surface = BRep_Tool.Surface_s(TopoDS.Face_s(modifier.ModifiedShape(face)))
        if surface.IsSame(BRep_Tool.Surface_s(face)) if hasattr(surface, "IsSame") else False:
            return _decline(payload, "no_change: the surface already meets the degree and tolerance")
        degree = max(getattr(surface, "UDegree", lambda: 0)(), getattr(surface, "VDegree", lambda: 0)())
        if degree > payload["max_degree"]:
            return _decline(payload, f"degree_not_met: the approximation has degree {degree} "
                                     f"> max_degree {payload['max_degree']}")
        surfaces.append(surface)
    return _replace_surfaces(
        payload, op="reapproximate", params={"tol_mm": payload["tol_mm"],
                                              "max_degree": payload["max_degree"]},
        start=start, old=old, bound=bound, surfaces=surfaces, faces=faces, extra={})


@register_kernel_fn("op_to_canonical")
@_declines
def _to_canonical(payload: dict) -> dict:
    """Replace each face's carrier by the plane, cylinder, cone or sphere it lies within `tol_mm` of
    (ShapeAnalysis_CanonicalRecognition); a face that is none of them declines the whole call."""
    from OCP.Geom import Geom_ConicalSurface, Geom_CylindricalSurface, Geom_Plane, Geom_SphericalSurface
    from OCP.gp import gp_Cone, gp_Cylinder, gp_Pln, gp_Sphere
    from OCP.ShapeAnalysis import ShapeAnalysis_CanonicalRecognition

    from meshpipeline.cad.repair_v4 import ws_kernel

    start = time.perf_counter()
    old = ws_kernel.read_brep(payload["brep_path"])
    bound = fidelity_bound(ws_kernel.part_diagonal(payload, old))  # lane C13
    faces = _faces_of(old, payload["faces"])
    tol = payload["tol_mm"]
    surfaces: list[Any] = []
    kinds: list[str] = []
    gaps: list[float] = []
    for face in faces:
        if _surface_kind(face) in ("Plane", "Cylinder", "Cone", "Sphere"):
            return _decline(payload, f"no_change: a face is already a {_surface_kind(face)}")
        recogniser = ShapeAnalysis_CanonicalRecognition(face)
        plane, cylinder, cone, sphere = gp_Pln(), gp_Cylinder(), gp_Cone(), gp_Sphere()
        if recogniser.IsPlane(tol, plane):
            surfaces.append(Geom_Plane(plane))
            kinds.append("plane")
        elif recogniser.IsCylinder(tol, cylinder):
            surfaces.append(Geom_CylindricalSurface(cylinder))
            kinds.append("cylinder")
        elif recogniser.IsCone(tol, cone):
            surfaces.append(Geom_ConicalSurface(cone))
            kinds.append("cone")
        elif recogniser.IsSphere(tol, sphere):
            surfaces.append(Geom_SphericalSurface(sphere))
            kinds.append("sphere")
        else:
            return _decline(payload, f"not_canonical: a face is not a plane, cylinder, cone or "
                                     f"sphere within {tol:.6g} mm")
        gaps.append(float(recogniser.GetGap()))
    return _replace_surfaces(
        payload, op="to_canonical", params={"tol_mm": tol}, start=start, old=old, bound=bound,
        surfaces=surfaces, faces=faces, extra={"canonical": kinds, "recognition_gap_mm": gaps})


def _fit_cluster_surface(faces: list[Any], tol: float) -> Any | None:
    """A B-spline surface through a grid of points of the cluster (GeomAPI_PointsToBSplineSurface).
    The grid is laid on the cluster's best-fit plane and each node is projected onto the carrier of
    the cluster face nearest to it, so the grid may reach a little past a ragged outline. The
    boundary is not fitted: the new face keeps the cluster's own boundary edges, and the
    two-way deviation measured afterwards is what says whether the fit is good enough."""
    import numpy as np
    from OCP.BRep import BRep_Tool
    from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeVertex
    from OCP.BRepExtrema import BRepExtrema_DistShapeShape
    from OCP.GeomAbs import GeomAbs_C2
    from OCP.GeomAPI import GeomAPI_PointsToBSplineSurface, GeomAPI_ProjectPointOnSurf
    from OCP.gp import gp_Pnt
    from OCP.TColgp import TColgp_Array2OfPnt

    samples = np.array([[p.X(), p.Y(), p.Z()] for f in faces for p in _face_points(f)])
    centre = samples.mean(axis=0)
    normal = np.linalg.svd(samples - centre, full_matrices=False)[2][2]
    # In-plane axes: SVD gives an arbitrary rotation for a square cluster, so take the global axis
    # lying most in the plane as `u` and complete the frame.
    seed = np.eye(3)[int(np.argmin(np.abs(normal)))]
    u_axis = seed - seed.dot(normal) * normal
    u_axis /= np.linalg.norm(u_axis)
    axes = np.array([u_axis, np.cross(normal, u_axis), normal])
    local = (samples - centre) @ axes.T
    (u0, v0), (u1, v1) = local[:, :2].min(axis=0), local[:, :2].max(axis=0)
    n = GRID_NODES
    grid = TColgp_Array2OfPnt(1, n, 1, n)
    for i in range(n):
        for j in range(n):
            u, v = u0 + (u1 - u0) * i / (n - 1), v0 + (v1 - v0) * j / (n - 1)
            node = gp_Pnt(*(centre + u * axes[0] + v * axes[1]))
            vertex = BRepBuilderAPI_MakeVertex(node).Vertex()
            nearest = min(faces, key=lambda f: BRepExtrema_DistShapeShape(vertex, f).Value())
            projection = GeomAPI_ProjectPointOnSurf(node, BRep_Tool.Surface_s(nearest))
            if projection.NbPoints() == 0:
                return None
            grid.SetValue(i + 1, j + 1, projection.NearestPoint())
    return GeomAPI_PointsToBSplineSurface(grid, 3, 5, GeomAbs_C2, tol / 2).Surface()


@register_kernel_fn("op_refit_cluster")
@_declines
def _refit_cluster(payload: dict) -> dict:
    """Replace a cluster of adjacent faces by ONE face on a fitted B-spline surface, bounded by the
    cluster's outline. The new face is declared added and the cluster declared removed.

    LIMITS (all decline, none guess): the cluster must be at least two faces in ONE shell with ONE
    closed outline (no holes, no pinch points); seam/closed faces are not supported; the grid fit
    is laid on the cluster's best-fit plane and is not boundary-constrained, so a cluster that is
    not a graph over a plane, or a ragged outline, shows up as a two-way deviation over `tol_mm`
    and is declined. The cluster's labels are NOT inherited: the new face is a fresh id."""
    from OCP.BRep import BRep_Builder
    from OCP.ShapeAnalysis import ShapeAnalysis_FreeBounds
    from OCP.TopAbs import TopAbs_EDGE, TopAbs_FACE
    from OCP.TopExp import TopExp
    from OCP.TopoDS import TopoDS, TopoDS_Compound
    from OCP.TopTools import TopTools_HSequenceOfShape, TopTools_IndexedDataMapOfShapeListOfShape

    from meshpipeline.cad.repair_v4 import ws_kernel

    start = time.perf_counter()
    old = ws_kernel.read_brep(payload["brep_path"])
    bound = fidelity_bound(ws_kernel.part_diagonal(payload, old))  # lane C13
    faces = _faces_of(old, payload["faces"])
    if len(faces) < 2:
        return _decline(payload, "not_a_cluster: refit_cluster needs at least two faces")
    shell = _shell_of(old, faces)
    if shell is None:
        return _decline(payload, "no_shell: the cluster's faces are not all in one shell")

    inside = TopTools_IndexedDataMapOfShapeListOfShape()
    builder = BRep_Builder()
    cluster = TopoDS_Compound()
    builder.MakeCompound(cluster)
    for f in faces:
        builder.Add(cluster, f)
    TopExp.MapShapesAndAncestors_s(cluster, TopAbs_EDGE, TopAbs_FACE, inside)
    outline = TopTools_HSequenceOfShape()
    for i in range(1, inside.Extent() + 1):
        if inside.FindFromIndex(i).Size() == 1:
            outline.Append(inside.FindKey(i))
    wires = TopTools_HSequenceOfShape()
    ShapeAnalysis_FreeBounds.ConnectEdgesToWires_s(outline, 1e-7, True, wires)
    if wires.Length() != 1 or not wires.Value(1).Closed():
        return _decline(payload, "no_single_outline: the cluster must have one closed outline "
                                 "(no holes, no pinch points)")
    surface = _fit_cluster_surface(faces, payload["tol_mm"])
    if surface is None:
        return _decline(payload, "fit_failed: no surface could be fitted to the cluster")

    outline_wire = TopoDS.Wire_s(wires.Value(1))
    replacement = _face_on_surface(_isolate([outline_wire], old), surface)
    pairs = _match_pairs([outline_wire], replacement)
    if not _is_valid(replacement):
        return _decline(payload, "no_valid_face: the fitted face is not valid")

    chosen: dict = {}

    def build(flipped: bool) -> Any:
        face = TopoDS.Face_s(replacement.Reversed()) if flipped else replacement
        chosen["face"] = face
        return _swap_in_shell(old, shell, faces, face, pairs)

    built, reason = _first_valid(old, build)
    if built is None:
        return _decline(payload, f"no_valid_replacement: {reason}")
    new, history = built
    added = chosen["face"]
    area_old, area_new = sum(_area(f) for f in faces), _area(added)
    measured = {"bound_mm": bound, **_two_way(faces, [added]),
                "area_change_ratio": abs(area_new - area_old) / max(area_old, 1e-12),
                "cluster_faces": len(faces), "fit_surface": _surface_kind(added)}
    return _finish(old, new, history, payload, op="refit_cluster",
                   params={"tol_mm": payload["tol_mm"]}, start=start, measured=measured,
                   added_faces=[_find_in(new, added)])


# --- parent side --------------------------------------------------------------------------------

_CONTINUITY = {"type": "string", "enum": ["G0", "G1"]}
#: a fitting tolerance in mm; NOT capped by the fidelity bound any more (owner decision 2026-11-24)
_TOL = {"type": "number", "minimum": 1e-6}


class _FaceOp:
    name: str
    kernel_fn: str
    params_schema: dict
    #: site kinds this op is a candidate for (a ranking hint; the search decides).
    site_kinds: frozenset[str]
    entity_prefix: str
    #: "faces" or "edges": the payload key under which the targets' kernel indices go.
    payload_key: str = "faces"
    declares_added = False
    declares_removed = False

    def applies_to(self, site: Site) -> bool:
        # B19: the site's KIND no longer forbids a family - `site_kinds` is now only the preference the det sweep
        # orders by (`search._PREFERRED_FAMILIES`). What an op still needs is an entity of its own sort.
        return any(e.startswith(self.entity_prefix) for e in site.entities)

    def apply(self, pool: WorkerPool, ws: Workspace, targets: list[StableId], params: dict, *,
              timeout_s: float) -> tuple[Workspace, Diff]:
        errors = validate_call(self.name, targets, params, ws)
        if errors:  # an op raises only OpDeclined / OpFailed
            raise OpDeclined(f"invalid_params: invalid call to {self.name}: " + "; ".join(errors))
        new, diff = run_kernel_op(
            pool, ws, kernel_fn=self.kernel_fn, op=self.name, timeout_s=timeout_s,
            payload={**params, self.payload_key: [ws.indices[t] for t in targets]})
        try:
            added = diff.params.pop("added_face_indices", [])
            face_at = {index: sid for sid, index in new.indices.items() if sid.startswith("F")}
            if self.declares_added:
                diff.declared_added = [face_at[i] for i in added]
            if self.declares_removed:
                diff.declared_removed = list(targets)
        except KeyError as exc:
            raise OpFailed("error", f"the kernel's added face {exc} is not in the new workspace") from exc
        return new, diff


def _schema(kind: str, minimum: int, maximum: int | None, properties: dict) -> dict:
    return {"type": "object", "properties": properties, "required": sorted(properties),
            "additionalProperties": False,
            "x-targets": {"kind": kind, "min": minimum, "max": maximum}}


class RebuildFace(_FaceOp):
    name = "rebuild_face"
    kernel_fn = "op_rebuild_face"
    params_schema = {**_schema("face", 1, 1, {"continuity": _CONTINUITY}),
                     "description": "Fix self-intersections of the face's wires; how far the boundary "
                                    "moved is measured and reported. G1 is honoured only where a "
                                    "neighbouring face constrains the re-fill; see "
                                    "Diff.params['measured']['continuity_achieved']."}
    site_kinds = frozenset({"invalid", "self_overlap", "intersection", "periodic", "pole"})
    entity_prefix = "F"


class FillLoop(_FaceOp):
    """Declares the face it adds. Any loop size: the span is measured and reported, not limited."""

    name = "fill_loop"
    kernel_fn = "op_fill_loop"
    params_schema = {**_schema("edge", 1, None, {"continuity": _CONTINUITY}),
                     "description": "Close one free loop with one declared face, of any size (the span "
                                    "is reported). A planar fill ignores G1; achieved "
                                    "continuity is in Diff.params['measured']."}
    site_kinds = frozenset({"open"})
    entity_prefix = "E"
    payload_key = "edges"
    declares_added = True


class Reapproximate(_FaceOp):
    name = "reapproximate"
    kernel_fn = "op_reapproximate"
    params_schema = _schema("face", 1, None, {
        "tol_mm": _TOL, "max_degree": {"type": "integer", "minimum": 3, "maximum": 9}})
    site_kinds = frozenset({"self_overlap", "pole", "periodic", "proximity"})
    entity_prefix = "F"


class ToCanonical(_FaceOp):
    name = "to_canonical"
    kernel_fn = "op_to_canonical"
    params_schema = _schema("face", 1, None, {"tol_mm": _TOL})
    site_kinds = frozenset({"proximity", "sliver", "invalid"})
    entity_prefix = "F"


class RefitCluster(_FaceOp):
    """Declares the one face it adds and the cluster it removes. Limits: one shell, one closed
    outline, no seam faces, labels of the cluster not inherited (see `_refit_cluster`)."""

    name = "refit_cluster"
    kernel_fn = "op_refit_cluster"
    params_schema = {**_schema("face", 2, None, {"tol_mm": _TOL}),
                     "description": "Refit 2+ adjacent faces of ONE shell with one closed outline "
                                    "into one B-spline face. No seam faces; cluster labels are not "
                                    "inherited; the two-way deviation is measured and reported."}
    site_kinds = frozenset({"sliver", "overlap", "self_overlap"})
    entity_prefix = "F"
    declares_added = True
    declares_removed = True


OPS = (RebuildFace(), FillLoop(), Reapproximate(), ToCanonical(), RefitCluster())
