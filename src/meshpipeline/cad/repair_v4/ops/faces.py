# Responsibility: The face-rebuild family - ops that REPLACE geometry: rebuild a face, fill a loop,
#                 re-approximate, snap to a canonical surface, and refit a cluster into one face.
# Owns: five `Op`s (`OPS`) and their kernel functions (`op_rebuild_face` ... `op_refit_cluster`).
# Boundaries: kernel functions run in a worker (OCP imported lazily); the `Op` classes run in the
#             parent and only move ids and paths. Choosing WHICH faces is the caller's (sites, Task
#             17; search, Task 18). Registry wiring is integration's: this module only exposes `OPS`.
"""Face rebuild ops - the most geometry-altering ops in the library, so every one of them measures
what it did and refuses what it cannot keep inside the fidelity bound.

THE BOUND. `bound = min(0.1 mm, 0.001 x diagonal)` of the part the op reads. `tol_mm` parameters are
capped by it (the op DECLINES a larger one rather than clamping it silently), and the kernel
function measures the deviation it actually caused and declines if that is over the bound too.
Declining returns `{"declined": reason}` and writes nothing.

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
import uuid
from pathlib import Path
from typing import Any

from meshpipeline.cad.repair_v4.kernel_calls import register as register_kernel_fn
from meshpipeline.cad.repair_v4.types import Site, StableId
from meshpipeline.cad.repair_v4.worker_pool import WorkerPool
from meshpipeline.cad.repair_v4.workspace import Diff, Workspace, apply_history

try:  # Task 11's contract types; until it is merged the same two names are defined here.
    from meshpipeline.cad.repair_v4.ops import OpDeclined, OpFailed  # type: ignore[attr-defined]
except ImportError:
    class OpDeclined(Exception):  # type: ignore[no-redef]
        """The op ran and declined: nothing was changed. `reason` is for the proposer."""

        def __init__(self, reason: str) -> None:
            super().__init__(reason)
            self.reason = reason

    class OpFailed(Exception):  # type: ignore[no-redef]
        """The worker did not answer: `status` is the pool's ("error", "timeout" or "crashed")."""

        def __init__(self, status: str, message: str) -> None:
            super().__init__(f"{status}: {message}")
            self.status = status
            self.message = message


#: Absolute ceiling of the fidelity bound, mm.
BOUND_MAX_MM = 0.1
#: Relative ceiling: the bound is at most this fraction of the part's bounding-box diagonal.
BOUND_DIAGONAL_RATIO = 0.001
#: `fill_loop` refuses a loop whose bounding box diagonal exceeds this fraction of the part's.
MAX_FILL_SPAN_RATIO = 0.05
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
    """How many faces of `shape` BRepCheck_Analyzer rejects (a valid shape has none)."""
    from OCP.BRepCheck import BRepCheck_Analyzer
    from OCP.TopAbs import TopAbs_FACE
    from OCP.TopExp import TopExp_Explorer

    bad = 0
    explorer = TopExp_Explorer(shape, TopAbs_FACE)
    while explorer.More():
        if not BRepCheck_Analyzer(explorer.Current()).IsValid():
            bad += 1
        explorer.Next()
    return bad


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
    fixer.Perform()
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


def _first_valid(old: Any, build: Any) -> Any:
    """`build(flipped)` for flipped False then True, the first whose part is no worse than `old`
    (a replacement face can come out inside-out against its neighbours); None if neither is."""
    baseline = _invalid_faces(old)
    for flipped in (False, True):
        result = build(flipped)
        if result is not None and _invalid_faces(result[0]) <= baseline:
            return result
    return None


def _finish(old: Any, new: Any, history: Any, payload: dict, *, op: str, params: dict, start: float,
            measured: dict, added_faces: list[Any] | None = None) -> dict:
    """Write `new`, then the history record (always via `ws_kernel.history_record`)."""
    from meshpipeline.cad.repair_v4 import ws_kernel

    ws_kernel.write_brep(new, payload["out_path"])
    record = ws_kernel.history_record(
        old, new, history, op=op, params=params, old_path=payload["brep_path"],
        new_path=payload["out_path"], quantum=payload["quantum"],
        seconds=time.perf_counter() - start)
    # The new faces' kernel indices in `new`; history_record has just proved the written file reads
    # back with the same order, so they are the indices the workspace will see.
    in_new = ws_kernel._maps(new)["face"]
    record["added_face_indices"] = [int(in_new.FindIndex(f)) for f in (added_faces or [])]
    record["measured"] = measured
    return record


def _decline(payload: dict, reason: str) -> dict:
    if os.path.exists(payload["out_path"]):
        os.unlink(payload["out_path"])
    return {"declined": reason}


def _over_bound(measured: dict, bound: float, label: str = "deviation") -> str | None:
    keys = [k for k in ("old_to_new", "new_to_old", "to_carrier") if k in measured]
    worst_key = max(keys, key=lambda k: measured[k], default=None)
    if worst_key is not None and measured[worst_key] > bound:
        return (f"over_bound: {label} {measured[worst_key]:.6g} mm ({worst_key}) exceeds the "
                f"fidelity bound {bound:.6g} mm; nothing was changed")
    return None


# --- kernel side: the ops -----------------------------------------------------------------------


def _pcurve_print(pcurve: Any, first: float, last: float) -> tuple:
    """A comparable mark of a pcurve segment: its range and seven points along it."""
    marks = [first, last]
    for k in range(7):
        point = pcurve.Value(first + (last - first) * k / 6)
        marks.extend((round(point.X(), 9), round(point.Y(), 9)))
    return tuple(marks)


def _fix_wires_in_uv(face: Any, precision: float) -> tuple[list[Any], bool]:
    """ShapeFix_Wire.FixSelfIntersection on each wire of `face`; (the wires, whether any changed).

    The self-intersection is a property of the wire in the face's UV domain, and ShapeFix_Wire acts
    on it there for a plane but does nothing on a B-spline carrier in this OCCT (observed on flat,
    tilted and curved B-spline faces with the loop clearly detected by ShapeAnalysis_Wire). So the
    wires' pcurves are put on a UV-identity plane, fixed there, and the changed edges are rebuilt
    on the real carrier from the fixed pcurves; an edge the tool left alone is reused as the very
    same TShape, so neighbours keep sharing it."""
    from OCP.BRep import BRep_Tool
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
            proxy = BRepBuilderAPI_MakeEdge(pcurve, proxy_plane, first, last).Edge()
            BRepLib.BuildCurves3d_s(proxy)
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
            edge = BRepBuilderAPI_MakeEdge(pcurve, surface, first, last).Edge()
            BRepLib.BuildCurves3d_s(edge)
            rebuilt.Add(edge)
        new_wires.append(rebuilt.Wire() if rebuilt.IsDone() else wire)
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
               reference: list[Any] | None = None) -> Any | None:
    """A face bounded by `wire_edges` (the very edges given; pass copies, see `_isolate`). `reference`
    is the same list as it is in `part`, where the neighbouring faces are looked up. Planar when the loop lies within `tol` of a
    plane (BRepLib_FindSurface); otherwise BRepOffsetAPI_MakeFilling, constrained G1 to the face on
    the other side of each edge when `continuity` is "G1" and such a face exists."""
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
        return None
    wire = wire_maker.Wire()
    finder = BRepLib_FindSurface(wire, tol, True)
    if finder.Found():
        face = BRepBuilderAPI_MakeFace(finder.Surface(), wire).Face()
        fixer = ShapeFix_Face(face)
        fixer.Perform()
        return TopoDS.Face_s(fixer.Face())
    filler = BRepOffsetAPI_MakeFilling()
    neighbours = TopTools_IndexedDataMapOfShapeListOfShape()
    TopExp.MapShapesAndAncestors_s(part, TopAbs_EDGE, TopAbs_FACE, neighbours)
    for edge, original in zip(wire_edges, reference or wire_edges, strict=True):
        across = None
        if continuity == "G1" and neighbours.Contains(original):
            across = next((TopoDS.Face_s(f) for f in neighbours.FindFromKey(original)), None)
        if across is not None:
            filler.Add(edge, across, GeomAbs_G1, True)
        else:
            filler.Add(edge, GeomAbs_C0, True)
    filler.Build()
    return TopoDS.Face_s(filler.Shape()) if filler.IsDone() else None


@register_kernel_fn("op_fill_loop")
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
    diagonal = ws_kernel.diagonal_of(old)
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
    if span > MAX_FILL_SPAN_RATIO * diagonal:
        return _decline(payload, (
            f"too_large: the loop spans {span:.4g} mm, {100 * span / diagonal:.1f}% of the part's "
            f"diagonal ({diagonal:.4g} mm); fill_loop refuses beyond {100 * MAX_FILL_SPAN_RATIO:g}%"))

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
    (copied,) = _isolate([loop_wire.Wire()], old)
    copies = [TopoDS.Edge_s(e) for e in _edges_in_order(copied)]
    originals = [TopoDS.Edge_s(e) for e in _edges_in_order(loop_wire.Wire())]
    chosen: dict = {}

    def build(flipped: bool) -> Any:
        wire_edges = [TopoDS.Edge_s(e.Reversed()) for e in copies] if flipped else copies
        face = _fill_face(wire_edges, old, payload["continuity"], bound, originals)
        if face is None or not _is_valid(face):
            return None
        chosen["face"] = face
        return _swap_in_shell(old, shell, [], face, _match_pairs([loop_wire.Wire()], face))

    built = _first_valid(old, build)
    if built is None:
        return _decline(payload, "no_valid_fill: no fill of this loop keeps the part valid")
    new, history = built
    fill = chosen["face"]
    # The added face is not measured against anything (nothing was there); what must hold is that
    # no ORIGINAL face moved, which is the one-sided original -> repaired distance.
    n_faces = ws_kernel._maps(old)["face"].Extent()
    measured = {"old_to_new": max(_max_distance(_face_points(f), new)
                                  for f in _faces_of(old, list(range(1, n_faces + 1)))),
                "span_mm": span, "span_ratio": span / diagonal, "bound_mm": bound,
                "fill_area_mm2": _area(fill), "fill_surface": _surface_kind(fill)}
    problem = _over_bound(measured, bound)
    if problem:
        return _decline(payload, problem)
    return _finish(old, new, history, payload, op="fill_loop",
                   params={"continuity": payload["continuity"]}, start=start, measured=measured,
                   added_faces=[_find_in(new, fill)])


@register_kernel_fn("op_rebuild_face")
def _rebuild_face(payload: dict) -> dict:
    """Remove self-intersections from a face's wires (ShapeFix_Wire.FixSelfIntersection), then, for
    G1 or when the face is still invalid, re-surface it by filling its outer boundary."""

    from meshpipeline.cad.repair_v4 import ws_kernel

    start = time.perf_counter()
    old = ws_kernel.read_brep(payload["brep_path"])
    bound = fidelity_bound(ws_kernel.diagonal_of(old))
    (face,) = _faces_of(old, payload["faces"])
    was_valid = _is_valid(face)
    from OCP.BRep import BRep_Tool

    wires, changed = _fix_wires_in_uv(face, min(bound, 1e-3))
    candidate = _face_on_surface(_isolate(wires, old), BRep_Tool.Surface_s(face))
    refilled = False
    if payload["continuity"] == "G1" or not _is_valid(candidate):
        filled = _fill_face(_outer_edges(candidate), old, payload["continuity"], bound,
                            _outer_edges(face) if len(_outer_edges(candidate)) == len(_outer_edges(face))
                            else None)
        if filled is not None and _is_valid(filled):
            candidate, refilled = filled, True
    if not changed and not refilled and was_valid:
        return _decline(payload, "no_change: the face is valid and has no self-intersection")
    if not _is_valid(candidate):
        return _decline(payload, "unfixable: the face is still invalid after FixSelfIntersection "
                                 "and a re-fill; nothing was changed")

    def build(flipped: bool) -> Any:
        return _apply_replacement(
            old, [(face, candidate.Reversed() if flipped else candidate),
                  *_match_pairs(_wires_of(face), candidate)])

    built = _first_valid(old, build)
    if built is None:
        return _decline(payload, "no_valid_rebuild: the rebuilt face leaves the part invalid")
    new, history = built
    points = _face_points(candidate)
    measured: dict = {"bound_mm": bound, "wire_fixed": changed, "refilled": refilled,
                      "area_old_mm2": _area(face), "area_new_mm2": _area(candidate)}
    if refilled:
        measured.update(_two_way([face], [candidate]))
    else:  # same carrier: positions can only differ along the (unchanged) surface
        measured["to_carrier"] = _distance_to_surface(points, BRep_Tool.Surface_s(face))
        # Not gated (removing a loop does move the trim by the loop's size); reported so the caller
        # can see how far the old boundary sat from the new face.
        measured["boundary_shift"] = _max_distance(_edge_points(face), candidate)
    problem = _over_bound(measured, bound)
    if problem:
        return _decline(payload, problem)
    return _finish(old, new, history, payload, op="rebuild_face",
                   params={"continuity": payload["continuity"]}, start=start, measured=measured)


def _replace_surfaces(payload: dict, *, op: str, params: dict, start: float, old: Any, bound: float,
                      surfaces: list[Any], faces: list[Any], extra: dict) -> dict:
    """Swap each of `faces` for the same boundary on its new carrier, measure, and write."""
    candidates = [_face_on_surface(_isolate(_wires_of(f), old), s) for f, s in zip(faces, surfaces, strict=True)]
    pairs = [p for f, c in zip(faces, candidates, strict=True) for p in _match_pairs(_wires_of(f), c)]
    if not all(_is_valid(c) for c in candidates):
        return _decline(payload, f"no_valid_face: the new carrier does not give a valid face ({op})")

    def build(flipped: bool) -> Any:
        return _apply_replacement(
            old, [*((f, c.Reversed() if flipped else c) for f, c in zip(faces, candidates, strict=True)),
                  *pairs])

    built = _first_valid(old, build)
    if built is None:
        return _decline(payload, f"no_valid_replacement: {op} leaves the part invalid")
    new, history = built
    measured = {"bound_mm": bound, **extra, **_two_way(faces, candidates)}
    problem = _over_bound(measured, min(bound, payload["tol_mm"]), "deviation vs tol_mm")
    if problem:
        return _decline(payload, problem)
    return _finish(old, new, history, payload, op=op, params=params, start=start, measured=measured)


def _tolerance_gate(payload: dict, bound: float) -> str | None:
    if payload["tol_mm"] > bound:
        return (f"over_bound: tol_mm {payload['tol_mm']:.6g} exceeds the fidelity bound "
                f"{bound:.6g} mm (min(0.1, 0.001 x diagonal))")
    return None


@register_kernel_fn("op_reapproximate")
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
    bound = fidelity_bound(ws_kernel.diagonal_of(old))
    refusal = _tolerance_gate(payload, bound)
    if refusal:
        return _decline(payload, refusal)
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
def _to_canonical(payload: dict) -> dict:
    """Replace each face's carrier by the plane, cylinder, cone or sphere it lies within `tol_mm` of
    (ShapeAnalysis_CanonicalRecognition); a face that is none of them declines the whole call."""
    from OCP.Geom import Geom_ConicalSurface, Geom_CylindricalSurface, Geom_Plane, Geom_SphericalSurface
    from OCP.gp import gp_Cone, gp_Cylinder, gp_Pln, gp_Sphere
    from OCP.ShapeAnalysis import ShapeAnalysis_CanonicalRecognition

    from meshpipeline.cad.repair_v4 import ws_kernel

    start = time.perf_counter()
    old = ws_kernel.read_brep(payload["brep_path"])
    bound = fidelity_bound(ws_kernel.diagonal_of(old))
    refusal = _tolerance_gate(payload, bound)
    if refusal:
        return _decline(payload, refusal)
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
def _refit_cluster(payload: dict) -> dict:
    """Replace a cluster of adjacent faces by ONE face on a fitted B-spline surface, bounded by the
    cluster's outline. The new face is declared added and the cluster declared removed."""
    from OCP.BRep import BRep_Builder
    from OCP.ShapeAnalysis import ShapeAnalysis_FreeBounds
    from OCP.TopAbs import TopAbs_EDGE, TopAbs_FACE
    from OCP.TopExp import TopExp
    from OCP.TopoDS import TopoDS, TopoDS_Compound
    from OCP.TopTools import TopTools_HSequenceOfShape, TopTools_IndexedDataMapOfShapeListOfShape

    from meshpipeline.cad.repair_v4 import ws_kernel

    start = time.perf_counter()
    old = ws_kernel.read_brep(payload["brep_path"])
    bound = fidelity_bound(ws_kernel.diagonal_of(old))
    refusal = _tolerance_gate(payload, bound)
    if refusal:
        return _decline(payload, refusal)
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

    built = _first_valid(old, build)
    if built is None:
        return _decline(payload, "no_valid_replacement: refit leaves the part invalid")
    new, history = built
    added = chosen["face"]
    measured = {"bound_mm": bound, **_two_way(faces, [added]),
                "cluster_faces": len(faces), "fit_surface": _surface_kind(added)}
    problem = _over_bound(measured, min(bound, payload["tol_mm"]), "deviation vs tol_mm")
    if problem:
        return _decline(payload, problem)
    return _finish(old, new, history, payload, op="refit_cluster",
                   params={"tol_mm": payload["tol_mm"]}, start=start, measured=measured,
                   added_faces=[_find_in(new, added)])


# --- parent side --------------------------------------------------------------------------------

_CONTINUITY = {"type": "string", "enum": ["G0", "G1"]}
_TOL = {"type": "number", "minimum": 1e-6, "maximum": BOUND_MAX_MM}


def _check(schema: dict, ws: Workspace, targets: list[StableId], params: dict, name: str) -> None:
    """Refuse (ValueError) a call the schema rejects. A small stand-in for Task 11's `check_call`,
    which covers the same ground; it goes when the registry does."""
    from meshpipeline.cad.repair_v4.workspace import UnknownTarget

    spec = schema["x-targets"]
    prefix = {"face": "F", "edge": "E"}[spec["kind"]]
    try:
        ws.resolve(list(targets))
    except UnknownTarget as gone:
        raise ValueError(f"invalid call to {name}: {gone}") from gone
    problems = [f"{t} is not a {spec['kind']}" for t in targets if not t.startswith(prefix)]
    if len(targets) < spec["min"]:
        problems.append(f"needs at least {spec['min']} {spec['kind']} target(s), got {len(targets)}")
    if spec.get("max") is not None and len(targets) > spec["max"]:
        problems.append(f"takes at most {spec['max']} target(s), got {len(targets)}")
    for key in schema["required"]:
        if key not in params:
            problems.append(f"missing parameter {key!r}")
    for key, value in params.items():
        rule = schema["properties"].get(key)
        if rule is None:
            problems.append(f"unknown parameter {key!r}")
        elif "enum" in rule and value not in rule["enum"]:
            problems.append(f"{key}={value!r} is not one of {rule['enum']}")
        elif rule["type"] in ("number", "integer"):
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                problems.append(f"{key} must be a number")
            elif value < rule["minimum"] or value > rule["maximum"]:
                problems.append(f"{key}={value} is outside [{rule['minimum']}, {rule['maximum']}]")
    if problems:
        raise ValueError(f"invalid call to {name}: " + "; ".join(problems))


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
        return site.kind in self.site_kinds and any(
            e.startswith(self.entity_prefix) for e in site.entities)

    def apply(self, pool: WorkerPool, ws: Workspace, targets: list[StableId], params: dict, *,
              timeout_s: float) -> tuple[Workspace, Diff]:
        _check(self.params_schema, ws, targets, params, self.name)
        out_path = str(Path(ws.brep_path).parent / f"{self.name}-{uuid.uuid4().hex[:12]}.brep")
        payload = {**params, self.payload_key: [ws.indices[t] for t in targets],
                   "brep_path": ws.brep_path, "out_path": out_path, "quantum": ws.quantum}
        result = pool.call(self.kernel_fn, payload, timeout_s=timeout_s)
        if not result.ok or result.value is None:
            raise OpFailed(result.status, result.error or "no answer")
        if "declined" in result.value:
            raise OpDeclined(str(result.value["declined"]))
        record = result.value
        new = apply_history(ws, record)
        diff = new.history[-1]
        face_at = {index: sid for sid, index in new.indices.items() if sid.startswith("F")}
        if self.declares_added:
            diff.declared_added = [face_at[i] for i in record["added_face_indices"]]
        if self.declares_removed:
            diff.declared_removed = list(targets)
        diff.params["measured"] = record["measured"]
        return new, diff


def _schema(kind: str, minimum: int, maximum: int | None, properties: dict) -> dict:
    return {"type": "object", "properties": properties, "required": sorted(properties),
            "additionalProperties": False,
            "x-targets": {"kind": kind, "min": minimum, "max": maximum}}


class RebuildFace(_FaceOp):
    name = "rebuild_face"
    kernel_fn = "op_rebuild_face"
    params_schema = _schema("face", 1, 1, {"continuity": _CONTINUITY})
    site_kinds = frozenset({"invalid", "self_overlap", "intersection", "periodic", "pole"})
    entity_prefix = "F"


class FillLoop(_FaceOp):
    """Declares the face it adds. Refuses a loop spanning more than 5% of the part's diagonal."""

    name = "fill_loop"
    kernel_fn = "op_fill_loop"
    params_schema = _schema("edge", 1, None, {"continuity": _CONTINUITY})
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
    """Declares the one face it adds and the cluster it removes."""

    name = "refit_cluster"
    kernel_fn = "op_refit_cluster"
    params_schema = _schema("face", 2, None, {"tol_mm": _TOL})
    site_kinds = frozenset({"sliver", "overlap", "self_overlap"})
    entity_prefix = "F"
    declares_added = True
    declares_removed = True


OPS = (RebuildFace(), FillLoop(), Reapproximate(), ToCanonical(), RefitCluster())
