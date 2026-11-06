# Responsibility: Measure a part for structural analysis (FEA build step 3): how many bodies it has,
# how thick its walls are under every face, and its stress hot spots - holes, fillets, notches and
# grooves with their radii, sharp inside corners with their angle - each with a place, a type and a
# size, as the record the geometry check keeps in its facts for the FEA plan (step 4).
# Boundaries: geometry only, deterministic, no model call, no persistence. A CAD file is read on its
# exact faces (OpenCASCADE): Face_k is the k-th CAD face, the same numbering the FEA deck writes
# (engines/gmsh/abaqus_deck.py). A triangle file has no CAD faces, so its smooth regions are
# measured instead (Region_k), and its radii are fitted. It plans nothing: what each hot spot gets
# is step 4's.
# Collaborates with: cad/fea_features.py (the measurements on triangles), application/
# geometry_check.py (runs it for an FEA part and stores the record), application/fea_check.py (the
# stage's view of it, and the record in the confirmed unit).
"""THE FEA MEASUREMENT RECORD (facts["fea_measure"]).

Lengths are millimetres under the unit the geometry check read the file in (`scale_to_m` says
which); application/fea_check.in_unit re-reads them in the unit the user confirmed.

    {"version": 1, "basis": "cad" | "triangles", "unit": "mm", "scale_to_m": 0.001,
     "bodies": 1, "face_names": "Face_k" | "Region_k",
     "faces": {"Face_1": {"kind", "area_mm2", "centroid_mm", "thickness_min_mm",
                          "thickness_median_mm", "radius_mm", "concave"}, ...},
     "thickness": {"min_mm", "face", "at_mm", "rays", "method"},
     "hot_spots": [{"id": "H1", "type": "hole" | "fillet" | "notch" | "sharp_corner",
                    "shape": "hole" | "fillet" | "notch" | "groove" | "inside corner",
                    "faces": ["Face_7"], "at_mm": [x, y, z], "radius_mm" | "angle_deg", ...,
                    "local_thickness_mm", "capped": bool}, ...],
     "counts": {"hole": 1, ...}, "complete": true, "notes": [...], "seconds": 0.4}
"""
from __future__ import annotations

import logging
import math
import time
from pathlib import Path

import numpy as np

from meshpipeline.cad import fea_features as ff

logger = logging.getLogger(__name__)

#: 2 (21 Oct): a wall is read inside one body, past the model's floor, never across a taper,
#: and only where a face's rays agree (cad/fea_features.wall_reach); 1 read the first
#: surface any ray met.
VERSION = 4
#: 3 (21 Oct): the record lists the CAD's cusps (cad_cusps, PLAN-fea-10).
#: 4 (4 Nov): ...and its run-outs, a face collapsing to a point (cad_cusps, kind "run_out").
#: Where the record lives in the geometry check's facts.
FACT_KEY = "fea_measure"
#: The CAD surface is tessellated for the measurement at a chord of this fraction of the part's
#: diagonal and this angle (radians, about 9 degrees): a 20 mm hole gets about 40 sides.
CHORD_FRACTION = 1.0 / 3000.0
ANGULAR_DEFLECTION = 0.15
#: Past this many triangles the walls are not ray-traced (the record says so); hot spots still are.
MAX_RAY_TRIANGLES = 600_000
#: The words a hot spot's shape is said in.
SHAPE_WORDS = {"hole": "hole", "fillet": "fillet", "notch": "notch", "groove": "groove",
               ff.SHARP_CORNER: "inside corner"}
_TYPE_ORDER = {ff.SHARP_CORNER: 0, ff.NOTCH: 1, ff.FILLET: 2, ff.HOLE: 3}
#: At most this many contacts are listed (the longest); `contact_count` says how many there are.
MAX_CONTACTS = 200


def _r(v: float) -> float:
    """Six significant figures: a micron on a metre-long part, a nanometre on a millimetre one."""
    v = float(v)
    return float(f"{v:.6g}") if math.isfinite(v) else v


def _pt(p) -> list[float]:
    return [_r(x) for x in np.asarray(p, dtype=float)]


# ------------------------------------------------------------------------------------ CAD ----
def measure_cad_file(path: Path | str, *, mm_per_unit: float) -> dict:
    """Read a STEP or IGES file and measure it (see measure_cad)."""
    from meshpipeline.cad.scout import read_cad

    return measure_cad(read_cad(Path(path)), mm_per_unit=mm_per_unit)


def measure_cad(shape, *, mm_per_unit: float) -> dict:
    """Measure an OpenCASCADE shape whose coordinates are `mm_per_unit` millimetres each. The
    shape is tessellated in place (it is the caller's read; nothing else is changed)."""
    return measure_cad_surface(shape, mm_per_unit=mm_per_unit)[0]


def measure_cad_surface(shape, *, mm_per_unit: float,
                        max_ray_triangles: int = MAX_RAY_TRIANGLES) -> tuple[dict, ff.Surface]:
    """measure_cad, and the labelled triangle surface it measured (millimetres, label k is
    Face_{k+1}): the FEA plan (engines/fea_*.py) sizes its zones on the same triangles. The plan
    measures the walls of a larger surface than the geometry check does (`max_ray_triangles`): it
    cannot hold walls to a rule it did not measure."""
    started = time.time()
    surface, faces, kinds = _brep_surface(shape, float(mm_per_unit))
    arcs = []
    for k, face in enumerate(faces):
        idx = np.flatnonzero(surface.label == k)
        arc = _face_arc(face, kinds[k], surface, idx, float(mm_per_unit))
        if arc is not None:
            arcs.append(arc)
    # EVERY CURVED FACE'S OWN CURVATURE (PLAN-fea-7): a swept wire, a spring's coil, a blended
    # free-form face is round however its CAD writes it - read from the surface itself
    curvature = {k: r for k, face in enumerate(faces)
                 if kinds[k] not in ("plane",) and (r := face_radius(face, float(mm_per_unit))) is not None}
    bodies, sewn = _cad_bodies(shape), None
    if bodies == 0:
        # A FILE OF LOOSE SURFACES (an IGES of trimmed faces, no shell, no solid): its bodies are COUNTED
        # as the pieces its faces sew into (sewn_bodies), said as `bodies_sewn` - `bodies` stays the
        # solids' count, which an assembly's staging and fuse are built on. The count is not used to split
        # the walls: a face the sewing leaves loose would read its own part's far side as another body's
        groups = sewn_bodies(shape, surface, float(mm_per_unit))
        if groups is not None:
            sewn = groups[1]
    record = _record(surface, arcs, bodies=bodies, basis="cad", kinds=kinds, started=started,
                     max_ray_triangles=max_ray_triangles, curvature=curvature)
    if sewn is not None:
        record["bodies_sewn"] = int(sewn)
    record["cusps"] = cad_cusps(shape, float(mm_per_unit))
    record["micro"] = cad_micro(shape, float(mm_per_unit), diag_mm=float(surface.diag))
    # PLAN-fea-12: the rounds under the measure floor are micro details too, named (no places to set aside)
    record["micro"] += [{**m, "points_mm": []} for m in record.pop("sub_floor", [])]
    # FOLD-BACK EDGES (cad/fold_back.py): two faces lying on each other along an edge - detected only
    from meshpipeline.cad.fold_back import fold_back_edges
    folds = fold_back_edges(shape, mm_per_unit=float(mm_per_unit))
    record["fold_back"] = [{**f, "faces": [f"Face_{k}" for k in f["faces"]]} for f in folds["folds"]]
    record["fold_back_complete"] = bool(folds["complete"])
    return record, surface


#: PLAN-fea-7's rounds read from curvature stop at this share of the part's diagonal: a gentler bend
#: is meshed by the base size already (its elements per quarter-turn need no zone of their own).
ROUND_CAP_SHARE = 0.25
#: MICRO DETAILS are recorded when an edge is shorter, or a face narrower, than this fraction of the
#: part's diagonal; the plan keeps those far below its own element size (PLAN-fea-12).
MICRO_FRACTION = 1e-3
#: Each micro detail is described by points this far apart along it (as a fraction of its length),
#: so a long thin strip is marked along its whole length, not only at its ends.
MICRO_SAMPLES = 24


def cad_micro(shape, mm: float, *, diag_mm: float) -> list[dict]:
    """The CAD's micro details: edges shorter than MICRO_FRACTION of the part, and faces narrower than
    that (twice the area over the perimeter) - a 0.004 mm edge on a screw's socket, the 0.001 mm flat
    strips a sprocket's teeth were drawn with. Each: kind, size (mm), the faces it touches (Face_k)
    and points along it (mm). The plan decides which are far below its element size."""
    from OCP.BRep import BRep_Tool
    from OCP.BRepAdaptor import BRepAdaptor_Curve
    from OCP.BRepGProp import BRepGProp
    from OCP.GProp import GProp_GProps
    from OCP.TopAbs import TopAbs_EDGE, TopAbs_FACE
    from OCP.TopExp import TopExp, TopExp_Explorer
    from OCP.TopoDS import TopoDS
    from OCP.TopTools import TopTools_IndexedDataMapOfShapeListOfShape, TopTools_IndexedMapOfShape

    limit = MICRO_FRACTION * float(diag_mm) / mm          # in the file's own unit
    fmap = TopTools_IndexedMapOfShape()
    TopExp.MapShapes_s(shape, TopAbs_FACE, fmap)
    e2f = TopTools_IndexedDataMapOfShapeListOfShape()
    TopExp.MapShapesAndAncestors_s(shape, TopAbs_EDGE, TopAbs_FACE, e2f)

    def length(e) -> float:
        g = GProp_GProps()
        BRepGProp.LinearProperties_s(e, g)
        return float(g.Mass())

    def along(e) -> list[list[float]]:
        if BRep_Tool.Degenerated_s(e):
            return []
        c = BRepAdaptor_Curve(e)
        u0, u1 = c.FirstParameter(), c.LastParameter()
        return [_pt(np.array([(q := c.Value(u0 + (u1 - u0) * k / (MICRO_SAMPLES - 1))).X(), q.Y(), q.Z()]) * mm)
                for k in range(MICRO_SAMPLES)]

    out: list[dict] = []
    for i in range(1, e2f.Extent() + 1):
        e = TopoDS.Edge_s(e2f.FindKey(i))
        if BRep_Tool.Degenerated_s(e):
            continue
        L = length(e)
        if L < limit:
            faces = sorted({fmap.FindIndex(f) for f in e2f.FindFromIndex(i)} - {0})
            out.append({"kind": "edge", "size_mm": _r(L * mm), "faces": [f"Face_{k}" for k in faces],
                        "points_mm": along(e)})
    for k in range(1, fmap.Extent() + 1):
        f = TopoDS.Face_s(fmap.FindKey(k))
        g = GProp_GProps()
        BRepGProp.SurfaceProperties_s(f, g)
        area = float(g.Mass())
        per, pts = 0.0, []
        ex = TopExp_Explorer(f, TopAbs_EDGE)
        edges = []
        while ex.More():
            edges.append(TopoDS.Edge_s(ex.Current()))
            ex.Next()
        per = sum(length(e) for e in edges)
        if per > 0 and 2.0 * area / per < limit:
            for e in edges:
                pts += along(e)
            out.append({"kind": "face", "size_mm": _r(2.0 * area / per * mm), "faces": [f"Face_{k}"],
                        "points_mm": pts})
    return out


#: A CUSP: two edges of one face leave a vertex within this many degrees of each other (a bore or a
#: boss just touching a straight face; a nut's chamfer cone meeting its flats). The face - and the
#: material beside it - runs to nothing there, so no element there can be well shaped at any size.
CUSP_DEG = 5.0
#: A RUN-OUT is a cusp too: a face whose boundary collapses to a point - a degenerated (zero-length)
#: edge - where a fillet or blend narrows to nothing against a bore or a pin (the punch's links, 4
#: Nov: an N-sided blend patch ending at 15 degrees, its elements jumping from 0.3 mm to 1 mm). A
#: sphere's, cone's or revolved face's collapsed edge is its POLE, where the surface is smooth and
#: nothing runs out: those surfaces are left out.
POLE_SURFACES = ("Sphere", "Cone", "SurfaceOfRevolution")


def cad_cusps(shape, mm: float) -> list[dict]:
    """Every cusp of the CAD solid (PLAN-fea-10): where, in millimetres, the angle between the two
    edges, and the faces they bound (Face_k, OpenCASCADE's order as everywhere here)."""
    from OCP.BRep import BRep_Tool
    from OCP.BRepAdaptor import BRepAdaptor_Curve
    from OCP.gp import gp_Pnt, gp_Vec
    from OCP.TopAbs import TopAbs_EDGE, TopAbs_FACE, TopAbs_VERTEX
    from OCP.TopExp import TopExp
    from OCP.TopoDS import TopoDS
    from OCP.TopTools import TopTools_IndexedDataMapOfShapeListOfShape, TopTools_IndexedMapOfShape

    fmap = TopTools_IndexedMapOfShape()
    TopExp.MapShapes_s(shape, TopAbs_FACE, fmap)
    v2e = TopTools_IndexedDataMapOfShapeListOfShape()
    TopExp.MapShapesAndAncestors_s(shape, TopAbs_VERTEX, TopAbs_EDGE, v2e)
    e2f = TopTools_IndexedDataMapOfShapeListOfShape()
    TopExp.MapShapesAndAncestors_s(shape, TopAbs_EDGE, TopAbs_FACE, e2f)
    out: list[dict] = []
    for i in range(1, v2e.Extent() + 1):
        v = TopoDS.Vertex_s(v2e.FindKey(i))
        p = BRep_Tool.Pnt_s(v)
        legs = []
        seen: list = []
        for e_shape in v2e.FindFromIndex(i):
            e = TopoDS.Edge_s(e_shape)
            if BRep_Tool.Degenerated_s(e) or any(e.IsSame(s) for s in seen):
                continue
            seen.append(e)
            try:
                c = BRepAdaptor_Curve(e)
                u = BRep_Tool.Parameter_s(v, e)
                q, d = gp_Pnt(), gp_Vec()
                c.D1(u, q, d)
            except Exception as exc:  # noqa: BLE001 - an edge we cannot read is no evidence of a cusp
                logger.debug("fea measure: an edge's tangent could not be read for the cusp check (%s)", exc)
                continue
            if d.Magnitude() <= 0:
                continue
            first = abs(u - c.FirstParameter()) <= abs(u - c.LastParameter())
            t = np.array([d.X(), d.Y(), d.Z()]) / d.Magnitude() * (1.0 if first else -1.0)
            k = e2f.FindIndex(e)
            faces = {fmap.FindIndex(f) for f in e2f.FindFromIndex(k)} if k else set()
            legs.append((t, faces))
        for a in range(len(legs)):
            for b in range(a + 1, len(legs)):
                common = legs[a][1] & legs[b][1]
                if not common:
                    continue
                ang = math.degrees(math.acos(max(-1.0, min(1.0, float(np.dot(legs[a][0], legs[b][0]))))))
                if ang < CUSP_DEG:
                    out.append({"at_mm": _pt(np.array([p.X(), p.Y(), p.Z()]) * mm), "angle_deg": _r(ang),
                                "faces": [f"Face_{k}" for k in sorted(legs[a][1] | legs[b][1]) if k > 0]})
    out += _run_outs(shape, mm, fmap)
    # one entry per place (a vertex can host more than one pair)
    unique: dict[tuple, dict] = {}
    for c in out:
        unique.setdefault(tuple(round(x, 4) for x in c["at_mm"]), c)
    return list(unique.values())


def _run_outs(shape, mm: float, fmap) -> list[dict]:
    """Every RUN-OUT of the solid (PLAN-fea-10): a vertex where a face's degenerated edge collapses
    its boundary to a point, on a face that is not a pole surface (POLE_SURFACES), with two real
    edges of the face leaving it. Same entries as cad_cusps, with "kind": "run_out"."""
    from OCP.BRep import BRep_Tool
    from OCP.BRepAdaptor import BRepAdaptor_Curve, BRepAdaptor_Surface
    from OCP.gp import gp_Pnt, gp_Vec
    from OCP.TopAbs import TopAbs_EDGE
    from OCP.TopExp import TopExp, TopExp_Explorer
    from OCP.TopoDS import TopoDS

    def leaving(e, v):
        c = BRepAdaptor_Curve(e)
        u = BRep_Tool.Parameter_s(v, e)
        q, d = gp_Pnt(), gp_Vec()
        c.D1(u, q, d)
        if d.Magnitude() <= 0:
            return None
        first = abs(u - c.FirstParameter()) <= abs(u - c.LastParameter())
        return np.array([d.X(), d.Y(), d.Z()]) / d.Magnitude() * (1.0 if first else -1.0)

    out: list[dict] = []
    for i in range(1, fmap.Extent() + 1):
        face = TopoDS.Face_s(fmap.FindKey(i))
        edges = []
        ex = TopExp_Explorer(face, TopAbs_EDGE)
        while ex.More():
            edges.append(TopoDS.Edge_s(ex.Current()))
            ex.Next()
        degenerate = [e for e in edges if BRep_Tool.Degenerated_s(e)]
        if not degenerate:
            continue
        try:
            kind = str(BRepAdaptor_Surface(face).GetType()).rsplit(".", 1)[-1].replace("GeomAbs_", "")
        except Exception as exc:  # noqa: BLE001 - a face we cannot read is no evidence of a run-out
            logger.debug("fea measure: a face's surface could not be read for the run-out check (%s)", exc)
            continue
        if kind in POLE_SURFACES:
            continue
        for d in degenerate:
            v = TopExp.FirstVertex_s(d)
            real: list = []
            for e in edges:
                if BRep_Tool.Degenerated_s(e) or any(e.IsSame(r) for r in real):
                    continue
                if TopExp.FirstVertex_s(e).IsSame(v) or TopExp.LastVertex_s(e).IsSame(v):
                    real.append(e)
            if len(real) < 2:
                continue                 # a seam through a pole, not a face narrowing to a point
            try:
                t = [leaving(e, v) for e in real[:2]]
            except Exception as exc:  # noqa: BLE001 - an edge we cannot read is no evidence
                logger.debug("fea measure: a run-out's edges could not be read (%s)", exc)
                continue
            if t[0] is None or t[1] is None:
                continue
            p = BRep_Tool.Pnt_s(v)
            ang = math.degrees(math.acos(max(-1.0, min(1.0, float(np.dot(t[0], t[1]))))))
            out.append({"at_mm": _pt(np.array([p.X(), p.Y(), p.Z()]) * mm), "angle_deg": _r(ang),
                        "faces": [f"Face_{i}"], "kind": "run_out"})
    return out


def _brep_surface(shape, mm: float):
    """The shape's faces as one labelled triangle surface in millimetres: label k is Face_{k+1}, in
    OpenCASCADE's face order (TopExp.MapShapes) - the order gmsh numbers the faces of a solid it
    imports, so Face_k here is Face_k in the delivered deck."""
    from OCP.BRep import BRep_Tool
    from OCP.BRepAdaptor import BRepAdaptor_Surface
    from OCP.TopAbs import TopAbs_FACE, TopAbs_REVERSED
    from OCP.TopExp import TopExp
    from OCP.TopLoc import TopLoc_Location
    from OCP.TopoDS import TopoDS
    from OCP.TopTools import TopTools_IndexedMapOfShape

    from meshpipeline.cad.occ_box import mesh_to_size

    mesh_to_size(shape, CHORD_FRACTION, ANGULAR_DEFLECTION)
    fmap = TopTools_IndexedMapOfShape()
    TopExp.MapShapes_s(shape, TopAbs_FACE, fmap)
    faces, kinds, blocks, tri_blocks, labels = [], [], [], [], []
    defl = np.zeros(fmap.Extent())
    base = 0
    for i in range(1, fmap.Extent() + 1):
        face = TopoDS.Face_s(fmap.FindKey(i))
        faces.append(face)
        kinds.append(_kind_word(BRepAdaptor_Surface(face, True).GetType()))
        loc = TopLoc_Location()
        tri = BRep_Tool.Triangulation_s(face, loc)
        if tri is None or tri.NbTriangles() == 0:
            continue
        # how far this face's triangles lie from its CAD surface (cad/fea_features COINCIDENT_BAND)
        defl[i - 1] = max(0.0, float(tri.Deflection())) * mm
        trsf = loc.Transformation()
        nodes = []
        for k in range(1, tri.NbNodes() + 1):
            p = tri.Node(k).Transformed(trsf)
            nodes.append((p.X(), p.Y(), p.Z()))
        flip = face.Orientation() == TopAbs_REVERSED
        t = np.empty((tri.NbTriangles(), 3), dtype=np.int64)
        for k in range(1, tri.NbTriangles() + 1):
            g = tri.Triangle(k)
            a, b, c = g.Value(1), g.Value(2), g.Value(3)
            t[k - 1] = (a, c, b) if flip else (a, b, c)
        blocks.append(np.asarray(nodes, dtype=float) * mm)
        tri_blocks.append(t - 1 + base)
        labels.append(np.full(len(t), i - 1, dtype=np.int64))
        base += len(nodes)
    if not blocks:
        raise ValueError("the CAD faces could not be tessellated")
    xyz, tris = _weld(np.vstack(blocks), np.vstack(tri_blocks))
    label = np.concatenate(labels)
    keep = (tris[:, 0] != tris[:, 1]) & (tris[:, 1] != tris[:, 2]) & (tris[:, 0] != tris[:, 2])
    names = [f"Face_{k}" for k in range(1, len(faces) + 1)]
    # which solid each face belongs to, and the face's own tolerance: a wall is read inside one
    # body, and never thinner than the model itself is sure of (cad/fea_features.wall_reach)
    tol = np.array([BRep_Tool.Tolerance_s(f) for f in faces], dtype=float) * mm
    surface = ff.Surface(xyz=xyz, tris=tris[keep], label=label[keep], names=names,
                         body=_face_bodies(shape, fmap), tol_mm=tol, defl_mm=defl, tapers=_brep_tapers(shape, fmap))
    return surface, faces, kinds


def _brep_tapers(shape, fmap) -> list[tuple[int, int]]:
    """The face pairs (0-based, fmap order, a < b) that share a CAD edge at an ACUTE angle: their outward
    normals at the edge's middle turn by more than 180 - TAPER_DEG (cad/fea_features.TAPER_DEG). Never
    raises: an edge that cannot be read is no taper."""
    import math

    from OCP.BRep import BRep_Tool
    from OCP.BRepAdaptor import BRepAdaptor_Curve, BRepAdaptor_Surface
    from OCP.BRepLProp import BRepLProp_SLProps
    from OCP.ShapeAnalysis import ShapeAnalysis_Surface
    from OCP.TopAbs import TopAbs_EDGE, TopAbs_FACE, TopAbs_REVERSED
    from OCP.TopExp import TopExp
    from OCP.TopoDS import TopoDS
    from OCP.TopTools import TopTools_IndexedDataMapOfShapeListOfShape

    out: set[tuple[int, int]] = set()
    limit = math.cos(math.radians(180.0 - ff.TAPER_DEG))
    try:
        e2f = TopTools_IndexedDataMapOfShapeListOfShape()
        TopExp.MapShapesAndAncestors_s(shape, TopAbs_EDGE, TopAbs_FACE, e2f)
        for i in range(1, e2f.Extent() + 1):
            faces = list(e2f.FindFromIndex(i))
            ks = sorted({fmap.FindIndex(f) - 1 for f in faces})
            if len(ks) != 2 or min(ks) < 0:
                continue
            e = TopoDS.Edge_s(e2f.FindKey(i))
            if BRep_Tool.Degenerated_s(e):
                continue
            c = BRepAdaptor_Curve(e)
            p = c.Value(0.5 * (c.FirstParameter() + c.LastParameter()))
            ns = []
            for k in ks:
                f = TopoDS.Face_s(fmap.FindKey(k + 1))
                uv = ShapeAnalysis_Surface(BRep_Tool.Surface_s(f)).ValueOfUV(p, 1e-9)
                pr = BRepLProp_SLProps(BRepAdaptor_Surface(f), uv.X(), uv.Y(), 1, 1e-9)
                if not pr.IsNormalDefined():
                    break
                n = pr.Normal()
                sign = -1.0 if f.Orientation() == TopAbs_REVERSED else 1.0
                ns.append((sign * n.X(), sign * n.Y(), sign * n.Z()))
            if len(ns) == 2 and sum(a * b for a, b in zip(ns[0], ns[1], strict=True)) < limit:
                out.add((ks[0], ks[1]))
    except Exception:  # noqa: BLE001 - the triangles' own tapers stand
        return sorted(out)
    return sorted(out)


def _face_bodies(shape, fmap) -> np.ndarray:
    """The body (solid, else shell) each face of `fmap` belongs to, in fmap's order; 0 for all when
    the shape has neither."""
    from OCP.TopAbs import TopAbs_FACE, TopAbs_SHELL, TopAbs_SOLID
    from OCP.TopExp import TopExp, TopExp_Explorer
    from OCP.TopTools import TopTools_IndexedMapOfShape

    body = np.zeros(fmap.Extent(), dtype=np.int64)
    for what in (TopAbs_SOLID, TopAbs_SHELL):
        m = TopTools_IndexedMapOfShape()
        TopExp.MapShapes_s(shape, what, m)
        if not m.Extent():
            continue
        for b in range(1, m.Extent() + 1):
            ex = TopExp_Explorer(m.FindKey(b), TopAbs_FACE)
            while ex.More():
                k = fmap.FindIndex(ex.Current())
                if k > 0:
                    body[k - 1] = b - 1
                ex.Next()
        break
    return body


def _weld(xyz: np.ndarray, tris: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """One vertex where several faces' tessellations put the same point (their shared edges).
    Exact repeats go first (a triangle soup repeats a vertex once per triangle: a fan of 20,000
    round one point would otherwise be 200 million near pairs); only the distinct points are then
    joined within a hair of each other."""
    from scipy.spatial import cKDTree

    exact, back = np.unique(xyz, axis=0, return_inverse=True)
    back = back.reshape(-1)
    diag = float(np.linalg.norm(exact.max(axis=0) - exact.min(axis=0))) or 1.0
    pairs = cKDTree(exact).query_pairs(1e-7 * diag, output_type="ndarray")
    rep = np.arange(len(exact))
    if len(pairs):
        from scipy.sparse import coo_matrix
        from scipy.sparse.csgraph import connected_components

        g = coo_matrix((np.ones(len(pairs)), (pairs[:, 0], pairs[:, 1])), shape=(len(exact), len(exact)))
        _n, comp = connected_components(g, directed=False)
        _u, first = np.unique(comp, return_index=True)
        rep = first[comp]
    used, inverse = np.unique(rep, return_inverse=True)
    return exact[used], inverse[back][tris]


#: A face's curvature is read on a CURVATURE_SAMPLES x CURVATURE_SAMPLES grid of its parameters;
#: its radius is the one CURVATURE_QUANTILE of the readings bend tighter than (the face's tight
#: side, not one stray reading at a seam).
CURVATURE_SAMPLES = 9
CURVATURE_QUANTILE = 0.75


def face_radius(face, mm: float) -> float | None:
    """The face's own radius of curvature (mm): from its principal curvatures sampled over its
    parameters (OpenCASCADE's BRepLProp_SLProps), the tighter of the two at each sample. None for a
    face that does not bend (a plane, a ruled flat) or whose curvature cannot be read."""
    from OCP.BRepAdaptor import BRepAdaptor_Surface
    from OCP.BRepLProp import BRepLProp_SLProps

    try:
        a = BRepAdaptor_Surface(face, True)
        u0, u1, v0, v1 = a.FirstUParameter(), a.LastUParameter(), a.FirstVParameter(), a.LastVParameter()
        if not all(math.isfinite(x) for x in (u0, u1, v0, v1)):
            return None
        ks = []
        n = CURVATURE_SAMPLES
        for i in range(n):
            for j in range(n):
                u = u0 + (u1 - u0) * (i + 0.5) / n
                v = v0 + (v1 - v0) * (j + 0.5) / n
                props = BRepLProp_SLProps(a, u, v, 2, 1e-9)
                if props.IsCurvatureDefined():
                    k = max(abs(props.MaxCurvature()), abs(props.MinCurvature()))
                    if math.isfinite(k):
                        ks.append(k)
    except Exception:  # noqa: BLE001 - a face whose curvature cannot be read keeps the other rules
        return None
    if not ks:
        return None
    k = float(np.quantile(np.asarray(ks), CURVATURE_QUANTILE))
    if k <= 1e-12:
        return None
    return _r(mm / k)


def _kind_word(t) -> str:
    name = str(t).rsplit(".", 1)[-1].replace("GeomAbs_", "").lower()
    return {"bsplinesurface": "bspline", "beziersurface": "bezier", "surfaceofrevolution": "revolution",
            "surfaceofextrusion": "extrusion", "offsetsurface": "offset", "othersurface": "other"}.get(name, name)


def _face_arc(face, kind: str, s: ff.Surface, idx: np.ndarray, mm: float) -> ff.Arc | None:
    """A face's round, exact when the CAD says it is a cylinder or a torus; fitted to its
    triangles when it is free-form (many CAD systems write fillets as B-splines)."""
    if kind in ("plane", "cone", "sphere") or not len(idx):
        return None
    if kind not in ("cylinder", "torus"):
        arc = ff.fit_round(*ff.face_samples(s, idx), s.diag)
        if arc is not None:
            arc.labels = {int(s.label[idx[0]])}
        return arc
    from OCP.BRepAdaptor import BRepAdaptor_Surface
    from OCP.BRepGProp import BRepGProp_Face
    from OCP.gp import gp_Pnt, gp_Vec

    a = BRepAdaptor_Surface(face, True)
    u0, u1, v0, v1 = a.FirstUParameter(), a.LastUParameter(), a.FirstVParameter(), a.LastVParameter()
    p, n = gp_Pnt(), gp_Vec()
    BRepGProp_Face(face).Normal(0.5 * (u0 + u1), 0.5 * (v0 + v1), p, n)
    P = np.array([p.X(), p.Y(), p.Z()]) * mm
    N = np.array([n.X(), n.Y(), n.Z()])
    verts = s.xyz[np.unique(s.tris[idx])]
    at = s.centroids[idx][int(np.argmin(np.linalg.norm(
        s.centroids[idx] - np.average(s.centroids[idx], axis=0, weights=np.maximum(s.areas[idx], 1e-300)), axis=1)))]
    label = {int(s.label[idx[0]])}
    if kind == "cylinder":
        cyl = a.Cylinder()
        ax = cyl.Axis()
        o = np.array([ax.Location().X(), ax.Location().Y(), ax.Location().Z()]) * mm
        d = np.array([ax.Direction().X(), ax.Direction().Y(), ax.Direction().Z()])
        rel = P - o
        radial = rel - (rel @ d) * d
        z = (verts - o) @ d
        return ff.Arc(kind="cylinder", radius=cyl.Radius() * mm, span_deg=min(360.0, math.degrees(u1 - u0)),
                      concave=float(N @ radial) < 0, axis=d, centre=o + d * 0.5 * (z.min() + z.max()),
                      labels=label, length=float(z.max() - z.min()), basis="exact", at=at, points=verts)
    tor = a.Torus()
    pos = tor.Position()
    o = np.array([pos.Location().X(), pos.Location().Y(), pos.Location().Z()]) * mm
    d = np.array([pos.Direction().X(), pos.Direction().Y(), pos.Direction().Z()])
    major = tor.MajorRadius() * mm
    rel = P - o
    radial = rel - (rel @ d) * d
    rn = float(np.linalg.norm(radial))
    if rn <= 0:
        return None
    tube = o + radial / rn * major
    return ff.Arc(kind="torus", radius=tor.MinorRadius() * mm, span_deg=min(360.0, math.degrees(v1 - v0)),
                  concave=float(N @ (P - tube)) < 0, axis=d, centre=o, major=major,
                  around_deg=min(360.0, math.degrees(u1 - u0)), labels=label, basis="exact", at=at)


#: Loose faces are sewn into bodies within this share of the part's diagonal - the clean check's own
#: sewing tolerance (cad/validity_brep.SEW_TOLERANCE), so the two never disagree on what is joined - and only
#: up to this many faces (cad/validity_brep.MAX_SEW_FACES).
SEW_SHARE = 1e-4
MAX_SEW_FACES = 20_000


def sewn_bodies(shape, surface: ff.Surface, mm: float) -> tuple[np.ndarray, int] | None:
    """THE BODIES OF A FILE OF LOOSE SURFACES: (each face's body, in face order; how many). Two faces are of
    one body where OpenCASCADE's sewing joins them along an edge - except an edge whose two faces lie back
    to back (cad/fea_features.CONTACT_DEG): that is two parts pressed together, sewn shut only because their
    edges happen to coincide. A face joined to nothing is a body of its own. None when there is nothing to
    sew or the sewing fails (the bodies stay uncounted)."""
    from OCP.BRepBuilderAPI import BRepBuilderAPI_Sewing
    from OCP.TopAbs import TopAbs_EDGE, TopAbs_FACE
    from OCP.TopExp import TopExp
    from OCP.TopoDS import TopoDS
    from OCP.TopTools import TopTools_IndexedDataMapOfShapeListOfShape, TopTools_IndexedMapOfShape

    try:
        fmap = TopTools_IndexedMapOfShape()
        TopExp.MapShapes_s(shape, TopAbs_FACE, fmap)
        n = fmap.Extent()
        if n == 0 or n > MAX_SEW_FACES:
            return None
        sewing = BRepBuilderAPI_Sewing(SEW_SHARE * float(surface.diag) / mm)
        for i in range(1, n + 1):
            sewing.Add(fmap.FindKey(i))
        sewing.Perform()
        sewn = sewing.SewedShape()
        if sewn is None or sewn.IsNull():
            return None
        sewn_faces = TopTools_IndexedMapOfShape()
        TopExp.MapShapes_s(sewn, TopAbs_FACE, sewn_faces)
        parent = list(range(sewn_faces.Extent() + 1))

        def root(i: int) -> int:
            while parent[i] != i:
                parent[i] = parent[parent[i]]
                i = parent[i]
            return i

        e2f = TopTools_IndexedDataMapOfShapeListOfShape()
        TopExp.MapShapesAndAncestors_s(sewn, TopAbs_EDGE, TopAbs_FACE, e2f)
        back_to_back = -math.cos(math.radians(ff.CONTACT_DEG))
        for i in range(1, e2f.Extent() + 1):
            ks = sorted({sewn_faces.FindIndex(f) for f in e2f.FindFromIndex(i)} - {0})
            if len(ks) != 2:
                continue                                  # a free edge, or several parts meeting at one
            if root(ks[0]) == root(ks[1]):
                continue                                  # joined already: this edge changes nothing (and
                                                          # its normals, read on the exact faces, are slow)
            dot = _edge_normals_dot(TopoDS.Edge_s(e2f.FindKey(i)), [sewn_faces.FindKey(k) for k in ks])
            if dot is not None and dot < back_to_back:
                continue                                  # two parts lying against each other: a contact
            parent[root(ks[0])] = root(ks[1])
        body = np.full(n, -1, dtype=np.int64)
        for i in range(1, n + 1):
            f = fmap.FindKey(i)
            g = sewing.Modified(f) if sewing.IsModified(f) else f
            k = sewn_faces.FindIndex(g)
            body[i - 1] = root(k) if k else -(i + 1)
        # numbered in face order, so the same file always gives the same numbers
        _u, first, inverse = np.unique(body, return_index=True, return_inverse=True)
        rank = np.empty(len(first), dtype=np.int64)
        rank[np.argsort(first, kind="stable")] = np.arange(len(first))
        body = rank[inverse.reshape(-1)]
        return body, int(len(first))
    except Exception as exc:  # noqa: BLE001 - uncounted, as before
        logger.info("fea measure: the loose faces could not be sewn into bodies (%s)", type(exc).__name__)
        return None


def _edge_normals_dot(edge, faces) -> float | None:
    """The dot product of two faces' outward normals at the middle of the edge they share; None when either
    cannot be read there."""
    from OCP.BRep import BRep_Tool
    from OCP.BRepAdaptor import BRepAdaptor_Curve, BRepAdaptor_Surface
    from OCP.BRepLProp import BRepLProp_SLProps
    from OCP.ShapeAnalysis import ShapeAnalysis_Surface
    from OCP.TopAbs import TopAbs_REVERSED
    from OCP.TopoDS import TopoDS

    try:
        if BRep_Tool.Degenerated_s(edge):
            return None
        c = BRepAdaptor_Curve(edge)
        p = c.Value(0.5 * (c.FirstParameter() + c.LastParameter()))
        ns = []
        for f_shape in faces:
            f = TopoDS.Face_s(f_shape)
            uv = ShapeAnalysis_Surface(BRep_Tool.Surface_s(f)).ValueOfUV(p, 1e-9)
            pr = BRepLProp_SLProps(BRepAdaptor_Surface(f), uv.X(), uv.Y(), 1, 1e-9)
            if not pr.IsNormalDefined():
                return None
            n = pr.Normal()
            sign = -1.0 if f.Orientation() == TopAbs_REVERSED else 1.0
            ns.append(np.array([n.X(), n.Y(), n.Z()]) * sign)
        return float(ns[0] @ ns[1])
    except Exception:  # noqa: BLE001 - unreadable: the faces are joined as the sewing joined them
        return None


def _cad_bodies(shape) -> int:
    from OCP.TopAbs import TopAbs_SHELL, TopAbs_SOLID
    from OCP.TopExp import TopExp
    from OCP.TopTools import TopTools_IndexedMapOfShape

    for what in (TopAbs_SOLID, TopAbs_SHELL):
        m = TopTools_IndexedMapOfShape()
        TopExp.MapShapes_s(shape, what, m)
        if m.Extent():
            return int(m.Extent())
    return 0


# ------------------------------------------------------------------------------ triangles ----
def measure_triangles(tris_mm) -> dict:
    """Measure a triangle file's surface ((m, 3, 3) millimetres). Its regions are the smooth
    stretches between sharp edges; its radii are fitted to the triangles."""
    started = time.time()
    tris = np.asarray(tris_mm, dtype=float).reshape(-1, 3, 3)
    xyz, t = _weld(tris.reshape(-1, 3), np.arange(len(tris) * 3).reshape(-1, 3))
    t = t[(t[:, 0] != t[:, 1]) & (t[:, 1] != t[:, 2]) & (t[:, 0] != t[:, 2])]
    if len(t) < 4:
        raise ValueError("too few triangles to measure")
    s = ff.Surface(xyz=xyz, tris=t, label=np.zeros(len(t), dtype=np.int64), names=["Region_1"])
    pairs = ff.edge_pairs(s)
    # OUTWARD: each closed piece faces away from the material (a file wound inside out, or one
    # piece of it, would read every valley as a ridge). A piece inside another is a sealed cavity's
    # wall, whose normals point into the cavity: it encloses a NEGATIVE volume when right, so the
    # sign each piece should have follows how deep it sits (outer skin +, cavity -, island +).
    comp = ff.components(len(t), pairs.tri_a, pairs.tri_b)
    a, b, c = (xyz[t[:, k]] for k in range(3))
    vol = np.bincount(comp, weights=np.einsum("ij,ij->i", a, np.cross(b, c)) / 6.0)
    depth = ff.nesting_depth(xyz, t, comp)
    flip = (vol[comp] < 0) != (depth[comp] % 2 == 1)
    if flip.any():
        t = t.copy()
        t[flip] = t[flip][:, [0, 2, 1]]
        s = ff.Surface(xyz=xyz, tris=t, label=s.label, names=s.names)
        pairs = ff.edge_pairs(s)
    label = ff.smooth_regions(t, pairs, len(t))
    names = [f"Region_{k}" for k in range(1, int(label.max()) + 2)]
    s = ff.Surface(xyz=xyz, tris=t, label=label, names=names)
    arcs = []
    for tri_idx, bends in ff.curved_patches(s, pairs):
        arc = ff.fit_round(*ff.patch_samples(s, pairs, bends), s.diag)
        if arc is not None:
            arc.labels = {int(x) for x in np.unique(label[tri_idx])}
            arcs.append(arc)
    # a body is an outer skin: a cavity's wall belongs to the body around it
    bodies = int(np.count_nonzero((np.abs(vol) > 1e-12 * s.diag ** 3) & (depth % 2 == 0)))
    return _record(s, arcs, bodies=bodies, basis="triangles", kinds=None, started=started, pairs=pairs)


# --------------------------------------------------------------------------------- record ----
def _record(s: ff.Surface, arcs: list[ff.Arc], *, bodies: int, basis: str, kinds: list[str] | None,
            started: float, pairs: ff.EdgePairs | None = None,
            max_ray_triangles: int = MAX_RAY_TRIANGLES, curvature: dict[int, float] | None = None) -> dict:
    notes: list[str] = []
    pairs = pairs if pairs is not None else ff.edge_pairs(s)
    complete = True
    if len(s.tris) <= max_ray_triangles:
        thick = ff.wall_thickness(s)
    else:
        thick = {"per_label": {}, "min": None, "rays": 0}
        complete = False
        notes.append(f"The walls were not measured: the surface has {len(s.tris)} triangles, over "
                     f"the {max_ray_triangles} measured quickly.")
    per = thick["per_label"]
    thin = thick.get("thin") or {}
    # the part keeps at most MAX_THIN_SPOTS_PART thin spots, the thinnest
    kept = sorted(((sp["thickness"], k, i) for k, t in thin.items() for i, sp in enumerate(t["spots"])))
    kept_set = {(k, i) for _t, k, i in kept[:ff.MAX_THIN_SPOTS_PART]}
    # a face that is one round (a cylinder, a torus, a fitted fillet): its radius and which way
    # it bends, so a plan can give a convex round enough elements to keep its shape
    round_of = {next(iter(a.labels)): a for a in arcs if len(a.labels) == 1}
    faces = {}
    # PLAN-fea-12: the rounds under the measure's own floor (cad/fea_features.FLOOR_FRACTION of the part)
    floor_mm = ff.FLOOR_FRACTION * float(s.diag)
    sub_floor: list[dict] = []
    for k, name in enumerate(s.names):
        idx = np.flatnonzero(s.label == k)
        if not len(idx):
            continue
        area = float(s.areas[idx].sum())
        row = {"kind": (kinds[k] if kinds else "region"), "area_mm2": _r(area),
               "centroid_mm": _pt(np.average(s.centroids[idx], axis=0, weights=np.maximum(s.areas[idx], 1e-300)))}
        if k in round_of and round_of[k].radius < floor_mm:
            # PLAN-fea-12: a round under the measure's own floor is a micro detail, never a zone (below)
            sub_floor.append({"kind": "round", "size_mm": _r(round_of[k].radius), "faces": [name]})
        elif k in round_of:
            row["radius_mm"] = _r(round_of[k].radius)
            row["concave"] = bool(round_of[k].concave)
        elif curvature and k in curvature and curvature[k] <= ROUND_CAP_SHARE * float(s.diag):
            # a curved face no cylinder or torus describes (a swept wire, a spring's coil, a
            # free-form blend): its own tightest bend, read from the CAD (PLAN-fea-7). A gentle
            # curve over ROUND_CAP_SHARE of the part is left to the base size.
            row["radius_mm"] = _r(curvature[k])
            row["radius_basis"] = "curvature"
        if k in per:
            row["thickness_min_mm"] = _r(per[k][0])
            row["thickness_median_mm"] = _r(per[k][1])
            if k in thin:
                # PLAN-fea-3: thin only in places - the rest of the face's wall, and each thin spot
                row["thickness_wall_mm"] = _r(thin[k]["wall"])
                row["thin_spots"] = [{"at_mm": _pt(sp["at"]), "radius_mm": _r(sp["radius"]),
                                      "thickness_mm": _r(sp["thickness"])}
                                     for i, sp in enumerate(thin[k]["spots"]) if (k, i) in kept_set]
        faces[name] = row

    def face_wall(k: int) -> float:
        # a face thin only in places: the wall of the rest of it (its thin spots are zones of their own)
        return thin[k]["wall"] if k in thin else per[k][0]

    def wall(labels) -> float | None:
        vals = [face_wall(k) for k in labels if k in per]
        return _r(min(vals)) if vals else None

    spots: list[dict] = []
    bores: list[dict] = []
    merged = ff.merge_coaxial(arcs)
    for arc in merged:
        kind = ff.kind_of(arc, s.diag)
        if kind is None:
            continue
        t, shape = kind
        if arc.radius < floor_mm:
            # PLAN-fea-12, UNDER THE MEASURE'S OWN FLOOR (27 Oct): a round whose radius is less than the floor the
            # wall readings themselves stop at (FLOOR_FRACTION of the part) is no feature the plan can hold - a
            # kart frame's 0.0016 mm "fillets" on a 600 mm tube asked for 0.4 um elements - so it is a micro
            # detail: set aside and named, never a hot spot
            sub_floor.append({"kind": t, "size_mm": _r(arc.radius), "faces": [s.names[k] for k in sorted(arc.labels)]})
            continue
        outer = ff.is_bore(arc, merged) if t == ff.HOLE else None
        if outer is not None:
            # PLAN-fea-4: A BORE IS THE PART'S MAIN WALL, NOT A HOLE (ff.BORE_*): no hot spot - the wall rule
            # sizes it - and it is named
            bores.append({"faces": [s.names[k] for k in sorted(arc.labels)], "radius_mm": _r(arc.radius),
                          "length_mm": _r(arc.length), "outer_radius_mm": _r(outer.radius),
                          "outer_faces": [s.names[k] for k in sorted(outer.labels)]})
            continue
        spot = {"type": t, "shape": SHAPE_WORDS[shape], "radius_mm": _r(arc.radius),
                "span_deg": _r(arc.span_deg), "faces": [s.names[k] for k in sorted(arc.labels)],
                "axis": _pt(arc.axis), "basis": arc.basis, "capped": False}
        if t == ff.HOLE:
            spot["at_mm"] = _pt(arc.centre)
            spot["depth_mm"] = _r(arc.length)
        else:
            spot["at_mm"] = _pt(arc.at if arc.at is not None else arc.centre)
        if arc.kind == "torus":
            spot["around_deg"] = _r(arc.around_deg)
        spot["local_thickness_mm"] = wall(arc.labels)
        spots.append(spot)
    for c in ff.sharp_corners(s, pairs):
        local = wall(c["labels"])
        spots.append({"type": ff.SHARP_CORNER, "shape": SHAPE_WORDS[ff.SHARP_CORNER],
                      "angle_deg": _r(c["angle_deg"]), "length_mm": _r(c["length_mm"]),
                      "faces": list(dict.fromkeys(s.names[k] for k in c["labels"])), "at_mm": _pt(c["at_mm"]),
                      "basis": "exact" if basis == "cad" else "triangles",
                      # THE CAP (Areen, 20 Oct): a perfectly sharp inside corner's stress never
                      # settles under refinement, so the plan stops refining it at a set level; the
                      # thinner wall beside it is the length that level is set against
                      "capped": True, "cap_basis_mm": local, "local_thickness_mm": local})
    # WHERE TWO BODIES TOUCH (cad/fea_features.contacts): contacts, never inside corners - said once per
    # pair of bodies, for the assembly reading and the stage
    touching = ff.contacts(s, pairs)
    spots.sort(key=lambda h: (_TYPE_ORDER[h["type"]], h.get("radius_mm") or h.get("angle_deg") or 0.0))
    counts: dict[str, int] = {}
    for h in spots:
        counts[h["type"]] = counts.get(h["type"], 0) + 1
    if len(spots) > ff.MAX_HOT_SPOTS:
        notes.append(f"{len(spots)} hot spots found; the {ff.MAX_HOT_SPOTS} sharpest and smallest are kept.")
        spots = spots[:ff.MAX_HOT_SPOTS]
    for i, h in enumerate(spots, start=1):
        h["id"] = f"H{i}"
    m = thick["min"]
    thickness = {"method": "ray along the inward normal from points spread over every face",
                 "rays": int(thick["rays"])}
    if m is not None:
        thickness.update(min_mm=_r(m[0]), face=s.names[m[1]], at_mm=_pt(m[2]))
    if basis == "triangles":
        notes.append("A triangle file has no CAD faces: its smooth regions (Region_k) are measured, "
                     "and its radii are fitted to the triangles.")
    if bores:
        notes.append(f"{len(bores)} bore(s) - the inside of a tube, sleeve or ring, sharing its axis with the "
                     "outside - are the part's own wall, sized by the wall rule, not stress-raising holes.")
    out = {"version": VERSION, "basis": basis, "unit": "mm", "bodies": int(bodies), "diag_mm": _r(s.diag),
           "face_names": "Face_k" if basis == "cad" else "Region_k", "faces": faces,
           "thickness": thickness, "hot_spots": spots, "counts": counts, "complete": complete,
           "notes": notes, "seconds": round(time.time() - started, 2)}
    if bores:
        out["bores"] = bores
    if touching:
        out["contact_count"] = len(touching)
        out["contacts"] = [{"faces": [s.names[k] for k in c["labels"]], "length_mm": _r(c["length_mm"]),
                            "at_mm": _pt(c["at_mm"]),
                            **({"bodies": [b + 1 for b in c["bodies"]]} if c["bodies"] is not None else {})}
                           for c in touching[:MAX_CONTACTS]]
        notes.append(f"{len(touching)} place(s) where two surfaces lie against each other (parts touching) are "
                     "read as contacts, not as sharp inside corners.")
    if sub_floor:
        out["sub_floor"] = sub_floor
    if thick.get("lying"):
        # PLAN-fea-3: FACES OF ONE BODY LYING ON EACH OTHER (a zero-thickness skin or membrane), read through
        # rather than taken for a wall - named, so the plan and the delivery can say where
        out["coincident"] = [{"faces": [s.names[a], s.names[b]]} for a, b in thick["lying"]]
    return out


__all__ = ["FACT_KEY", "VERSION", "measure_cad", "measure_cad_file", "measure_cad_surface",
           "measure_triangles"]
