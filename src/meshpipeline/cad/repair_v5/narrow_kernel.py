# Responsibility: the kernel half of v5's NARROW-REGION detector (round 2, fix E): what B-rep entities sit at the
#                 points where a mesher's WORST elements are - small edges, small/narrow faces, and narrow regions
#                 (two non-adjacent edges, or two non-adjacent faces, closer than `e`) - each with its distance from
#                 the nearest worst point.
# Owns: `v5_narrow_facts` (registered kernel fn) and `narrow_facts`.
# Boundaries: measures, never decides (`narrow.py` does). Indices are the BREP's own TopExp map indices (what
#             `ws.indices` holds). The per-face measurements are `quality_kernel`'s, reused, not copied.
"""Where does the mesh actually fail? `mesh_child` locates a low-quality mesh at its worst tets (the centroid of their
boundary nodes, mm); this measures the geometry within `radius_mm` of those points:

- every face and edge within the radius, with its distance to the nearest point, its size (edge length; face area,
  inertia width, remove_small's width) and, for a face narrower than `e`, its neighbours (for the merge);
- NARROW EDGE PAIRS: two edges within the radius that share no vertex and come closer than `e` (a neck, a step lower
  than `e`, a thin strip between two curves): the region a mesher must fit flat tets into;
- NARROW FACE PAIRS: two faces within the radius that share no edge or vertex and come closer than `e`.

Bounded by `budget_s` (`truncated` says so); the per-point lists are capped nearest-first.
"""

from __future__ import annotations

import math
import time
from typing import Any

from meshpipeline.cad.repair_v4.kernel_calls import register

CALL_BUDGET_S = 30.0
#: at most this many faces / edges per point are kept, nearest first
MAX_NEAR = 24
#: at most this many near edges (faces) per point are paired for the narrow-pair test (n^2 distance queries)
MAX_PAIRED = 16


def _box(shape: Any) -> tuple:
    from OCP.Bnd import Bnd_Box
    from OCP.BRepBndLib import BRepBndLib

    box = Bnd_Box()
    BRepBndLib.Add_s(shape, box, False)
    return box.Get()


def _box_dist(box: tuple, p: tuple) -> float:
    return math.sqrt(sum(max(0.0, box[k] - p[k], p[k] - box[k + 3]) ** 2 for k in range(3)))


def _dist(a: Any, b: Any) -> float | None:
    from OCP.BRepExtrema import BRepExtrema_DistShapeShape

    d = BRepExtrema_DistShapeShape(a, b)
    return float(d.Value()) if d.IsDone() else None


def _vertex_set(shape: Any) -> set[int]:
    from OCP.TopAbs import TopAbs_VERTEX
    from OCP.TopExp import TopExp_Explorer

    out: set[int] = set()
    ex = TopExp_Explorer(shape, TopAbs_VERTEX)
    while ex.More():
        out.add(ex.Current().TShape().__hash__())
        ex.Next()
    return out


def narrow_facts(shape: Any, *, points: list, e_mm: float, radius_mm: float,
                 budget_s: float = CALL_BUDGET_S) -> dict[str, Any]:
    from OCP.BRep import BRep_Tool
    from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeVertex
    from OCP.gp import gp_Pnt
    from OCP.TopAbs import TopAbs_EDGE, TopAbs_FACE, TopAbs_SOLID
    from OCP.TopExp import TopExp
    from OCP.TopoDS import TopoDS
    from OCP.TopTools import TopTools_IndexedMapOfShape

    from meshpipeline.cad.repair_v5.quality_kernel import _ancestors, _faces_of, _length, face_widths, neighbours

    start = time.monotonic()
    fmap, emap, smap = TopTools_IndexedMapOfShape(), TopTools_IndexedMapOfShape(), TopTools_IndexedMapOfShape()
    TopExp.MapShapes_s(shape, TopAbs_FACE, fmap)
    TopExp.MapShapes_s(shape, TopAbs_EDGE, emap)
    TopExp.MapShapes_s(shape, TopAbs_SOLID, smap)
    efmap = _ancestors(shape, TopAbs_EDGE, TopAbs_FACE)
    fsmap = _ancestors(shape, TopAbs_FACE, TopAbs_SOLID)
    out: dict[str, Any] = {"e_mm": e_mm, "radius_mm": radius_mm, "points": [], "truncated": False}
    if not (e_mm > 0 and radius_mm > 0 and points):
        return {**out, "error": "nothing to measure (no points, or a non-positive size)"}
    fboxes = [_box(fmap.FindKey(i)) for i in range(1, fmap.Extent() + 1)]
    eboxes = [_box(emap.FindKey(j)) for j in range(1, emap.Extent() + 1)]

    def late() -> bool:
        if time.monotonic() - start > budget_s:
            out["truncated"] = True
            return True
        return False

    def solid_of(face: Any) -> int | None:
        if not fsmap.Contains(face):
            return None
        for s in fsmap.FindFromKey(face):
            return smap.FindIndex(s)
        return None

    for raw in points[:12]:
        if late():
            break
        p = tuple(float(c) for c in raw[:3])
        vertex = BRepBuilderAPI_MakeVertex(gp_Pnt(*p)).Vertex()
        row: dict[str, Any] = {"xyz": [round(c, 6) for c in p], "faces": [], "edges": [], "edge_pairs": [],
                               "face_pairs": []}
        near_f = []
        for i, box in enumerate(fboxes, start=1):
            if _box_dist(box, p) > radius_mm:
                continue
            d = _dist(vertex, fmap.FindKey(i))
            if d is not None and d <= radius_mm:
                near_f.append((d, i))
        near_e = []
        for j, box in enumerate(eboxes, start=1):
            if _box_dist(box, p) > radius_mm:
                continue
            edge = TopoDS.Edge_s(emap.FindKey(j))
            if BRep_Tool.Degenerated_s(edge):
                continue
            d = _dist(vertex, edge)
            if d is not None and d <= radius_mm:
                near_e.append((d, j))
        near_f.sort()
        near_e.sort()
        for d, i in near_f[:MAX_NEAR]:
            if late():
                break
            face = TopoDS.Face_s(fmap.FindKey(i))
            area, width, rs_width = face_widths(face)
            frow: dict[str, Any] = {"face": i, "dist_mm": round(d, 9), "area_mm2": round(area, 12),
                                    "width_mm": round(width, 9), "rs_width_mm": round(rs_width, 9),
                                    "solid": solid_of(face)}
            if width < e_mm or area < e_mm * e_mm:
                try:
                    frow["neighbours"] = neighbours(face, fmap, emap, efmap)[:6]
                except Exception as why:  # noqa: BLE001 - one unmeasurable face degrades the facts
                    out.setdefault("errors", []).append(f"face {i}: {type(why).__name__}: {why}"[:200])
            row["faces"].append(frow)
        for d, j in near_e[:MAX_NEAR]:
            edge = TopoDS.Edge_s(emap.FindKey(j))
            row["edges"].append({"edge": j, "dist_mm": round(d, 9), "length_mm": round(_length(edge), 9),
                                 "faces": [fmap.FindIndex(f) for f in _faces_of(efmap, edge)]})
        # narrow edge pairs: no shared vertex, closer than e
        paired = [(j, TopoDS.Edge_s(emap.FindKey(j))) for _d, j in near_e[:MAX_PAIRED]]
        verts = {j: _vertex_set(edge) for j, edge in paired}
        for a in range(len(paired)):
            for b in range(a + 1, len(paired)):
                if late():
                    break
                (ja, ea), (jb, eb) = paired[a], paired[b]
                if verts[ja] & verts[jb]:
                    continue
                d = _dist(ea, eb)
                if d is not None and 0.0 < d < e_mm:
                    fa = {fmap.FindIndex(f) for f in _faces_of(efmap, ea)}
                    fb = {fmap.FindIndex(f) for f in _faces_of(efmap, eb)}
                    row["edge_pairs"].append({"a": ja, "b": jb, "gap_mm": round(d, 9), "faces_a": sorted(fa),
                                              "faces_b": sorted(fb), "common": sorted(fa & fb)})
        # narrow face pairs: no shared vertex (so no shared edge either), closer than e
        fpaired = [(i, TopoDS.Face_s(fmap.FindKey(i))) for _d, i in near_f[:MAX_PAIRED]]
        fverts = {i: _vertex_set(face) for i, face in fpaired}
        for a in range(len(fpaired)):
            for b in range(a + 1, len(fpaired)):
                if late():
                    break
                (ia, fa_), (ib, fb_) = fpaired[a], fpaired[b]
                if fverts[ia] & fverts[ib]:
                    continue
                d = _dist(fa_, fb_)
                if d is not None and 0.0 < d < e_mm:
                    row["face_pairs"].append({"a": ia, "b": ib, "gap_mm": round(d, 9)})
        row["edge_pairs"].sort(key=lambda r: r["gap_mm"])
        row["face_pairs"].sort(key=lambda r: r["gap_mm"])
        out["points"].append(row)
    out["seconds"] = round(time.monotonic() - start, 3)
    return out


@register("v5_narrow_facts")
def _v5_narrow_facts(payload: dict) -> dict:
    from meshpipeline.cad.repair_v4.ws_kernel import read_brep

    return narrow_facts(read_brep(payload["brep_path"]), points=list(payload.get("points") or []),
                        e_mm=float(payload["e_mm"]), radius_mm=float(payload["radius_mm"]),
                        budget_s=float(payload.get("budget_s", CALL_BUDGET_S)))
