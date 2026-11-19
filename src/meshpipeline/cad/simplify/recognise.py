# Responsibility: Find the features of a B-rep solid that a simplification could remove - holes, blends, slot ends - and price each by the mesh size the FEA plan would spend on it.
# Boundaries: geometry only, deterministic, no model; it reads a shape and returns candidates, and never edits, writes or stages anything.
# Collaborates with: cad/simplify/inspect.py (the entry point), devtools/simplify/harness.py (the bench this was measured on), engines/fea_plan (PLAN-fea-4, the hot-spot sizing rule the cost estimate uses).
"""HOLES, BLENDS AND SLOT ENDS, FROM THE B-REP.

A HOLE grows from a concave cylinder over the coaxial cylinders and cones touching it (a chamfer or
countersink at its mouth belongs to the hole), and is a hole only when its same-radius cylinders turn
a full circle. A BLEND is a torus or a cylinder spanning under a half turn, of radius under
BLEND_RATIO of the diagonal, grown over the blends of the same concavity touching it - one fillet run
along an edge loop is one feature. A concave cylinder spanning a half turn or more but not a full one
is a SLOT END, reported and never removable.

Measured on the NIST MBE PMI corpus (docs/simplification/DEVLOG.md): the slot-end class exists because
OpenCASCADE's BRepAlgoAPI_Defeaturing cannot remove a 180-degree concave round and returns the input
unchanged while reporting success; twelve of fifty-nine candidates on nist_ftc_09 were that.

Concavity is read from the face's own normal (BRepGProp_Face honours the face orientation): a
cylinder is concave when its outward normal points toward its axis, so the material is outside it.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np

from meshpipeline.cad.simplify.contracts import FeatureCandidate, FeatureKind

TWO_PI = 2.0 * math.pi
#: a blend is a round of radius under this fraction of the part diagonal
BLEND_RATIO = 0.03
#: PLAN-fea-4: a hot spot gets 6 elements per quarter-turn of its arc
ELEMENTS_PER_QUARTER_TURN = 6


@dataclass
class _Face:
    kind: str
    area: float
    centroid: tuple[float, float, float]
    radius: float = 0.0
    span: float = 0.0
    concave: bool = False
    axis: tuple = ()


def _faces(shape):
    from OCP.TopAbs import TopAbs_FACE
    from OCP.TopExp import TopExp
    from OCP.TopoDS import TopoDS
    from OCP.TopTools import TopTools_IndexedMapOfShape
    fmap = TopTools_IndexedMapOfShape()
    TopExp.MapShapes_s(shape, TopAbs_FACE, fmap)
    return [TopoDS.Face_s(fmap.FindKey(i)) for i in range(1, fmap.Extent() + 1)], fmap


def _adjacency(shape, fmap) -> list[set[int]]:
    from OCP.TopAbs import TopAbs_EDGE, TopAbs_FACE
    from OCP.TopExp import TopExp
    from OCP.TopTools import TopTools_IndexedDataMapOfShapeListOfShape
    em = TopTools_IndexedDataMapOfShapeListOfShape()
    TopExp.MapShapesAndAncestors_s(shape, TopAbs_EDGE, TopAbs_FACE, em)
    adj: list[set[int]] = [set() for _ in range(fmap.Extent())]
    for i in range(1, em.Extent() + 1):
        ids = [fmap.FindIndex(f) - 1 for f in em.FindFromIndex(i)]
        for a in ids:
            adj[a].update(b for b in ids if b != a)
    return adj


def _describe(face) -> _Face:
    from OCP.BRepAdaptor import BRepAdaptor_Surface
    from OCP.BRepGProp import BRepGProp, BRepGProp_Face
    from OCP.BRepTools import BRepTools
    from OCP.GeomAbs import GeomAbs_Cone, GeomAbs_Cylinder, GeomAbs_Plane, GeomAbs_Torus
    from OCP.gp import gp_Pnt, gp_Vec
    from OCP.GProp import GProp_GProps
    s = BRepAdaptor_Surface(face)
    t = s.GetType()
    g = GProp_GProps()
    BRepGProp.SurfaceProperties_s(face, g)
    c = g.CentreOfMass()
    kind = {GeomAbs_Plane: "plane", GeomAbs_Cylinder: "cyl", GeomAbs_Cone: "cone",
            GeomAbs_Torus: "torus"}.get(t, "other")
    info = _Face(kind, g.Mass(), (c.X(), c.Y(), c.Z()))
    if kind not in ("cyl", "cone", "torus"):
        return info
    u0, u1, v0, v1 = BRepTools.UVBounds_s(face)
    info.span = u1 - u0
    p, n = gp_Pnt(), gp_Vec()
    BRepGProp_Face(face).Normal(0.5 * (u0 + u1), 0.5 * (v0 + v1), p, n)
    pv, nv = np.array([p.X(), p.Y(), p.Z()]), np.array([n.X(), n.Y(), n.Z()])
    if kind in ("cyl", "cone"):
        geom = s.Cylinder() if kind == "cyl" else s.Cone()
        info.radius = geom.Radius() if kind == "cyl" else geom.RefRadius()
        ax = geom.Axis()
        o, d = ax.Location(), ax.Direction()
        ov, dv = np.array([o.X(), o.Y(), o.Z()]), np.array([d.X(), d.Y(), d.Z()])
        info.axis = (tuple(ov), tuple(dv))
        w = pv - ov
        info.concave = float(nv @ (w - (w @ dv) * dv)) < 0.0
    else:
        tor = s.Torus()
        info.radius = tor.MinorRadius()
        ax = tor.Axis()
        o, d = ax.Location(), ax.Direction()
        ov, dv = np.array([o.X(), o.Y(), o.Z()]), np.array([d.X(), d.Y(), d.Z()])
        w = pv - ov
        radial = w - (w @ dv) * dv
        rn = float(np.linalg.norm(radial))
        tube_centre = ov + (radial / rn * tor.MajorRadius() if rn > 0 else 0.0)
        info.concave = float(nv @ (tube_centre - pv)) > 0.0
    return info


def _coaxial(a: _Face, b: _Face, tol: float) -> bool:
    if not a.axis or not b.axis:
        return False
    (pa, da), (pb, db) = a.axis, b.axis
    da, db = np.asarray(da), np.asarray(db)
    if abs(abs(float(da @ db)) - 1.0) > 1e-6:
        return False
    w = np.asarray(pb) - np.asarray(pa)
    return float(np.linalg.norm(w - (w @ da) * da)) < tol


def _bbox(shapes) -> tuple[float, float, float, float, float, float]:
    from OCP.Bnd import Bnd_Box
    from OCP.BRepBndLib import BRepBndLib
    b = Bnd_Box()
    for s in shapes:
        BRepBndLib.Add_s(s, b)
    x0, y0, z0, x1, y1, z1 = (float(v) for v in b.Get())
    return (x0, y0, z0, x1, y1, z1)


def feature_size(radius: float) -> float:
    """The element edge PLAN-fea-4 asks of a round: a quarter-turn of arc over 6 elements."""
    return (0.5 * math.pi * radius) / ELEMENTS_PER_QUARTER_TURN


def surface_cells(area: float, h: float) -> int:
    """Equilateral triangles of edge h needed to cover the area: the feature's own surface cost."""
    return int(math.ceil(area / (math.sqrt(3.0) / 4.0 * h * h))) if h > 0 else 0


def recognise(shape) -> tuple[list[FeatureCandidate], float]:
    """Candidates and the part diagonal they are scaled against."""
    faces, fmap = _faces(shape)
    info = [_describe(f) for f in faces]
    adj = _adjacency(shape, fmap)
    box = _bbox([shape])
    diag = math.dist(box[:3], box[3:])
    tol = 1e-4 * diag
    used: set[int] = set()
    groups: list[tuple[FeatureKind, set[int], _Face]] = []

    for i, fi in enumerate(info):
        if i in used or fi.kind != "cyl" or not fi.concave:
            continue
        group, stack = {i}, [i]
        while stack:
            j = stack.pop()
            for k in adj[j]:
                fk = info[k]
                if k in group or k in used:
                    continue
                if fk.kind in ("cyl", "cone") and _coaxial(fi, fk, tol) and (fk.concave or fk.kind == "cone"):
                    group.add(k)
                    stack.append(k)
        span = sum(info[j].span for j in group if info[j].kind == "cyl" and abs(info[j].radius - fi.radius) < tol)
        if span >= TWO_PI - 1e-3:
            used |= group
            groups.append((FeatureKind.hole, group, fi))
        elif span >= math.pi - 1e-3:
            used |= group
            groups.append((FeatureKind.slot_end, group, fi))

    def is_blend(f: _Face) -> bool:
        return (f.kind == "torus" or (f.kind == "cyl" and f.span < math.pi - 1e-3)) and f.radius < BLEND_RATIO * diag

    for i, fi in enumerate(info):
        if i in used or not is_blend(fi):
            continue
        group, stack = {i}, [i]
        while stack:
            j = stack.pop()
            for k in adj[j]:
                if k not in group and k not in used and is_blend(info[k]) and info[k].concave == fi.concave:
                    group.add(k)
                    stack.append(k)
        used |= group
        groups.append((FeatureKind.blend, group, fi))

    out: list[FeatureCandidate] = []
    for n, (kind, group, seed) in enumerate(groups):
        ids = sorted(group)
        areas = [info[j].area for j in ids]
        area = float(sum(areas))
        cx, cy, cz = (float(v) for v in np.average([info[j].centroid for j in ids], axis=0,
                                                   weights=areas if area > 0 else None))
        centroid = (cx, cy, cz)
        radius = max(info[j].radius for j in ids) if kind is FeatureKind.blend else seed.radius
        h = feature_size(radius)
        out.append(FeatureCandidate(
            id=f"{kind.value}{n}", kind=kind, faces=tuple(j + 1 for j in ids), radius=float(radius),
            radius_ratio=float(radius / diag) if diag else 0.0, concave=bool(seed.concave), area=area,
            centroid=centroid, bbox=_bbox([faces[j] for j in ids]), feature_size=h,
            est_surface_cells=surface_cells(area, h), removable=kind is not FeatureKind.slot_end))
    return out, diag
