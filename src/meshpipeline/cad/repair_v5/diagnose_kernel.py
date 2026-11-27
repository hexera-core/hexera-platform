# Responsibility: The one kernel measurement v5 diagnosis adds: the geometry of each face pair near a failure site.
# Owns: `v5_pair_facts` (registered kernel fn) and the pure `pair_facts_of` it is built from.
# Boundaries: measures, never decides. The class rule lives in diagnose.py; the thresholds that say "coaxial" for
#             `split_aligned` are that op's own (ops/imprint.py), read here, not copied.
"""Face-pair geometry for defect diagnosis.

For each named face, every other face whose box comes within `max_gap_mm` of it and that does not share an edge with
it: the exact gap, the two surface types, the axis angle and offset when both are cylinders/cones, the plane-to-axis
tangency when one is a plane, the angle between the surface normals at the closest points, and the solid each face
belongs to. The partner face of a shaft-in-bore crossing is usually NOT a face any mesher named (gmsh names
one surface), so it is searched for here rather than taken from the site.
"""

from __future__ import annotations

import math
import time
from typing import Any

from meshpipeline.cad.repair_v4.kernel_calls import register
from meshpipeline.cad.repair_v4.ws_kernel import _maps, read_brep

CALL_BUDGET_S = 60.0
MAX_NAMED_FACES = 12
MAX_PARTNERS_PER_FACE = 6
_CURVED = ("cylinder", "cone")


def _kind(face: Any) -> str:
    from OCP.BRepAdaptor import BRepAdaptor_Surface

    return str(BRepAdaptor_Surface(face).GetType()).split(".")[-1].replace("GeomAbs_", "").lower()


def _axis_and_radius(face: Any) -> tuple[Any, float] | None:
    from OCP.BRepAdaptor import BRepAdaptor_Surface
    from OCP.GeomAbs import GeomAbs_Cone, GeomAbs_Cylinder

    s = BRepAdaptor_Surface(face)
    if s.GetType() == GeomAbs_Cylinder:
        return s.Cylinder().Position(), float(s.Cylinder().Radius())
    if s.GetType() == GeomAbs_Cone:
        return s.Cone().Position(), float(s.Cone().RefRadius())
    return None


def _plane(face: Any) -> Any | None:
    from OCP.BRepAdaptor import BRepAdaptor_Surface
    from OCP.GeomAbs import GeomAbs_Plane

    s = BRepAdaptor_Surface(face)
    return s.Plane() if s.GetType() == GeomAbs_Plane else None


def _normal_at(face: Any, point: Any) -> Any | None:
    """The face's outward-or-inward normal at the surface parameter nearest `point` (orientation is ignored later)."""
    from OCP.BRep import BRep_Tool
    from OCP.BRepGProp import BRepGProp_Face
    from OCP.gp import gp_Pnt, gp_Vec
    from OCP.ShapeAnalysis import ShapeAnalysis_Surface

    uv = ShapeAnalysis_Surface(BRep_Tool.Surface_s(face)).ValueOfUV(point, 1e-7)
    p, n = gp_Pnt(), gp_Vec()
    BRepGProp_Face(face).Normal(uv.X(), uv.Y(), p, n)
    return n if n.Magnitude() > 1e-12 else None


def _closest(a: Any, b: Any) -> dict:
    """Gap, closest points and the normal angle there (None unless both closest points are interior to a face)."""
    from OCP.BRepExtrema import BRepExtrema_DistShapeShape, BRepExtrema_IsInFace

    m = BRepExtrema_DistShapeShape(a, b)
    if not m.IsDone() or m.NbSolution() < 1:
        return {"gap_mm": None, "point_a": None, "point_b": None, "normal_angle_deg": None}
    pa, pb = m.PointOnShape1(1), m.PointOnShape2(1)
    angle = None
    if m.SupportTypeShape1(1) == BRepExtrema_IsInFace and m.SupportTypeShape2(1) == BRepExtrema_IsInFace:
        na, nb = _normal_at(a, pa), _normal_at(b, pb)
        if na is not None and nb is not None:
            raw = math.degrees(na.Angle(nb))
            angle = round(min(raw, 180.0 - raw), 4)   # orientation-free: 0 = parallel surfaces, 90 = crossing
    return {"gap_mm": round(float(m.Value()), 6),
            "point_a": [round(pa.X(), 6), round(pa.Y(), 6), round(pa.Z(), 6)],
            "point_b": [round(pb.X(), 6), round(pb.Y(), 6), round(pb.Z(), 6)],
            "normal_angle_deg": angle}


def pair_facts_of(a: Any, b: Any, diag: float) -> dict:
    """The measured geometry of one face pair (both TopoDS_Face)."""
    from OCP.gp import gp_Lin

    from meshpipeline.cad.repair_v4.ops.imprint import COAXIAL_ANGLE_RAD, COAXIAL_FRACTION

    out: dict = {"surface_a": _kind(a), "surface_b": _kind(b), **_closest(a, b)}
    ax_a, ax_b = _axis_and_radius(a), _axis_and_radius(b)
    if ax_a and ax_b:
        da, db = ax_a[0].Direction(), ax_b[0].Direction()
        ang = min(da.Angle(db), da.Angle(db.Reversed()))
        offset = gp_Lin(ax_b[0].Axis()).Distance(ax_a[0].Location())
        out.update(radius_a_mm=round(ax_a[1], 6), radius_b_mm=round(ax_b[1], 6),
                   axis_angle_deg=round(math.degrees(ang), 6), axis_offset_mm=round(offset, 6),
                   # exactly the test split_aligned applies (ops/imprint._coaxial): it declines anything else
                   split_aligned_coaxial=bool(ang <= COAXIAL_ANGLE_RAD and offset <= COAXIAL_FRACTION * diag))
    else:
        curved, flat = (ax_a, _plane(b)) if ax_a else (ax_b, _plane(a))
        if curved and flat is not None:
            # near-tangent: the plane runs parallel to the axis at about one radius from it
            axis, radius = curved
            normal = flat.Axis().Direction()
            tilt = abs(90.0 - math.degrees(normal.Angle(axis.Direction())))
            out.update(radius_mm=round(radius, 6), plane_axis_tilt_deg=round(tilt, 6),
                       plane_axis_gap_mm=round(abs(flat.Distance(axis.Location()) - radius), 6))
    return out


def _shares_edge(a: Any, b: Any) -> bool:
    from OCP.TopAbs import TopAbs_EDGE
    from OCP.TopExp import TopExp
    from OCP.TopTools import TopTools_IndexedMapOfShape

    ea, eb = TopTools_IndexedMapOfShape(), TopTools_IndexedMapOfShape()
    TopExp.MapShapes_s(a, TopAbs_EDGE, ea)
    TopExp.MapShapes_s(b, TopAbs_EDGE, eb)
    return any(eb.Contains(ea.FindKey(i)) for i in range(1, ea.Extent() + 1))


def _solid_of(maps: dict) -> dict[int, int]:
    """face index -> solid index (the first solid that holds it)."""
    from OCP.TopAbs import TopAbs_FACE
    from OCP.TopExp import TopExp
    from OCP.TopTools import TopTools_IndexedMapOfShape

    out: dict[int, int] = {}
    for s in range(1, maps["solid"].Extent() + 1):
        faces = TopTools_IndexedMapOfShape()
        TopExp.MapShapes_s(maps["solid"].FindKey(s), TopAbs_FACE, faces)
        for i in range(1, faces.Extent() + 1):
            idx = maps["face"].FindIndex(faces.FindKey(i))
            if idx and idx not in out:
                out[idx] = s
    return out


@register("v5_pair_facts")
def v5_pair_facts(payload: dict) -> dict:
    """`{"pairs": [{a, b, solid_a, solid_b, ...pair_facts_of}], "truncated"}` for each named face index (`faces`)
    against every face within `max_gap_mm` of it, nearest first per face. Indices are kernel face indices."""
    from OCP.Bnd import Bnd_Box
    from OCP.BRepBndLib import BRepBndLib
    from OCP.TopoDS import TopoDS

    deadline = time.monotonic() + CALL_BUDGET_S
    shape = read_brep(payload["brep_path"])
    maps = _maps(shape)
    faces = maps["face"]
    limit = float(payload["max_gap_mm"])
    whole = Bnd_Box()
    BRepBndLib.Add_s(shape, whole)
    x0, y0, z0, x1, y1, z1 = whole.Get()
    diag = math.dist((x0, y0, z0), (x1, y1, z1))
    boxes = []
    for i in range(1, faces.Extent() + 1):
        box = Bnd_Box()
        BRepBndLib.Add_s(faces.FindKey(i), box, False)
        box.Enlarge(limit)
        boxes.append(box)
    solid = _solid_of(maps)
    out: list[dict] = []
    seen: set[tuple[int, int]] = set()
    truncated = False
    for a in [int(x) for x in payload["faces"]][:MAX_NAMED_FACES]:
        if not 1 <= a <= faces.Extent():
            continue
        # DOWNCAST (v5 step 7, measured on jig): the map hands back a TopoDS_Shape, BRepAdaptor_Surface takes only a
        # TopoDS_Face, and without the cast this function raised TypeError on every real part - so diagnosis never
        # saw a close pair, and every close_faces defect was classed from an empty pair list
        fa = TopoDS.Face_s(faces.FindKey(a))
        found = []
        for b in range(1, faces.Extent() + 1):
            if b == a or (min(a, b), max(a, b)) in seen or boxes[a - 1].IsOut(boxes[b - 1]):
                continue
            if time.monotonic() > deadline:
                truncated = True
                break
            fb = TopoDS.Face_s(faces.FindKey(b))
            if _shares_edge(fa, fb):
                continue   # legitimate neighbours, not a defect
            facts = pair_facts_of(fa, fb, diag)
            if facts["gap_mm"] is None or facts["gap_mm"] > limit:
                continue
            seen.add((min(a, b), max(a, b)))
            found.append({"a": a, "b": b, "solid_a": solid.get(a), "solid_b": solid.get(b), **facts})
        found.sort(key=lambda p: (p["gap_mm"], p["b"]))
        out += found[:MAX_PARTNERS_PER_FACE]
    return {"pairs": out, "truncated": truncated, "diagonal_mm": round(diag, 6)}
