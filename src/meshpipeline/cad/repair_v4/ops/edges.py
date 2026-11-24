# Responsibility: The edge and tolerance repair ops - rebuild an edge as the intersection of its two
#                 faces, tighten inflated tolerances to what the geometry earns, and restore the
#                 degenerate edges at a pole.
# Owns: three Op classes (`OPS`), their kernel functions (registered into kernel_calls.REGISTRY)
#       and the measurements (`edge_deviation`, `max_edge_tolerance`) that prove they worked.
# Boundaries: the parent half imports no OCCT; every kernel function runs in a worker and reads
#             and writes BREP paths. Registry wiring (ops/__init__) is not here. How far geometry moves is
#             MEASURED and reported (`max_move`, beside the part's fidelity bound); it gates nothing (owner 2026-11-24).
"""Edge and tolerance ops: `rebuild_edge_as_intersection`, `tighten_tolerances`, `fix_poles`.

Why they exist: an edge whose tolerance is far larger than its true pcurve/3D deviation lets the face
mesh leave the surface (gmsh "overlapping facets"). Tightening is measurement, not labelling: a
tolerance is only ever lowered to a value the geometry has been measured to satisfy, and an edge
that cannot reach the target keeps its honest, larger value and is REPORTED, never clamped.

WHAT IS AND IS NOT GUARANTEED. An edge tolerance is at least the kernel's own forced-SameParameter
result and at least a measured pcurve/3D deviation (513 uniform samples + a local golden-section
refinement around the worst). That is trustworthy for smooth edges; it is NOT a proven bound:
sampling is uniform, not curvature-aware, and the refinement finds a local maximum, so a spike
narrower than a sample cell or a second peak elsewhere can be missed. Independent evidence is only
BRepCheck: no sub-shape may be newly invalid (`_assert_no_new_invalid`), which works on inputs that
are already invalid. Face tolerances are not measured; see `_tighten_tolerances`.

Identity: tolerance ops mutate TShapes in place, so the kernel reads the BREP twice - `old` (to
fingerprint, since a bbox includes tolerance) and `base` (to mutate) - and tells the workspace what
became what through a BRepTools_History it composes (`_compose_history`). Only entities that really
changed appear in `Diff.modified`.
"""

from __future__ import annotations

import functools
import logging
import os
import time
import uuid
from collections.abc import Callable
from typing import Any

from meshpipeline.cad.repair_v4.kernel_calls import register
from meshpipeline.cad.repair_v4.ops import OpDeclined, OpFailed
from meshpipeline.cad.repair_v4.types import Site, StableId
from meshpipeline.cad.repair_v4.worker_pool import WorkerPool
from meshpipeline.cad.repair_v4.workspace import Diff, Workspace, apply_history

logger = logging.getLogger(__name__)

#: Dense uniform samples per pcurve, then a golden-section refinement around the worst one, so a
#: peak between samples on a curved edge is found rather than skipped.
_SAMPLES = 513
_SIDE_KINDS = ("invalid", "self_overlap", "overlap", "intersection", "proximity")
_KIND_OF_PREFIX = {"F": "face", "E": "edge", "B": "solid"}


def fidelity_bound(diagonal: float) -> float:
    """The furthest an op may move geometry: min(0.1 mm, 0.001 x diagonal)."""
    return min(0.1, 0.001 * diagonal)


# ---------------------------------------------------------------- kernel half (runs in a worker)

def _edge_faces(shape: Any) -> Any:
    from OCP.TopAbs import TopAbs_EDGE, TopAbs_FACE
    from OCP.TopExp import TopExp
    from OCP.TopTools import TopTools_IndexedDataMapOfShapeListOfShape

    anc = TopTools_IndexedDataMapOfShapeListOfShape()
    TopExp.MapShapesAndAncestors_s(shape, TopAbs_EDGE, TopAbs_FACE, anc)
    return anc


def _distinct_faces(anc: Any, edge: Any) -> list[Any]:
    from OCP.TopoDS import TopoDS

    out: list[Any] = []
    for f in anc.FindFromKey(edge):
        if not any(f.IsSame(g) for g in out):
            out.append(TopoDS.Face_s(f))
    return out


def edge_deviation(edge: Any, faces: list[Any]) -> float:
    """Largest distance between the edge's 3D curve and each of its pcurves lifted onto its face at
    the same parameter: the quantity SameParameter promises is <= the tolerance. `_SAMPLES` uniform
    samples, then a golden-section search for the maximum inside the two cells around the worst."""
    from OCP.BRepAdaptor import BRepAdaptor_Curve, BRepAdaptor_Curve2d, BRepAdaptor_Surface

    curve = BRepAdaptor_Curve(edge)
    first, last = curve.FirstParameter(), curve.LastParameter()
    step = (last - first) / (_SAMPLES - 1)
    worst = 0.0
    for face in faces:
        pc = BRepAdaptor_Curve2d(edge, face)
        surf = BRepAdaptor_Surface(face)

        def gap(t: float, pc: Any = pc, surf: Any = surf) -> float:
            uv = pc.Value(t)
            return float(curve.Value(t).Distance(surf.Value(uv.X(), uv.Y())))

        values = [gap(first + step * i) for i in range(_SAMPLES)]
        peak = max(range(_SAMPLES), key=values.__getitem__)
        worst = max(worst, values[peak])
        lo, hi = first + step * max(peak - 1, 0), first + step * min(peak + 1, _SAMPLES - 1)
        phi = 0.6180339887498949
        for _ in range(40):
            x1, x2 = hi - phi * (hi - lo), lo + phi * (hi - lo)
            if gap(x1) < gap(x2):
                lo = x1
            else:
                hi = x2
            worst = max(worst, gap(x1), gap(x2))
    return worst


def max_edge_tolerance(shape: Any) -> float:
    from OCP.BRep import BRep_Tool
    from OCP.TopAbs import TopAbs_EDGE
    from OCP.TopExp import TopExp_Explorer
    from OCP.TopoDS import TopoDS

    worst = 0.0
    ex = TopExp_Explorer(shape, TopAbs_EDGE)
    while ex.More():
        worst = max(worst, BRep_Tool.Tolerance_s(TopoDS.Edge_s(ex.Current())))
        ex.Next()
    return worst


def _indexed(shape: Any, kind: str) -> Any:
    from meshpipeline.cad.repair_v4.ws_kernel import _maps

    return _maps(shape)[kind]


def _settle_tolerance(edge: Any, faces: list[Any]) -> float:
    """Give the edge the tolerance the KERNEL reaches, never less than the measured deviation.

    The tolerance is reset to the floor so that a forced BRepLib::SameParameter reports what it
    actually reaches (it never lowers an inflated value on its own); the result is
    max(kernel tolerance, measured deviation, 1e-7), no margin. There is deliberately NO per-edge
    BRepCheck here: an edge analysed alone has no face context and cannot see orientation or
    in-face problems, so such a check could not reject anything. The in-context check is
    `_assert_no_new_invalid`, run on the whole result by `_finish`."""
    from OCP.BRep import BRep_Builder, BRep_Tool
    from OCP.BRepLib import BRepLib
    from OCP.ShapeFix import ShapeFix_ShapeTolerance
    from OCP.TopAbs import TopAbs_EDGE

    setter = ShapeFix_ShapeTolerance()
    setter.SetTolerance(edge, 1e-7, TopAbs_EDGE)
    BRepLib.SameParameter_s(edge, 1e-7, True)   # returns nothing: the flag and tolerance are read back
    dev = edge_deviation(edge, faces)
    setter.SetTolerance(edge, max(float(BRep_Tool.Tolerance_s(edge)), dev, 1e-7), TopAbs_EDGE)
    if not BRep_Tool.SameParameter_s(edge):
        BRep_Builder().SameParameter(edge, True)  # only reached when `dev` above is the evidence
    return dev


def _invalid_subshapes(shape: Any) -> set[tuple[str, int]]:
    """Every face, edge, wire, shell and solid that BRepCheck_Analyzer rejects, judged in the
    context of the whole shape, as (kind, 1-based index)."""
    from OCP.BRepCheck import BRepCheck_Analyzer
    from OCP.TopAbs import TopAbs_EDGE, TopAbs_FACE, TopAbs_SHELL, TopAbs_SOLID, TopAbs_WIRE
    from OCP.TopExp import TopExp
    from OCP.TopTools import TopTools_IndexedMapOfShape

    analyzer = BRepCheck_Analyzer(shape)
    out: set[tuple[str, int]] = set()
    for kind, topabs in (("face", TopAbs_FACE), ("edge", TopAbs_EDGE), ("wire", TopAbs_WIRE),
                         ("shell", TopAbs_SHELL), ("solid", TopAbs_SOLID)):
        indexed = TopTools_IndexedMapOfShape()
        TopExp.MapShapes_s(shape, topabs, indexed)
        out.update((kind, i) for i in range(1, indexed.Extent() + 1)
                   if not analyzer.IsValid(indexed.FindKey(i)))
    return out


class KernelDeclined(Exception):
    """A kernel refusal ABOUT THE SHAPE (nothing to change, a bound it would break, a geometry it cannot handle):
    `_declines` returns it to the parent as `{"declined": "<code>: <detail>"}`, the protocol every op uses, so the
    search and the proposer see a decline with a stable code, never an `error: RuntimeError`. A real bug still
    raises and comes back as `OpFailed`."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code, self.detail = code, detail


def _declines(fn: Callable[[dict], dict]) -> Callable[[dict], dict]:
    @functools.wraps(fn)
    def kernel(payload: dict) -> dict:
        try:
            return fn(payload)
        except KernelDeclined as why:
            return {"declined": str(why)}

    return kernel


def _assert_no_new_invalid(op: str, old: Any, result: Any, *, same_indices: bool) -> None:
    """Fail if the op made BRepCheck reject anything it accepted before. Unlike a whole-shape
    validity test this works on the usual repair input, which is already invalid elsewhere.
    `same_indices`: in-place ops keep every index, so sub-shapes are compared one to one; a rebuilt
    shape (fix_poles) renumbers, so only the count per kind may not grow."""
    before, after = _invalid_subshapes(old), _invalid_subshapes(result)
    fresh: list[Any]
    if same_indices:
        fresh = sorted(after - before)
    else:
        kinds = {k for k, _ in after}
        fresh = sorted(k for k in kinds
                       if sum(1 for x, _ in after if x == k) > sum(1 for x, _ in before if x == k))
    if fresh:
        raise KernelDeclined("invalidated", f"{op}: BRepCheck rejects sub-shapes it accepted before the op: {fresh}")


def _mapping_and_changed(old: Any, base: Any, result: Any, h: Any, quantum: float,
                         force_changed: set[tuple[str, int]]) -> tuple[Any, list[dict]]:
    """A BRepTools_History old -> `result`, and the OLD entities that really changed.

    `old` and `base` are two reads of one BREP, so entity i of one is entity i of the other. An op
    mutates `base` (and maybe rebuilds it into `result`, `h` being that rebuild's history). Since
    `old` shares no TShape with `result`, every old entity is told its successor explicitly: the
    entity itself if `result` still holds it, else what `h` says replaced it. New edges nobody
    claims are Generated from the face that holds them."""
    from OCP.BRepTools import BRepTools_History

    from meshpipeline.cad.repair_v4.ws_kernel import _maps, fingerprint

    out = BRepTools_History()
    old_maps, base_maps, new_maps = _maps(old), _maps(base), _maps(result)
    claimed: dict[str, set[int]] = {k: set() for k in new_maps}
    changed: list[dict] = []
    for kind, omap in old_maps.items():
        for i in range(1, omap.Extent() + 1):
            old_sub, base_sub = omap.FindKey(i), base_maps[kind].FindKey(i)
            successors: list[Any] = []
            if new_maps[kind].Contains(base_sub):
                successors.append(base_sub)
            elif not h.IsRemoved(base_sub):
                successors.extend(s for s in h.Modified(base_sub) if s.ShapeType() == base_sub.ShapeType())
            for s in successors:
                out.AddModified(old_sub, s)
                claimed[kind].add(new_maps[kind].FindIndex(s))
            if not successors:
                out.Remove(old_sub)
            # In place, a bbox moves with tolerance, which is not a change to the entity; only a
            # rebuilt entity is compared by fingerprint.
            same = len(successors) == 1 and (
                result is base
                or fingerprint(kind, successors[0], quantum, i) == fingerprint(kind, old_sub, quantum, i))
            if not same or (kind, i) in force_changed:
                changed.append({"type": kind, "index": i})
    # new edges (e.g. restored degenerate edges) belong to the face that holds them
    anc = _edge_faces(result)
    for j in range(1, new_maps["edge"].Extent() + 1):
        if j in claimed["edge"]:
            continue
        edge = new_maps["edge"].FindKey(j)
        for face in anc.FindFromKey(edge):
            for i in range(1, old_maps["face"].Extent() + 1):
                if new_maps["face"].Contains(face) and h.Modified(base_maps["face"].FindKey(i)).Size() and \
                        any(face.IsSame(s) for s in h.Modified(base_maps["face"].FindKey(i))):
                    out.AddGenerated(old_maps["face"].FindKey(i), edge)
    return out, changed


def _finish(payload: dict, op: str, params: dict, old: Any, base: Any, result: Any, h: Any,
            force_changed: set[tuple[str, int]], start: float) -> dict:
    from OCP.BRepCheck import BRepCheck_Analyzer

    from meshpipeline.cad.repair_v4.ws_kernel import diagonal_of, history_record, write_brep

    quantum = float(payload["quantum"])
    _assert_no_new_invalid(op, old, result, same_indices=result is base)
    valid_after = bool(BRepCheck_Analyzer(result).IsValid())
    history, changed = _mapping_and_changed(old, base, result, h, quantum, force_changed)
    write_brep(result, payload["out_path"])
    rec = history_record(old, result, history, op=op, params=params, old_path=payload["brep_path"],
                         new_path=payload["out_path"], quantum=quantum,
                         seconds=time.perf_counter() - start)
    rec["changed"] = changed
    rec["params"]["max_edge_tolerance_after"] = max_edge_tolerance(result)
    rec["params"]["valid_after"] = valid_after
    rec["params"]["diagonal"] = diagonal_of(result)
    return rec


def _targets_to_edges(base: Any, targets: dict[str, list[int]]) -> list[Any]:
    """The distinct edges under the targeted faces, edges and solids; every edge when none given."""
    from OCP.TopAbs import TopAbs_EDGE
    from OCP.TopExp import TopExp_Explorer
    from OCP.TopoDS import TopoDS

    emap = _indexed(base, "edge")
    if not any(targets.values()):
        return [TopoDS.Edge_s(emap.FindKey(i)) for i in range(1, emap.Extent() + 1)]
    picked: dict[int, Any] = {}
    for i in targets.get("edge", []):
        picked[i] = TopoDS.Edge_s(emap.FindKey(i))
    for kind in ("face", "solid"):
        kmap = _indexed(base, kind)
        for i in targets.get(kind, []):
            ex = TopExp_Explorer(kmap.FindKey(i), TopAbs_EDGE)
            while ex.More():
                e = TopoDS.Edge_s(ex.Current())
                picked[emap.FindIndex(e)] = e
                ex.Next()
    return [picked[k] for k in sorted(picked)]


def _targets_to_faces(base: Any, targets: dict[str, list[int]]) -> list[Any]:
    """Faces in scope: targeted faces and the faces of targeted solids; all when nothing is given;
    none when only edges are given."""
    from OCP.TopAbs import TopAbs_FACE
    from OCP.TopExp import TopExp_Explorer
    from OCP.TopoDS import TopoDS

    fmap = _indexed(base, "face")
    if not any(targets.values()):
        return [TopoDS.Face_s(fmap.FindKey(i)) for i in range(1, fmap.Extent() + 1)]
    picked: dict[int, Any] = {i: TopoDS.Face_s(fmap.FindKey(i)) for i in targets.get("face", [])}
    smap = _indexed(base, "solid")
    for i in targets.get("solid", []):
        ex = TopExp_Explorer(smap.FindKey(i), TopAbs_FACE)
        while ex.More():
            f = TopoDS.Face_s(ex.Current())
            picked[fmap.FindIndex(f)] = f
            ex.Next()
    return [picked[k] for k in sorted(picked)]


@register("v4_tighten_tolerances")
@_declines
def _tighten_tolerances(payload: dict) -> dict:
    """Lower edge, vertex and face tolerances to what the geometry has been shown to satisfy.

    Per edge above the target: forced SameParameter, then the tolerance is the kernel's reached
    value or the dense measured deviation, whichever is larger, gated on BRepCheck (see
    `_settle_tolerance`). Vertices in scope are capped to the target and then re-covered wherever an
    edge needs more. A face in scope is lowered to the largest tolerance of its own edges (never
    below 1e-7): a face cannot honestly be tighter than its boundary, and it is not independently
    measurable, so it is not clamped to the target. Anything left above the target is reported in
    `over_target` / `faces_over_target`, never relabelled. Faces are only CAPPED at
    max(target, edge tolerances) - their tolerance is not measured. Edges that already claim at or
    below the target are not changed but ARE measured, and any that under-report are listed in
    `under_reported` as [index, measured deviation]."""
    from OCP.BRep import BRep_Tool
    from OCP.BRepLib import BRepLib
    from OCP.ShapeFix import ShapeFix_Edge, ShapeFix_ShapeTolerance
    from OCP.TopAbs import TopAbs_EDGE, TopAbs_FACE, TopAbs_VERTEX
    from OCP.TopExp import TopExp, TopExp_Explorer
    from OCP.TopoDS import TopoDS

    from meshpipeline.cad.repair_v4.ws_kernel import diagonal_of, read_brep

    start = time.perf_counter()
    target_rel = float(payload["target_rel"])
    if not 1e-7 <= target_rel <= 1e-4:
        raise KernelDeclined("invalid_params", f"target_rel {target_rel!r} is outside [1e-7, 1e-4]")
    old, base = read_brep(payload["brep_path"]), read_brep(payload["brep_path"])
    diag = diagonal_of(base)
    target = target_rel * diag
    before = max_edge_tolerance(base)
    anc = _edge_faces(base)
    targets = payload.get("targets") or {}
    edges = _targets_to_edges(base, targets)
    faces = _targets_to_faces(base, targets)
    emap, fmap = _indexed(base, "edge"), _indexed(base, "face")
    force: set[tuple[str, int]] = set()
    over: list[int] = []
    fixer = ShapeFix_Edge()
    setter = ShapeFix_ShapeTolerance()
    # Vertices first: an edge's reached tolerance is floored by its vertices'.
    seen: list[Any] = []
    for edge in edges:
        for v in (TopExp.FirstVertex_s(edge, True), TopExp.LastVertex_s(edge, True)):
            if not any(v.IsSame(s) for s in seen):
                seen.append(v)
                if BRep_Tool.Tolerance_s(TopoDS.Vertex_s(v)) > target:
                    setter.LimitTolerance(v, 0.0, target, TopAbs_VERTEX)
    touched: list[Any] = []
    for edge in edges:
        if BRep_Tool.Degenerated_s(edge) or BRep_Tool.Tolerance_s(edge) <= target:
            continue   # at or below target: not ours to touch, and not ours to re-judge
        index = emap.FindIndex(edge)
        if not BRep_Tool.IsGeometric_s(edge) and not BRepLib.BuildCurves3d_s(edge, 1e-7):
            raise KernelDeclined("no_3d_curve", f"edge {index} has no 3D curve and BuildCurves3d could not make one")
        was = BRep_Tool.Tolerance_s(edge)
        _settle_tolerance(edge, _distinct_faces(anc, edge))
        touched.append(edge)
        if BRep_Tool.Tolerance_s(edge) != was:
            force.add(("edge", index))     # lowered OR raised: both are real changes
        if BRep_Tool.Tolerance_s(edge) > target:
            over.append(index)
    for i in range(1, emap.Extent() + 1):  # re-cover vertices that an edge still needs
        e = TopoDS.Edge_s(emap.FindKey(i))
        if not BRep_Tool.Degenerated_s(e) and fixer.FixVertexTolerance(e):
            logger.debug("tighten: raised a vertex tolerance to cover edge %d", i)
    faces_over: list[int] = []
    for face in faces:
        findex = fmap.FindIndex(face)
        edge_tols = []
        ex = TopExp_Explorer(face, TopAbs_EDGE)
        while ex.More():
            edge_tols.append(BRep_Tool.Tolerance_s(TopoDS.Edge_s(ex.Current())))
            ex.Next()
        floor = max([1e-7, *edge_tols])
        if BRep_Tool.Tolerance_s(face) > max(target, floor):
            # Not measurable on the surface, so never below the target the caller accepted: lowered
            # to max(target, its edges' tolerance), which is a cap, not a finding.
            setter.SetTolerance(face, max(target, floor), TopAbs_FACE)
            force.add(("face", findex))
        if BRep_Tool.Tolerance_s(face) > target:
            faces_over.append(findex)
    under: list[list[float]] = []
    for edge in edges:   # a claim at/below target is left alone, but a false one is reported
        if BRep_Tool.Degenerated_s(edge) or any(edge.IsSame(t) for t in touched):
            continue
        dev = edge_deviation(edge, _distinct_faces(anc, edge))
        if dev > BRep_Tool.Tolerance_s(edge):
            under.append([emap.FindIndex(edge), dev])
    params = {"target_rel": target_rel, "target_tolerance": target,
              "max_edge_tolerance_before": before, "over_target": over,
              "faces_over_target": faces_over, "under_reported": under}
    return _finish(payload, "tighten_tolerances", params, old, base, base, _no_history(), force, start)


def _no_history() -> Any:
    from OCP.BRepTools import BRepTools_History

    return BRepTools_History()


@register("v4_rebuild_edge_as_intersection")
@_declines
def _rebuild_edge_as_intersection(payload: dict) -> dict:
    """Replace an edge's 3D curve, in place, by the intersection curve of its two faces."""
    from OCP.BRep import BRep_Builder, BRep_Tool
    from OCP.BRepAdaptor import BRepAdaptor_Curve
    from OCP.GeomAPI import GeomAPI_IntSS, GeomAPI_ProjectPointOnCurve
    from OCP.ShapeBuild import ShapeBuild_Edge
    from OCP.ShapeFix import ShapeFix_Edge
    from OCP.TopAbs import TopAbs_FORWARD
    from OCP.TopExp import TopExp
    from OCP.TopLoc import TopLoc_Location
    from OCP.TopoDS import TopoDS

    from meshpipeline.cad.repair_v4.ws_kernel import diagonal_of, read_brep

    start = time.perf_counter()
    old, base = read_brep(payload["brep_path"]), read_brep(payload["brep_path"])
    diag = diagonal_of(base)
    bound = fidelity_bound(diag)
    anc = _edge_faces(base)
    emap = _indexed(base, "edge")
    force: set[tuple[str, int]] = set()
    moves: dict[int, float] = {}
    before = max_edge_tolerance(base)
    for index in payload["targets"]["edge"]:
        edge = TopoDS.Edge_s(emap.FindKey(index))
        faces = _distinct_faces(anc, edge)
        if BRep_Tool.Degenerated_s(edge) or len(faces) != 2:
            raise KernelDeclined("not_applicable", f"edge {index} does not lie between exactly two faces "
                             f"({len(faces)} found, degenerated={BRep_Tool.Degenerated_s(edge)})")
        # vertices in the order of the edge's STORED curve parameter, whatever its face orientation
        forward = TopoDS.Edge_s(edge.Oriented(TopAbs_FORWARD))
        v1, v2 = TopExp.FirstVertex_s(forward, True), TopExp.LastVertex_s(forward, True)
        if v1.IsSame(v2):
            raise KernelDeclined("not_applicable", f"edge {index} is closed; rebuilding a closed edge is not supported")
        p1, p2 = BRep_Tool.Pnt_s(v1), BRep_Tool.Pnt_s(v2)
        cur = BRepAdaptor_Curve(edge)
        t0, t1 = cur.FirstParameter(), cur.LastParameter()
        samples = [cur.Value(t0 + (t1 - t0) * k / (_SAMPLES - 1)) for k in range(_SAMPLES)]
        mid_old = samples[_SAMPLES // 2]
        inter = GeomAPI_IntSS(BRep_Tool.Surface_s(faces[0]), BRep_Tool.Surface_s(faces[1]), 1e-7)
        if not inter.IsDone() or inter.NbLines() == 0:
            raise KernelDeclined("no_intersection", f"the faces of edge {index} have no intersection curve")
        best: tuple[float, Any, float, float] | None = None
        for li in range(1, inter.NbLines() + 1):
            line = inter.Line(li)
            # IntSS returns a trimmed circle spanning the whole period: use the periodic basis so an
            # edge that wraps the trim point is representable.
            basis = getattr(line, "BasisCurve", None)
            if basis is not None and basis().IsPeriodic() and \
                    abs((line.LastParameter() - line.FirstParameter()) - basis().Period()) < 1e-9:
                line = basis()
            for candidate in (line, line.Reversed()):
                a = GeomAPI_ProjectPointOnCurve(p1, candidate)
                b = GeomAPI_ProjectPointOnCurve(p2, candidate)
                if a.NbPoints() == 0 or b.NbPoints() == 0:
                    continue
                u1, u2 = a.LowerDistanceParameter(), b.LowerDistanceParameter()
                if candidate.IsPeriodic():
                    period = candidate.Period()
                    while u2 <= u1:
                        u2 += period
                elif u2 <= u1:
                    continue
                mid = candidate.Value((u1 + u2) / 2)
                score = mid.Distance(mid_old) + a.LowerDistance() + b.LowerDistance()
                if best is None or score < best[0]:
                    best = (score, candidate, u1, u2)
        if best is None:
            raise KernelDeclined("no_intersection", f"no intersection curve of edge {index}'s faces spans its vertices")
        _, curve, u1, u2 = best
        move = max(GeomAPI_ProjectPointOnCurve(s, curve).LowerDistance() for s in samples)
        moves[index] = move
        builder = BRep_Builder()
        builder.SameParameter(edge, False)
        builder.UpdateEdge(edge, curve, TopLoc_Location(), max(BRep_Tool.Tolerance_s(edge), 1e-7))
        builder.Range(edge, u1, u2)
        sbe, fixer = ShapeBuild_Edge(), ShapeFix_Edge()
        for face in faces:
            sbe.RemovePCurve(edge, face)   # stale pcurves are dropped, never trusted
            if not fixer.FixAddPCurve(edge, face, False, 1e-7):
                # False is "nothing to add": benign only if the kernel can still give a pcurve (on a
                # plane it derives one by projection). If it cannot, the op must not continue.
                if BRep_Tool.CurveOnSurface_s(edge, face, 0.0, 0.0) is None:
                    raise KernelDeclined("no_pcurve", f"edge {index}: FixAddPCurve added no pcurve and none exists")
                logger.debug("rebuild: edge %d already has a derivable pcurve on a face", index)
        if fixer.FixSameParameter(edge, 1e-7):
            logger.debug("rebuild: FixSameParameter adjusted edge %d", index)
        _settle_tolerance(edge, faces)
        if fixer.FixVertexTolerance(edge):
            logger.debug("rebuild: raised a vertex tolerance to cover edge %d", index)
        force.add(("edge", index))
    params = {"edges": list(payload["targets"]["edge"]), "max_edge_tolerance_before": before,
              "max_move": max(moves.values(), default=0.0), "fidelity_bound": bound}
    return _finish(payload, "rebuild_edge_as_intersection", params, old, base, base, _no_history(),
                   force, start)


@register("v4_fix_poles")
@_declines
def _fix_poles(payload: dict) -> dict:
    """Run ShapeFix_Face on the faces (wire fixing incl. FixDegenerated, missing seam), through one
    ReShape context so the rebuilt faces and their containers come back with a history.

    Every targeted face must have been changed (a face ShapeFix leaves alone is reported, not
    skipped silently) and every rebuilt face must pass BRepCheck_Analyzer. The rest of the shape
    may be invalid already; it only may not get worse (`_finish`)."""
    from OCP.BRepCheck import BRepCheck_Analyzer
    from OCP.ShapeBuild import ShapeBuild_ReShape
    from OCP.ShapeFix import ShapeFix_Face
    from OCP.TopoDS import TopoDS

    from meshpipeline.cad.repair_v4.ws_kernel import read_brep

    start = time.perf_counter()
    old, base = read_brep(payload["brep_path"]), read_brep(payload["brep_path"])
    fmap = _indexed(base, "face")
    ctx = ShapeBuild_ReShape()
    unchanged: list[int] = []
    faces: dict[int, Any] = {}
    for index in payload["targets"]["face"]:
        face = TopoDS.Face_s(fmap.FindKey(index))
        faces[index] = face
        fixer = ShapeFix_Face(face)
        fixer.SetContext(ctx)
        if not fixer.Perform():
            unchanged.append(index)
    if unchanged:
        raise KernelDeclined("no_change", f"ShapeFix_Face.Perform changed nothing on face(s) {unchanged}")
    result = ctx.Apply(base)
    bad = [i for i, f in faces.items() if not BRepCheck_Analyzer(ctx.Value(f)).IsValid()]
    if bad:
        raise KernelDeclined("invalidated", f"fix_poles: the rebuilt face(s) {bad} still fail BRepCheck_Analyzer")
    params = {"faces": list(payload["targets"]["face"]),
              "max_edge_tolerance_before": max_edge_tolerance(base)}
    return _finish(payload, "fix_poles", params, old, base, result, ctx.History(), set(), start)


# ---------------------------------------------------------------- parent half (no OCCT)

def _split(ws: Workspace, targets: list[StableId], allowed: str) -> dict[str, list[int]]:
    ws.resolve(targets)
    out: dict[str, list[int]] = {"face": [], "edge": [], "solid": []}
    for sid in targets:
        kind = _KIND_OF_PREFIX[sid[0]]
        if kind not in allowed.split(","):
            raise ValueError(f"{sid} is a {kind}; this op takes {allowed}")
        out[kind].append(ws.indices[sid])
    return out


def _run(pool: WorkerPool, ws: Workspace, fn: str, payload: dict, timeout_s: float) -> tuple[Workspace, Diff]:
    payload = {**payload, "brep_path": ws.brep_path, "quantum": ws.quantum,
               "out_path": os.path.join(os.path.dirname(ws.brep_path), f"{fn}-{uuid.uuid4().hex[:8]}.brep")}
    result = pool.call(fn, payload, timeout_s=timeout_s)
    if not result.ok or result.value is None:
        raise OpFailed(result.status, f"{fn}: {result.error or 'no answer'}")
    record = result.value
    if "declined" in record:
        raise OpDeclined(str(record["declined"]))
    new = apply_history(ws, record)
    # a 1:1 Modified entry for an entity that did not change is noise the proposer must not read
    changed = {(c["type"], int(c["index"])) for c in record["changed"]}
    diff = new.history[-1]
    for sid in [s for s in diff.modified if (_KIND_OF_PREFIX[s[0]], ws.indices[s]) not in changed]:
        del diff.modified[sid]
    return new, diff


class RebuildEdgeAsIntersection:
    name = "rebuild_edge_as_intersection"
    params_schema: dict = {"type": "object", "properties": {}, "additionalProperties": False}

    def applies_to(self, site: Site) -> bool:
        return site.kind in _SIDE_KINDS and any(e.startswith("E") for e in site.entities)

    def apply(self, pool: WorkerPool, ws: Workspace, targets: list[StableId], params: dict,
              *, timeout_s: float) -> tuple[Workspace, Diff]:
        return _run(pool, ws, "v4_rebuild_edge_as_intersection",
                    {"targets": _split(ws, targets, "edge")}, timeout_s)


class TightenTolerances:
    name = "tighten_tolerances"
    params_schema: dict = {
        "type": "object",
        "properties": {"target_rel": {"type": "number", "minimum": 1e-7, "maximum": 1e-4}},
        "required": ["target_rel"], "additionalProperties": False}

    def applies_to(self, site: Site) -> bool:
        return site.kind in _SIDE_KINDS

    def apply(self, pool: WorkerPool, ws: Workspace, targets: list[StableId], params: dict,
              *, timeout_s: float) -> tuple[Workspace, Diff]:
        """`targets` are the scope: faces, edges or solids; empty means the whole part."""
        return _run(pool, ws, "v4_tighten_tolerances",
                    {"targets": _split(ws, targets, "face,edge,solid"),
                     "target_rel": params["target_rel"]}, timeout_s)


class FixPoles:
    name = "fix_poles"
    params_schema: dict = {"type": "object", "properties": {}, "additionalProperties": False}

    def applies_to(self, site: Site) -> bool:
        return site.kind in ("pole", "periodic", "invalid") and any(e.startswith("F") for e in site.entities)

    def apply(self, pool: WorkerPool, ws: Workspace, targets: list[StableId], params: dict,
              *, timeout_s: float) -> tuple[Workspace, Diff]:
        return _run(pool, ws, "v4_fix_poles", {"targets": _split(ws, targets, "face")}, timeout_s)


OPS: tuple[Any, ...] = (RebuildEdgeAsIntersection(), TightenTolerances(), FixPoles())
