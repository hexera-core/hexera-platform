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
                          "thickness_median_mm"}, ...},
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

VERSION = 1
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
    started = time.time()
    surface, faces, kinds = _brep_surface(shape, float(mm_per_unit))
    arcs = []
    for k, face in enumerate(faces):
        idx = np.flatnonzero(surface.label == k)
        arc = _face_arc(face, kinds[k], surface, idx, float(mm_per_unit))
        if arc is not None:
            arcs.append(arc)
    return _record(surface, arcs, bodies=_cad_bodies(shape), basis="cad", kinds=kinds, started=started)


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
    base = 0
    for i in range(1, fmap.Extent() + 1):
        face = TopoDS.Face_s(fmap.FindKey(i))
        faces.append(face)
        kinds.append(_kind_word(BRepAdaptor_Surface(face, True).GetType()))
        loc = TopLoc_Location()
        tri = BRep_Tool.Triangulation_s(face, loc)
        if tri is None or tri.NbTriangles() == 0:
            continue
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
    return ff.Surface(xyz=xyz, tris=tris[keep], label=label[keep], names=names), faces, kinds


def _weld(xyz: np.ndarray, tris: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """One vertex where several faces' tessellations put the same point (their shared edges)."""
    from scipy.spatial import cKDTree

    diag = float(np.linalg.norm(xyz.max(axis=0) - xyz.min(axis=0))) or 1.0
    pairs = cKDTree(xyz).query_pairs(1e-7 * diag, output_type="ndarray")
    rep = np.arange(len(xyz))
    if len(pairs):
        from scipy.sparse import coo_matrix
        from scipy.sparse.csgraph import connected_components

        g = coo_matrix((np.ones(len(pairs)), (pairs[:, 0], pairs[:, 1])), shape=(len(xyz), len(xyz)))
        _n, comp = connected_components(g, directed=False)
        _u, first = np.unique(comp, return_index=True)
        rep = first[comp]
    used, inverse = np.unique(rep, return_inverse=True)
    return xyz[used], inverse[tris]


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
                      labels=label, length=float(z.max() - z.min()), basis="exact", at=at)
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
    # OUTWARD: each piece is turned so it encloses a positive volume (a file wound inside out, or
    # one piece of it, would read every valley as a ridge)
    comp = ff.components(len(t), pairs.tri_a, pairs.tri_b)
    a, b, c = (xyz[t[:, k]] for k in range(3))
    vol = np.bincount(comp, weights=np.einsum("ij,ij->i", a, np.cross(b, c)) / 6.0)
    flip = vol[comp] < 0
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
    bodies = int(np.count_nonzero(np.abs(vol) > 1e-12 * s.diag ** 3))
    return _record(s, arcs, bodies=bodies, basis="triangles", kinds=None, started=started, pairs=pairs)


# --------------------------------------------------------------------------------- record ----
def _record(s: ff.Surface, arcs: list[ff.Arc], *, bodies: int, basis: str, kinds: list[str] | None,
            started: float, pairs: ff.EdgePairs | None = None) -> dict:
    notes: list[str] = []
    pairs = pairs if pairs is not None else ff.edge_pairs(s)
    complete = True
    if len(s.tris) <= MAX_RAY_TRIANGLES:
        thick = ff.wall_thickness(s)
    else:
        thick = {"per_label": {}, "min": None, "rays": 0}
        complete = False
        notes.append(f"The walls were not measured: the surface has {len(s.tris)} triangles, over "
                     f"the {MAX_RAY_TRIANGLES} measured quickly.")
    per = thick["per_label"]
    faces = {}
    for k, name in enumerate(s.names):
        idx = np.flatnonzero(s.label == k)
        if not len(idx):
            continue
        area = float(s.areas[idx].sum())
        row = {"kind": (kinds[k] if kinds else "region"), "area_mm2": _r(area),
               "centroid_mm": _pt(np.average(s.centroids[idx], axis=0, weights=np.maximum(s.areas[idx], 1e-300)))}
        if k in per:
            row["thickness_min_mm"] = _r(per[k][0])
            row["thickness_median_mm"] = _r(per[k][1])
        faces[name] = row

    def wall(labels) -> float | None:
        vals = [per[k][0] for k in labels if k in per]
        return _r(min(vals)) if vals else None

    spots: list[dict] = []
    for arc in ff.merge_coaxial(arcs):
        kind = ff.kind_of(arc)
        if kind is None:
            continue
        t, shape = kind
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
                      "faces": [s.names[k] for k in c["labels"]], "at_mm": _pt(c["at_mm"]),
                      "basis": "exact" if basis == "cad" else "triangles",
                      # THE CAP (Areen, 20 Oct): a perfectly sharp inside corner's stress never
                      # settles under refinement, so the plan stops refining it at a set level; the
                      # thinner wall beside it is the length that level is set against
                      "capped": True, "cap_basis_mm": local, "local_thickness_mm": local})
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
    return {"version": VERSION, "basis": basis, "unit": "mm", "bodies": int(bodies),
            "face_names": "Face_k" if basis == "cad" else "Region_k", "faces": faces,
            "thickness": thickness, "hot_spots": spots, "counts": counts, "complete": complete,
            "notes": notes, "seconds": round(time.time() - started, 2)}


__all__ = ["FACT_KEY", "VERSION", "measure_cad", "measure_cad_file", "measure_triangles"]
