# Responsibility: the kernel half of v5's low_quality detectors: MEASURE a BREP's small edges, small faces, narrow
#                 (sliver) faces and small/floating solids against a size `e` the parent hands in. Never decides;
#                 `quality.py` does.
# Owns: `v5_quality_facts` (registered kernel fn) and the per-entity measurements it is built from.
# Boundaries: runs inside a kernel worker only; OCP is imported inside functions. Indices are the BREP's own TopExp
#             map indices (`ws_kernel._maps`), i.e. what `ws.indices` holds, so the parent translates them exactly.
"""Facts for the low_quality class (CRS v5 step 10), all measured against `e` (the parent's threshold, a tenth of
the shipped element size - Cubit's rule):

- small edges: a non-degenerated edge SHORTER than `e`, with its two faces and the dihedral angle between them;
- small faces: a face whose AREA is under `e^2`;
- narrow faces (slivers): a face whose inertia width `sqrt(12 I_min / A)` - the short side of a strip - is under `e`;
  with `rs_width_mm`, the width `remove_small` itself measures (it never under-measures, so it can be the strip's
  length) so the parent can size that op without it refusing its own target;
- for every flagged face: its NEIGHBOURS across its edges (shared length, dihedral angle at the shared edge's middle:
  0 = smooth continuation, 90 = a right-angle edge), which is what a merge into a neighbour is chosen by;
- solids (multi-solid parts): every solid's volume, the part's, and for the smallest ones whether they touch another
  solid (the nearest other solid and the distance).

Bounded by `CALL_BUDGET_S` (`truncated` says so); lists are capped smallest-first.
"""

from __future__ import annotations

import math
import time
from typing import Any

from meshpipeline.cad.repair_v4.kernel_calls import register

CALL_BUDGET_S = 30.0
MAX_ITEMS = 64
#: at most this many of the smallest solids are tested for touching another (one distance query per pair)
MAX_TOUCH_SOLIDS = 8
#: two solids within this many `e` of each other touch (a shared face measures ~0)
TOUCH_OF_E = 1e-3


def _length(edge: Any) -> float:
    from OCP.BRepGProp import BRepGProp
    from OCP.GProp import GProp_GProps

    props = GProp_GProps()
    BRepGProp.LinearProperties_s(edge, props)
    return float(props.Mass())


def face_widths(face: Any) -> tuple[float, float, float]:
    """(area, inertia width, remove_small's width) of one face. The inertia width is `sqrt(12 I_min / A)`: the short
    side of a rectangle, `sqrt(3) r` of a disc. remove_small's is the larger of that and the second bounding-box
    extent (`ops.shells._width`), reproduced here so the parent can size that op from a measurement."""
    from OCP.Bnd import Bnd_Box
    from OCP.BRepBndLib import BRepBndLib
    from OCP.BRepGProp import BRepGProp
    from OCP.GProp import GProp_GProps

    props = GProp_GProps()
    BRepGProp.SurfaceProperties_s(face, props)
    area = float(props.Mass())
    if area <= 0:
        return 0.0, 0.0, 0.0
    i_min = min(props.PrincipalProperties().Moments())
    inertia = (12.0 * max(i_min, 0.0) / area) ** 0.5
    box = Bnd_Box()
    BRepBndLib.Add_s(face, box)
    x0, y0, z0, x1, y1, z1 = box.Get()
    second = sorted((x1 - x0, y1 - y0, z1 - z0))[1]
    return area, inertia, max(inertia, second)


def _normal_at(face: Any, point: Any) -> Any | None:
    """The face's OUTWARD normal at the surface point nearest `point` (orientation applied), or None."""
    from OCP.BRep import BRep_Tool
    from OCP.GeomLProp import GeomLProp_SLProps
    from OCP.ShapeAnalysis import ShapeAnalysis_Surface
    from OCP.TopAbs import TopAbs_REVERSED

    surface = BRep_Tool.Surface_s(face)
    uv = ShapeAnalysis_Surface(surface).ValueOfUV(point, 1e-6)
    props = GeomLProp_SLProps(surface, uv.X(), uv.Y(), 1, 1e-9)
    if not props.IsNormalDefined():
        return None
    n = props.Normal()
    if face.Orientation() == TopAbs_REVERSED:
        n.Reverse()
    return n


def dihedral_deg(edge: Any, face_a: Any, face_b: Any) -> float | None:
    """The angle between the two faces' outward normals at the edge's middle: 0 = a smooth continuation (the edge
    is no feature), 90 = a right-angle edge. None when a normal is undefined there."""
    from OCP.BRepAdaptor import BRepAdaptor_Curve

    curve = BRepAdaptor_Curve(edge)
    mid = curve.Value(0.5 * (curve.FirstParameter() + curve.LastParameter()))
    na, nb = _normal_at(face_a, mid), _normal_at(face_b, mid)
    if na is None or nb is None:
        return None
    return math.degrees(na.Angle(nb))


def _ancestors(shape: Any, sub: Any, parent: Any) -> Any:
    from OCP.TopExp import TopExp
    from OCP.TopTools import TopTools_IndexedDataMapOfShapeListOfShape

    out = TopTools_IndexedDataMapOfShapeListOfShape()
    TopExp.MapShapesAndAncestors_s(shape, sub, parent, out)
    return out


def _faces_of(efmap: Any, edge: Any) -> list[Any]:
    from OCP.TopoDS import TopoDS

    if not efmap.Contains(edge):
        return []
    out: list[Any] = []
    for f in efmap.FindFromKey(edge):
        face = TopoDS.Face_s(f)
        if not any(face.IsSame(g) for g in out):
            out.append(face)
    return out


def neighbours(face: Any, fmap: Any, emap: Any, efmap: Any) -> list[dict]:
    """Every face sharing an edge with `face`: index, shared length, the dihedral at the longest shared edge."""
    from OCP.BRep import BRep_Tool
    from OCP.TopAbs import TopAbs_EDGE
    from OCP.TopExp import TopExp_Explorer
    from OCP.TopoDS import TopoDS

    rows: dict[int, dict] = {}
    seen: set[int] = set()
    ex = TopExp_Explorer(face, TopAbs_EDGE)
    while ex.More():
        edge = TopoDS.Edge_s(ex.Current())
        ex.Next()
        j = emap.FindIndex(edge)
        if j in seen or BRep_Tool.Degenerated_s(edge):
            continue
        seen.add(j)
        length = _length(edge)
        for other in _faces_of(efmap, edge):
            if other.IsSame(face):
                continue
            k = fmap.FindIndex(other)
            row = rows.setdefault(k, {"face": k, "shared_mm": 0.0, "edges": [], "dihedral_deg": None, "_best": -1.0})
            row["shared_mm"] += length
            row["edges"].append(j)
            if length > row["_best"]:
                row["_best"] = length
                try:
                    angle = dihedral_deg(edge, face, other)
                except Exception:  # noqa: BLE001 - an unmeasurable angle is unknown, never a feature edge invented
                    angle = None
                row["dihedral_deg"] = None if angle is None else round(angle, 3)
    out = []
    for row in rows.values():
        row.pop("_best")
        row["shared_mm"] = round(row["shared_mm"], 9)
        out.append(row)
    return sorted(out, key=lambda r: -r["shared_mm"])


def quality_facts(shape: Any, *, e_mm: float, faces: list[int] | None = None, budget_s: float = CALL_BUDGET_S,
                  max_items: int = MAX_ITEMS) -> dict[str, Any]:
    from OCP.BRep import BRep_Tool
    from OCP.BRepExtrema import BRepExtrema_DistShapeShape
    from OCP.BRepGProp import BRepGProp
    from OCP.GProp import GProp_GProps
    from OCP.TopAbs import TopAbs_EDGE, TopAbs_FACE, TopAbs_SOLID
    from OCP.TopExp import TopExp
    from OCP.TopoDS import TopoDS
    from OCP.TopTools import TopTools_IndexedMapOfShape

    start = time.monotonic()
    fmap, emap, smap = TopTools_IndexedMapOfShape(), TopTools_IndexedMapOfShape(), TopTools_IndexedMapOfShape()
    TopExp.MapShapes_s(shape, TopAbs_FACE, fmap)
    TopExp.MapShapes_s(shape, TopAbs_EDGE, emap)
    TopExp.MapShapes_s(shape, TopAbs_SOLID, smap)
    efmap = _ancestors(shape, TopAbs_EDGE, TopAbs_FACE)
    fsmap = _ancestors(shape, TopAbs_FACE, TopAbs_SOLID)
    out: dict[str, Any] = {"e_mm": e_mm, "faces": fmap.Extent(), "edges": emap.Extent(), "solids": smap.Extent(),
                           "small_edges": [], "small_faces": [], "narrow_faces": [], "solid_volumes": [],
                           "part_volume": None, "truncated": False}
    if not (e_mm > 0 and math.isfinite(e_mm)):
        return {**out, "error": f"e_mm {e_mm!r} is not a positive size"}

    def solid_of(face: Any) -> int | None:
        if not fsmap.Contains(face):
            return None
        for s in fsmap.FindFromKey(face):
            return smap.FindIndex(s)
        return None

    scope = set(faces) if faces is not None else None
    small_e, small_f, narrow = [], [], []
    for j in range(1, emap.Extent() + 1):
        if time.monotonic() - start > budget_s:
            out["truncated"] = True
            break
        edge = TopoDS.Edge_s(emap.FindKey(j))
        if BRep_Tool.Degenerated_s(edge):
            continue
        adj = _faces_of(efmap, edge)
        idx = [fmap.FindIndex(f) for f in adj]
        if scope is not None and not set(idx) & scope:
            continue
        length = _length(edge)
        if 0 < length < e_mm:
            row: dict[str, Any] = {"edge": j, "length_mm": round(length, 9), "faces": idx}
            if len(adj) == 2:
                try:
                    angle = dihedral_deg(edge, adj[0], adj[1])
                except Exception:  # noqa: BLE001
                    angle = None
                row["dihedral_deg"] = None if angle is None else round(angle, 3)
            small_e.append(row)
    for i in range(1, fmap.Extent() + 1):
        if scope is not None and i not in scope:
            continue
        if time.monotonic() - start > budget_s:
            out["truncated"] = True
            break
        face = TopoDS.Face_s(fmap.FindKey(i))
        area, width, rs_width = face_widths(face)
        if area < e_mm * e_mm:
            small_f.append({"face": i, "area_mm2": round(area, 12), "width_mm": round(width, 9),
                            "rs_width_mm": round(rs_width, 9), "solid": solid_of(face)})
        elif width < e_mm:
            narrow.append({"face": i, "area_mm2": round(area, 9), "width_mm": round(width, 9),
                           "rs_width_mm": round(rs_width, 9), "solid": solid_of(face)})
    small_e.sort(key=lambda r: r["length_mm"])
    small_f.sort(key=lambda r: r["area_mm2"])  # type: ignore[arg-type, return-value]
    narrow.sort(key=lambda r: r["width_mm"])  # type: ignore[arg-type, return-value]
    out["small_edges"], out["small_faces"], out["narrow_faces"] = (
        small_e[:max_items], small_f[:max_items], narrow[:max_items])
    out["counts"] = {"small_edges": len(small_e), "small_faces": len(small_f), "narrow_faces": len(narrow)}
    for row in [*out["small_faces"], *out["narrow_faces"]]:
        if time.monotonic() - start > budget_s:
            out["truncated"] = True
            break
        try:
            row["neighbours"] = neighbours(TopoDS.Face_s(fmap.FindKey(row["face"])), fmap, emap, efmap)[:6]
        except Exception as why:  # noqa: BLE001 - one unmeasurable face degrades the facts, never loses them
            out.setdefault("errors", []).append(f"face {row['face']}: {type(why).__name__}: {why}"[:200])
    if smap.Extent() > 1:
        vols = []
        for k in range(1, smap.Extent() + 1):
            props = GProp_GProps()
            BRepGProp.VolumeProperties_s(smap.FindKey(k), props)
            vols.append([k, abs(float(props.Mass()))])
        out["part_volume"] = sum(v for _k, v in vols)
        vols.sort(key=lambda r: r[1])
        rows = [{"solid": k, "volume_mm3": v} for k, v in vols[:max_items]]
        for row in rows[:MAX_TOUCH_SOLIDS]:
            if time.monotonic() - start > budget_s:
                out["truncated"] = True
                break
            me = smap.FindKey(row["solid"])
            best: tuple[float, int] | None = None
            for k in range(1, smap.Extent() + 1):
                if k == row["solid"] or time.monotonic() - start > budget_s:
                    continue
                dist = BRepExtrema_DistShapeShape(me, smap.FindKey(k))
                if dist.IsDone() and (best is None or dist.Value() < best[0]):
                    best = (float(dist.Value()), k)
            if best is not None:
                row["nearest_solid"], row["nearest_mm"] = best[1], round(best[0], 9)
                row["touching"] = best[0] <= TOUCH_OF_E * e_mm
        out["solid_volumes"] = rows
    out["seconds"] = round(time.monotonic() - start, 3)
    return out


@register("v5_quality_facts")
def _v5_quality_facts(payload: dict) -> dict:
    from meshpipeline.cad.repair_v4.ws_kernel import read_brep

    return quality_facts(read_brep(payload["brep_path"]), e_mm=float(payload["e_mm"]), faces=payload.get("faces"),
                         budget_s=float(payload.get("budget_s", CALL_BUDGET_S)))
