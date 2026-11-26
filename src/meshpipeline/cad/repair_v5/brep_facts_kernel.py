# Responsibility: the kernel half of v5's invalid_brep_tolerance detectors: MEASURE a BREP's tolerances, spline
#                 parameterisation, poles/degenerate edges and extent. Never decides; `brep_faults.py` does.
# Owns: `v5_brep_facts` (registered kernel fn) and the per-entity measurements it is built from.
# Boundaries: runs inside a kernel worker only; OCP is imported inside functions. Indices are the BREP's own TopExp
#             map indices (`ws_kernel._maps`), i.e. what `ws.indices` holds, so the parent translates them exactly.
"""Facts for the invalid_brep_tolerance class.

- tolerances: every edge/face/vertex tolerance above `tol_threshold_mm` (vertices reported through their edges);
- splines: per B-spline/Bezier face its degrees, knot and pole counts and PARAMETER-SPEED RATIO (max/min |dS/du|
  along interior iso-lines, and the same in v): a surface whose parameter runs 100x faster in one place than another
  is badly parameterised, and gmsh's 2D stage meshes in that parameter space; per B-spline edge curve the same;
- poles: a degenerated edge whose surface iso is NOT collapsed, a collapsed boundary iso with no degenerated edge
  on it, and a non-degenerated edge shorter than its own tolerance;
- the bounding-box diagonal (and whether it is open).

Bounded by `CALL_BUDGET_S` (`truncated` says so); lists are capped worst-first.
"""

from __future__ import annotations

import math
import time
from typing import Any

from meshpipeline.cad.repair_v4.kernel_calls import register

CALL_BUDGET_S = 60.0
MAX_ITEMS = 64
#: interior samples per iso-line (the ends are skipped: a pole there is a pole, not a parameterisation)
SPEED_SAMPLES = 9
ISO_LINES = 3
#: an iso shorter than this many tolerances is collapsed (a pole); a degenerated edge longer than it is not
COLLAPSED_TOLS = 10.0
_SPLINE_TYPES = ("BSplineSurface", "BezierSurface")


def _type(face: Any) -> str:
    from OCP.BRepAdaptor import BRepAdaptor_Surface

    return str(BRepAdaptor_Surface(face).GetType()).split(".")[-1].replace("GeomAbs_", "")


def _speed_ratio(surface: Any, u0: float, u1: float, v0: float, v1: float, along_u: bool) -> float | None:
    """max/min of |dS/du| (or |dS/dv|) over interior samples of ISO_LINES iso-lines; None when unmeasurable."""
    from OCP.gp import gp_Pnt, gp_Vec

    worst: float | None = None
    for k in range(1, ISO_LINES + 1):
        fixed = (v0 + (v1 - v0) * k / (ISO_LINES + 1)) if along_u else (u0 + (u1 - u0) * k / (ISO_LINES + 1))
        speeds = []
        for j in range(1, SPEED_SAMPLES + 1):
            t = j / (SPEED_SAMPLES + 1)
            u, v = (u0 + (u1 - u0) * t, fixed) if along_u else (fixed, v0 + (v1 - v0) * t)
            p, du, dv = gp_Pnt(), gp_Vec(), gp_Vec()
            surface.D1(u, v, p, du, dv)
            speeds.append((du if along_u else dv).Magnitude())
        top, low = max(speeds), min(speeds)
        if top <= 0:
            continue
        ratio = top / max(low, top * 1e-12)
        worst = ratio if worst is None else max(worst, ratio)
    return worst


def _curve_speed_ratio(curve: Any, t0: float, t1: float) -> float | None:
    from OCP.gp import gp_Pnt, gp_Vec

    speeds = []
    for j in range(1, SPEED_SAMPLES + 1):
        p, d = gp_Pnt(), gp_Vec()
        curve.D1(t0 + (t1 - t0) * j / (SPEED_SAMPLES + 1), p, d)
        speeds.append(d.Magnitude())
    top = max(speeds)
    return None if top <= 0 else top / max(min(speeds), top * 1e-12)


def _iso_length(surface: Any, fixed: float, a: float, b: float, u_fixed: bool) -> float:
    pts = [surface.Value(fixed, a + (b - a) * j / 8) if u_fixed else surface.Value(a + (b - a) * j / 8, fixed)
           for j in range(9)]
    return sum(pts[j].Distance(pts[j + 1]) for j in range(8))


def _edge_length(edge: Any) -> float:
    from OCP.BRepGProp import BRepGProp
    from OCP.GProp import GProp_GProps

    props = GProp_GProps()
    BRepGProp.LinearProperties_s(edge, props)
    return float(props.Mass())


def _spline_face(index: int, face: Any, kind: str) -> dict:
    from OCP.BRep import BRep_Tool
    from OCP.BRepTools import BRepTools

    surface = BRep_Tool.Surface_s(face)
    u0, u1, v0, v1 = BRepTools.UVBounds_s(face)
    row: dict[str, Any] = {"face": index, "type": kind}
    try:
        row.update(udeg=int(surface.UDegree()), vdeg=int(surface.VDegree()),
                   upoles=int(surface.NbUPoles()), vpoles=int(surface.NbVPoles()))
        if kind == "BSplineSurface":
            row.update(uknots=int(surface.NbUKnots()), vknots=int(surface.NbVKnots()))
    except Exception:  # noqa: BLE001 - a handle that is not the concrete type: the speeds still say something
        pass
    row["speed_ratio_u"] = _speed_ratio(surface, u0, u1, v0, v1, True)
    row["speed_ratio_v"] = _speed_ratio(surface, u0, u1, v0, v1, False)
    return row


def _spline_edge(index: int, edge: Any) -> dict | None:
    from OCP.BRepAdaptor import BRepAdaptor_Curve
    from OCP.GeomAbs import GeomAbs_BSplineCurve

    curve = BRepAdaptor_Curve(edge)
    if curve.GetType() != GeomAbs_BSplineCurve:
        return None
    spline = curve.BSpline()
    return {"edge": index, "deg": int(spline.Degree()), "knots": int(spline.NbKnots()),
            "poles": int(spline.NbPoles()),
            "speed_ratio": _curve_speed_ratio(curve, curve.FirstParameter(), curve.LastParameter())}


def _poles_of_face(index: int, face: Any, kind: str, emap: Any) -> list[dict]:
    """Pole faults on one face: a degenerated edge whose iso is not collapsed; a collapsed boundary iso (spline
    faces only) with no degenerated edge on the face; a non-degenerated edge shorter than its tolerance."""
    from OCP.BRep import BRep_Tool
    from OCP.BRepTools import BRepTools
    from OCP.TopAbs import TopAbs_EDGE
    from OCP.TopExp import TopExp_Explorer
    from OCP.TopoDS import TopoDS

    out: list[dict] = []
    surface = BRep_Tool.Surface_s(face)
    has_degenerate = False
    ex = TopExp_Explorer(face, TopAbs_EDGE)
    while ex.More():
        edge = TopoDS.Edge_s(ex.Current())
        ex.Next()
        tol = max(BRep_Tool.Tolerance_s(edge), 1e-7)
        if BRep_Tool.Degenerated_s(edge):
            has_degenerate = True
            pcurve = BRep_Tool.CurveOnSurface_s(edge, face, 0.0, 0.0)
            if pcurve is None:
                continue
            a, b = BRep_Tool.Range_s(edge)
            pts = []
            for j in range(9):
                uv = pcurve.Value(a + (b - a) * j / 8)
                pts.append(surface.Value(uv.X(), uv.Y()))
            spread = max(p.Distance(q) for p in pts for q in pts)
            if spread > COLLAPSED_TOLS * tol:
                out.append({"face": index, "edge": emap.FindIndex(edge), "why": "degenerate_not_collapsed",
                            "spread_mm": spread, "tol_mm": tol})
        else:
            length = _edge_length(edge)
            if length < tol:
                out.append({"face": index, "edge": emap.FindIndex(edge), "why": "zero_length_edge",
                            "length_mm": length, "tol_mm": tol})
    # round 2: a wire MISSING an edge (two consecutive edges ending at different vertices, farther apart than their
    # tolerances) - what a degenerated-flagged real edge leaves after the STEP round trip
    from meshpipeline.cad.repair_v4.ops.boundary import wire_gaps

    for _wire, k, _v1, _v2, gap in wire_gaps(face):
        out.append({"face": index, "edge": None, "why": "missing_edge", "after": k, "gap_mm": gap})
    if kind in _SPLINE_TYPES and not has_degenerate:
        u0, u1, v0, v1 = BRepTools.UVBounds_s(face)
        tol = max(BRep_Tool.Tolerance_s(face), 1e-7)
        for name, fixed, a, b, u_fixed in (("u0", u0, v0, v1, True), ("u1", u1, v0, v1, True),
                                           ("v0", v0, u0, u1, False), ("v1", v1, u0, u1, False)):
            length = _iso_length(surface, fixed, a, b, u_fixed)
            if length < tol:
                out.append({"face": index, "edge": None, "why": "collapsed_without_degenerate", "iso": name,
                            "length_mm": length, "tol_mm": tol})
    return out


def _solid_of_faces(shape: Any, fmap: Any) -> dict[int, int]:
    """face index -> the (first) solid index holding it, in the shape's own TopExp solid map."""
    from OCP.TopAbs import TopAbs_FACE, TopAbs_SOLID
    from OCP.TopExp import TopExp, TopExp_Explorer
    from OCP.TopTools import TopTools_IndexedMapOfShape

    smap = TopTools_IndexedMapOfShape()
    TopExp.MapShapes_s(shape, TopAbs_SOLID, smap)
    out: dict[int, int] = {}
    for s in range(1, smap.Extent() + 1):
        ex = TopExp_Explorer(smap.FindKey(s), TopAbs_FACE)
        while ex.More():
            out.setdefault(fmap.FindIndex(ex.Current()), s)
            ex.Next()
    return out


def _seams_of_face(index: int, face: Any, emap: Any, solid_of: dict[int, int], out: dict) -> None:
    """Round 2 (closed_periodic_face): a face carrying a seam is a CLOSED face (listed with its solid, so a periodic
    failure that names only bodies can be aimed at them); a seam whose 3D curve is off the surface iso by more than
    `boundary.SEAM_DEV_MM` is a bad seam (its tolerance may cover it: the reader raises it to, and the mesher still
    meshes the seam where the 3D curve is)."""
    from OCP.BRep import BRep_Tool

    from meshpipeline.cad.repair_v4.ops.boundary import SEAM_DEV_MM, seam_deviation, seams_of

    seams = seams_of(face)
    if not seams:
        return
    out["closed_faces"].append({"face": index, "solid": solid_of.get(index)})
    for edge in seams:
        dev = seam_deviation(face, edge)
        if dev is not None and dev > SEAM_DEV_MM:
            out["bad_seams"].append({"face": index, "edge": emap.FindIndex(edge), "dev_mm": dev,
                                     "tol_mm": BRep_Tool.Tolerance_s(edge)})


def brep_facts(shape: Any, *, tol_threshold_mm: float | None, faces: list[int] | None = None,
               budget_s: float = CALL_BUDGET_S, max_items: int = MAX_ITEMS) -> dict:
    """The facts of `shape` (indices are its own TopExp map indices). `faces` restricts the per-face scans (spline,
    pole) to those faces; tolerances and the extent are always whole-part. `tol_threshold_mm` None = 1e-3 x diagonal
    (layer 1's own large_tolerance line)."""
    from OCP.Bnd import Bnd_Box
    from OCP.BRep import BRep_Tool
    from OCP.BRepBndLib import BRepBndLib
    from OCP.TopAbs import TopAbs_EDGE, TopAbs_FACE
    from OCP.TopExp import TopExp
    from OCP.TopoDS import TopoDS
    from OCP.TopTools import TopTools_IndexedMapOfShape

    start = time.monotonic()
    box = Bnd_Box()
    BRepBndLib.Add_s(shape, box)
    open_box = (not box.IsVoid()) and any(
        (box.IsOpenXmin(), box.IsOpenXmax(), box.IsOpenYmin(), box.IsOpenYmax(), box.IsOpenZmin(), box.IsOpenZmax()))
    if box.IsVoid():
        diag = None
    else:
        x0, y0, z0, x1, y1, z1 = box.Get()
        diag = math.dist((x0, y0, z0), (x1, y1, z1))
    fmap, emap = TopTools_IndexedMapOfShape(), TopTools_IndexedMapOfShape()
    TopExp.MapShapes_s(shape, TopAbs_FACE, fmap)
    TopExp.MapShapes_s(shape, TopAbs_EDGE, emap)
    threshold = tol_threshold_mm if tol_threshold_mm is not None else (1e-3 * diag if diag else None)
    out: dict[str, Any] = {"diag_mm": diag, "open_box": open_box, "faces": fmap.Extent(), "edges": emap.Extent(),
                           "tol_threshold_mm": threshold, "tolerance": {"edges": [], "faces": []},
                           "max_tolerance": 0.0, "min_tolerance": None, "splines": [], "curve_splines": [],
                           "poles": [], "bad_seams": [], "closed_faces": [], "truncated": False}
    if diag is None or open_box or not math.isfinite(diag):
        return {**out, "seconds": round(time.monotonic() - start, 3)}
    over_e, over_f = [], []
    tols = []
    for i in range(1, emap.Extent() + 1):
        edge = TopoDS.Edge_s(emap.FindKey(i))
        tol = BRep_Tool.Tolerance_s(edge)
        # a vertex's tolerance is reported through its edge: an op aims at edges, not vertices
        vtol = max((BRep_Tool.Tolerance_s(TopoDS.Vertex_s(v)) for v in (TopExp.FirstVertex_s(edge),
                                                                        TopExp.LastVertex_s(edge)) if not v.IsNull()),
                   default=0.0)
        worst = max(tol, vtol)
        tols.append(tol)
        out["max_tolerance"] = max(out["max_tolerance"], worst)
        if threshold is not None and worst > threshold:
            over_e.append([i, worst])
    for i in range(1, fmap.Extent() + 1):
        tol = BRep_Tool.Tolerance_s(TopoDS.Face_s(fmap.FindKey(i)))
        tols.append(tol)
        if threshold is not None and tol > threshold:
            over_f.append([i, tol])
    out["max_tolerance"] = max([out["max_tolerance"], *tols])
    out["min_tolerance"] = min(tols) if tols else None
    out["tolerance"] = {"edges": sorted(over_e, key=lambda r: -r[1])[:max_items],
                        "faces": sorted(over_f, key=lambda r: -r[1])[:max_items],
                        "edges_over": len(over_e), "faces_over": len(over_f)}
    scan = faces if faces is not None else list(range(1, fmap.Extent() + 1))
    seen_edges: set[int] = set()
    solid_of = _solid_of_faces(shape, fmap)
    from OCP.TopExp import TopExp_Explorer

    for index in scan:
        if not 1 <= index <= fmap.Extent():
            continue
        if time.monotonic() - start > budget_s:
            out["truncated"] = True
            break
        face = TopoDS.Face_s(fmap.FindKey(index))
        kind = _type(face)
        try:
            if kind in _SPLINE_TYPES:
                out["splines"].append(_spline_face(index, face, kind))
            out["poles"].extend(_poles_of_face(index, face, kind, emap))
            _seams_of_face(index, face, emap, solid_of, out)
            ex = TopExp_Explorer(face, TopAbs_EDGE)
            while ex.More():
                edge = TopoDS.Edge_s(ex.Current())
                ex.Next()
                j = emap.FindIndex(edge)
                if j in seen_edges or BRep_Tool.Degenerated_s(edge):
                    continue
                seen_edges.add(j)
                row = _spline_edge(j, edge)
                if row is not None:
                    out["curve_splines"].append(row)
        except Exception as why:  # noqa: BLE001 - one unmeasurable face degrades the facts, never loses them
            out.setdefault("errors", []).append(f"face {index}: {type(why).__name__}: {why}"[:200])

    def badness(r: dict) -> float:
        return max(r.get("speed_ratio_u") or 0, r.get("speed_ratio_v") or 0, r.get("speed_ratio") or 0,
                   r.get("udeg") or r.get("deg") or 0, r.get("uknots") or r.get("knots") or 0)

    out["splines"] = sorted(out["splines"], key=lambda r: -badness(r))[:max_items]
    out["curve_splines"] = sorted(out["curve_splines"], key=lambda r: -badness(r))[:max_items]
    out["poles"] = out["poles"][:max_items]
    out["bad_seams"] = sorted(out["bad_seams"], key=lambda r: -r["dev_mm"])[:max_items]
    out["closed_faces"] = out["closed_faces"][:max_items]
    out["seconds"] = round(time.monotonic() - start, 3)
    return out


@register("v5_brep_facts")
def _v5_brep_facts(payload: dict) -> dict:
    from meshpipeline.cad.repair_v4.ws_kernel import read_brep

    return brep_facts(read_brep(payload["brep_path"]), tol_threshold_mm=payload.get("tol_threshold_mm"),
                      faces=payload.get("faces"), budget_s=float(payload.get("budget_s", CALL_BUDGET_S)))
