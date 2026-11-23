# Responsibility: The edge and tolerance repair ops - rebuild an edge as the intersection of its two
#                 faces, tighten inflated tolerances to what the geometry earns, and restore the
#                 degenerate edges at a pole.
# Owns: three Op classes (`OPS`), their kernel functions (registered into kernel_calls.REGISTRY)
#       and the measurements (`edge_deviation`, `max_edge_tolerance`) that prove they worked.
# Boundaries: the parent half imports no OCCT; every kernel function runs in a worker and reads
#             and writes BREP paths. Registry wiring (ops/__init__) is not here. Geometry moves
#             only within the fidelity bound min(0.1 mm, 0.001 x diagonal); a larger move aborts.
"""Edge and tolerance ops: `rebuild_edge_as_intersection`, `tighten_tolerances`, `fix_poles`.

Why they exist: an edge whose tolerance is far larger than its true pcurve/3D deviation lets the face
mesh leave the surface (gmsh "overlapping facets"). Tightening is measurement, not labelling: a
tolerance is only ever lowered to a value the geometry has been measured to satisfy, and an edge
that cannot reach the target keeps its honest, larger value and is REPORTED, never clamped.

Identity: tolerance ops mutate TShapes in place, so the kernel reads the BREP twice - `old` (to
fingerprint, since a bbox includes tolerance) and `base` (to mutate) - and tells the workspace what
became what through a BRepTools_History it composes (`_compose_history`). Only entities that really
changed appear in `Diff.modified`.
"""

from __future__ import annotations

import os
import time
import uuid
from typing import Any

from meshpipeline.cad.repair_v4.kernel_calls import register
from meshpipeline.cad.repair_v4.types import Site, StableId
from meshpipeline.cad.repair_v4.worker_pool import WorkerPool
from meshpipeline.cad.repair_v4.workspace import Diff, Workspace, apply_history

_SAMPLES = 33
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
    """Largest distance between the edge's 3D curve and each of its pcurves lifted onto its face,
    sampled at the same parameter: the quantity SameParameter promises is <= the tolerance."""
    from OCP.BRepAdaptor import BRepAdaptor_Curve, BRepAdaptor_Curve2d, BRepAdaptor_Surface

    curve = BRepAdaptor_Curve(edge)
    first, last = curve.FirstParameter(), curve.LastParameter()
    worst = 0.0
    for face in faces:
        pc = BRepAdaptor_Curve2d(edge, face)
        surf = BRepAdaptor_Surface(face)
        for i in range(_SAMPLES):
            t = first + (last - first) * i / (_SAMPLES - 1)
            uv = pc.Value(t)
            worst = max(worst, curve.Value(t).Distance(surf.Value(uv.X(), uv.Y())))
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
    """Set the edge's tolerance to what its measured pcurve/3D deviation needs (+5 %, floor 1e-7):
    lower if it was inflated, higher if the geometry is worse than claimed. Returns the deviation.
    BRepLib::SameParameter only ever helps when it finds the gap; this is the measurement that
    decides, and it is the same one `_tighten_tolerances` re-checks at the end."""
    from OCP.BRep import BRep_Builder
    from OCP.ShapeFix import ShapeFix_ShapeTolerance
    from OCP.TopAbs import TopAbs_EDGE

    dev = edge_deviation(edge, faces)
    ShapeFix_ShapeTolerance().SetTolerance(edge, max(1e-7, dev * 1.05), TopAbs_EDGE)
    BRep_Builder().SameParameter(edge, True)
    return dev


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
    history, changed = _mapping_and_changed(old, base, result, h, quantum, force_changed)
    write_brep(result, payload["out_path"])
    rec = history_record(old, result, history, op=op, params=params, old_path=payload["brep_path"],
                         new_path=payload["out_path"], quantum=quantum,
                         seconds=time.perf_counter() - start)
    rec["changed"] = changed
    rec["params"]["max_edge_tolerance_after"] = max_edge_tolerance(result)
    rec["params"]["valid_after"] = bool(BRepCheck_Analyzer(result).IsValid())
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


@register("v4_tighten_tolerances")
def _tighten_tolerances(payload: dict) -> dict:
    """Lower edge and vertex tolerances to what the geometry has been measured to satisfy.

    Per edge above the target: rebuild pcurves with a FORCED SameParameter (which sets the tolerance
    to the deviation it reaches, not to a label), keep that if it is lower. Vertices in scope are
    capped to the target and then re-covered wherever an edge needs more, so nothing is ever claimed
    tighter than it measures. An edge that stays above the target is reported in `over_target`."""
    from OCP.BRep import BRep_Tool
    from OCP.BRepLib import BRepLib
    from OCP.ShapeFix import ShapeFix_Edge, ShapeFix_ShapeTolerance
    from OCP.TopAbs import TopAbs_VERTEX
    from OCP.TopExp import TopExp
    from OCP.TopoDS import TopoDS

    from meshpipeline.cad.repair_v4.ws_kernel import diagonal_of, read_brep

    start = time.perf_counter()
    target_rel = float(payload["target_rel"])
    if not 1e-7 <= target_rel <= 1e-4:
        raise ValueError(f"target_rel {target_rel!r} is outside [1e-7, 1e-4]")
    old, base = read_brep(payload["brep_path"]), read_brep(payload["brep_path"])
    diag = diagonal_of(base)
    target = target_rel * diag
    before = max_edge_tolerance(base)
    anc = _edge_faces(base)
    edges = _targets_to_edges(base, payload.get("targets") or {})
    emap = _indexed(base, "edge")
    force: set[tuple[str, int]] = set()
    over: list[int] = []
    fixer = ShapeFix_Edge()
    # Vertices first: an edge's reached tolerance is floored by its vertices'.
    limit = ShapeFix_ShapeTolerance()
    seen: list[Any] = []
    for edge in edges:
        for v in (TopExp.FirstVertex_s(edge, True), TopExp.LastVertex_s(edge, True)):
            if not any(v.IsSame(s) for s in seen):
                seen.append(v)
                if BRep_Tool.Tolerance_s(TopoDS.Vertex_s(v)) > target:
                    limit.LimitTolerance(v, 0.0, target, TopAbs_VERTEX)
    for edge in edges:
        if BRep_Tool.Degenerated_s(edge):
            continue
        if BRep_Tool.Tolerance_s(edge) <= target:
            continue
        index = emap.FindIndex(edge)
        if not BRep_Tool.IsGeometric_s(edge):
            BRepLib.BuildCurves3d_s(edge, 1e-7)
        was = BRep_Tool.Tolerance_s(edge)
        BRepLib.SameParameter_s(edge, 1e-7, True)
        _settle_tolerance(edge, _distinct_faces(anc, edge))
        if BRep_Tool.Tolerance_s(edge) < was:
            force.add(("edge", index))
        if BRep_Tool.Tolerance_s(edge) > target:
            over.append(index)
    for i in range(1, emap.Extent() + 1):  # re-cover vertices that an edge still needs
        e = TopoDS.Edge_s(emap.FindKey(i))
        if not BRep_Tool.Degenerated_s(e):
            fixer.FixVertexTolerance(e)
    # the deviation must really be inside the tolerance now (SameParameter satisfied)
    for edge in edges:
        if not BRep_Tool.Degenerated_s(edge):
            faces = _distinct_faces(anc, edge)
            dev = edge_deviation(edge, faces)
            if dev > BRep_Tool.Tolerance_s(edge) + 1e-9:
                raise RuntimeError(f"edge {emap.FindIndex(edge)}: deviation {dev:.3g} exceeds its "
                                   f"tolerance {BRep_Tool.Tolerance_s(edge):.3g} after tightening")
    params = {"target_rel": target_rel, "target_tolerance": target,
              "max_edge_tolerance_before": before, "over_target": over}
    return _finish(payload, "tighten_tolerances", params, old, base, base, _no_history(), force, start)


def _no_history() -> Any:
    from OCP.BRepTools import BRepTools_History

    return BRepTools_History()


@register("v4_rebuild_edge_as_intersection")
def _rebuild_edge_as_intersection(payload: dict) -> dict:
    """Replace an edge's 3D curve, in place, by the intersection curve of its two faces."""
    from OCP.BRep import BRep_Builder, BRep_Tool
    from OCP.BRepAdaptor import BRepAdaptor_Curve
    from OCP.GeomAPI import GeomAPI_IntSS, GeomAPI_ProjectPointOnCurve
    from OCP.ShapeBuild import ShapeBuild_Edge
    from OCP.ShapeFix import ShapeFix_Edge
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
            raise ValueError(f"edge {index} does not lie between exactly two faces "
                             f"({len(faces)} found, degenerated={BRep_Tool.Degenerated_s(edge)})")
        v1, v2 = TopExp.FirstVertex_s(edge, True), TopExp.LastVertex_s(edge, True)
        if v1.IsSame(v2):
            raise ValueError(f"edge {index} is closed; rebuilding a closed edge is not supported")
        p1, p2 = BRep_Tool.Pnt_s(v1), BRep_Tool.Pnt_s(v2)
        cur = BRepAdaptor_Curve(edge)
        t0, t1 = cur.FirstParameter(), cur.LastParameter()
        samples = [cur.Value(t0 + (t1 - t0) * k / (_SAMPLES - 1)) for k in range(_SAMPLES)]
        mid_old = samples[_SAMPLES // 2]
        inter = GeomAPI_IntSS(BRep_Tool.Surface_s(faces[0]), BRep_Tool.Surface_s(faces[1]), 1e-7)
        if not inter.IsDone() or inter.NbLines() == 0:
            raise ValueError(f"the faces of edge {index} have no intersection curve")
        best: tuple[float, Any, float, float] | None = None
        for li in range(1, inter.NbLines() + 1):
            line = inter.Line(li)
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
            raise ValueError(f"no intersection curve of edge {index}'s faces spans its vertices")
        _, curve, u1, u2 = best
        move = max(GeomAPI_ProjectPointOnCurve(s, curve).LowerDistance() for s in samples)
        if move > bound:
            raise ValueError(f"edge {index}: the intersection curve is {move:.3g} from the existing "
                             f"edge, beyond the fidelity bound {bound:.3g}")
        moves[index] = move
        builder = BRep_Builder()
        builder.SameParameter(edge, False)
        builder.UpdateEdge(edge, curve, TopLoc_Location(), max(BRep_Tool.Tolerance_s(edge), 1e-7))
        builder.Range(edge, u1, u2)
        sbe, fixer = ShapeBuild_Edge(), ShapeFix_Edge()
        for face in faces:
            sbe.RemovePCurve(edge, face)
            fixer.FixAddPCurve(edge, face, False, 1e-7)
        fixer.FixSameParameter(edge, 1e-7)
        _settle_tolerance(edge, faces)
        fixer.FixVertexTolerance(edge)
        force.add(("edge", index))
    params = {"edges": list(payload["targets"]["edge"]), "max_edge_tolerance_before": before,
              "max_move": max(moves.values(), default=0.0), "fidelity_bound": bound}
    return _finish(payload, "rebuild_edge_as_intersection", params, old, base, base, _no_history(),
                   force, start)


@register("v4_fix_poles")
def _fix_poles(payload: dict) -> dict:
    """Run ShapeFix_Face on the faces (wire fixing incl. FixDegenerated, missing seam), through one
    ReShape context so the rebuilt faces and their containers come back with a history."""
    from OCP.ShapeBuild import ShapeBuild_ReShape
    from OCP.ShapeFix import ShapeFix_Face
    from OCP.TopoDS import TopoDS

    from meshpipeline.cad.repair_v4.ws_kernel import read_brep

    start = time.perf_counter()
    old, base = read_brep(payload["brep_path"]), read_brep(payload["brep_path"])
    fmap = _indexed(base, "face")
    ctx = ShapeBuild_ReShape()
    for index in payload["targets"]["face"]:
        fixer = ShapeFix_Face(TopoDS.Face_s(fmap.FindKey(index)))
        fixer.SetContext(ctx)
        fixer.Perform()
    result = ctx.Apply(base)
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
        raise RuntimeError(f"{fn} failed: {result.status}: {result.error}")
    record = result.value
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
