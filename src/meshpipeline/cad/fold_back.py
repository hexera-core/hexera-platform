# Responsibility: Find where a solid's surface folds back onto itself along an edge - two faces whose outward normals point opposite ways all along the edge they share: a knife edge of almost no angle, or a crack of almost no width. No mesher can fill one (car_vs_pole, 24 Oct: a strip 800 mm long lying 0.04 mm from its neighbour; gmsh refused the overlapping surface meshes at every size tried).
# Boundaries: detect only, on the exact B-rep; never heals (the CAD repair stage owns healing). Pure apart from reading the shape; never raises.
# Collaborates with: cad/fea_measure.py (records it with the FEA measurement), application/fea_check.py (stops the stage with the repair way out).
"""FOLD-BACK EDGES.

Along an edge shared by two faces of one solid, each face's outward normal is read at a few points.
Where the two point opposite ways at every point (within FOLD_DEG), the material on one side of the
edge is a wedge of almost no angle - or the gap is a slot of almost no width - so the two faces lie
on top of each other next to the edge. A tangent blend (normals the SAME way) is never one.
"""
from __future__ import annotations

import math

import numpy as np

#: Two outward normals within this many degrees of opposite, at every point read, make a fold.
FOLD_DEG = 1.0
#: Points read along each edge (its ends left out).
SAMPLES = 5
#: The most edges looked at (a part with more is not searched past them; the record says so).
MAX_EDGES = 200_000


def fold_back_edges(shape, *, mm_per_unit: float = 1.0, fold_deg: float = FOLD_DEG) -> dict:
    """{"folds": [{"body": k (1-based solid), "faces": [a, b] (1-based, OpenCASCADE's face order - the
    Face_k of the measurement), "length_mm", "opening_deg"}], "complete": bool}."""
    out: dict = {"folds": [], "complete": True}
    try:
        from OCP.BRep import BRep_Tool
        from OCP.BRepAdaptor import BRepAdaptor_Curve, BRepAdaptor_Surface
        from OCP.BRepLProp import BRepLProp_SLProps
        from OCP.GCPnts import GCPnts_AbscissaPoint
        from OCP.ShapeAnalysis import ShapeAnalysis_Surface
        from OCP.TopAbs import TopAbs_EDGE, TopAbs_FACE, TopAbs_REVERSED, TopAbs_SOLID
        from OCP.TopExp import TopExp, TopExp_Explorer
        from OCP.TopoDS import TopoDS
        from OCP.TopTools import TopTools_IndexedDataMapOfShapeListOfShape, TopTools_IndexedMapOfShape
    except ImportError:
        out["complete"] = False
        return out
    limit = -math.cos(math.radians(float(fold_deg)))
    fmap = TopTools_IndexedMapOfShape()
    TopExp.MapShapes_s(shape, TopAbs_FACE, fmap)
    seen = 0
    body = 0
    ex = TopExp_Explorer(shape, TopAbs_SOLID)
    while ex.More():
        body += 1
        solid = ex.Current()
        ex.Next()
        edges = TopTools_IndexedDataMapOfShapeListOfShape()
        TopExp.MapShapesAndAncestors_s(solid, TopAbs_EDGE, TopAbs_FACE, edges)
        for i in range(1, edges.Extent() + 1):
            seen += 1
            if seen > MAX_EDGES:
                out["complete"] = False
                return out
            try:
                edge = TopoDS.Edge_s(edges.FindKey(i))
                faces = list(edges.FindFromIndex(i))
                if len(faces) != 2 or BRep_Tool.Degenerated_s(edge) or faces[0].IsSame(faces[1]):
                    continue
                curve = BRepAdaptor_Curve(edge)
                ts = np.linspace(curve.FirstParameter(), curve.LastParameter(), SAMPLES + 2)[1:-1]
                normals = []
                for f in faces:
                    face = TopoDS.Face_s(f)
                    surf = BRepAdaptor_Surface(face)
                    proj = ShapeAnalysis_Surface(BRep_Tool.Surface_s(face))
                    row: list[np.ndarray | None] = []
                    for t in ts:
                        uv = proj.ValueOfUV(curve.Value(float(t)), 1e-7)
                        props = BRepLProp_SLProps(surf, uv.X(), uv.Y(), 1, 1e-9)
                        if not props.IsNormalDefined():
                            row.append(None)
                            continue
                        n = props.Normal()
                        v = np.array([n.X(), n.Y(), n.Z()])
                        row.append(-v if face.Orientation() == TopAbs_REVERSED else v)
                    normals.append(row)
                dots = [float(np.dot(a, b)) for a, b in zip(*normals, strict=True) if a is not None and b is not None]
                if len(dots) < 2 or max(dots) >= limit:
                    continue
                out["folds"].append({
                    "body": body, "faces": sorted([fmap.FindIndex(faces[0]), fmap.FindIndex(faces[1])]),
                    "length_mm": round(GCPnts_AbscissaPoint.Length_s(curve) * float(mm_per_unit), 2),
                    "opening_deg": round(math.degrees(math.acos(min(1.0, -min(dots)))), 3)})
            except Exception:  # noqa: BLE001 - an edge that cannot be read is not judged, but counted
                out["unread"] = int(out.get("unread", 0)) + 1
    return out


__all__ = ["FOLD_DEG", "fold_back_edges"]
